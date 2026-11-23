#!/usr/bin/env python3
"""Zen Garden welcome runner — pulls new hires from BambooHR, composes
mobile-safe verbatim cards (welcome_compose.py), posts to #zengarden.

Privacy model: the public repo never holds GTKY answers. This runner
fetches them from the BambooHR API at post time using BAMBOOHR_API_KEY
(GitHub secret). welcome_batches.json (committed) holds only employee
ids, dates and pins. Logs print NO answer text (Actions logs are public)
— only counts, match results and flag labels.

Lanes:
  --batch N      post batch N from welcome_batches.json (if due & armed)
  (no args)      scheduled tick: post the oldest due armed batch, else
                 no_op. (Week-2 auto-detection lane lands before Dec 7.)
  --dry-run      compose + log sanitized summary, post nothing
  --offline F    compose from a local answers file (never in CI) and
                 print the full preview — for local review only.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import urllib.request
from datetime import date, datetime
from zoneinfo import ZoneInfo

from welcome_compose import (
    OPENERS, MAX_LINE, clean, compose_person, review_flags,
)

ET = ZoneInfo("America/New_York")
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(SCRIPT_DIR, "posted_state.json")
BATCHES_FILE = os.path.join(SCRIPT_DIR, "welcome_batches.json")
COMPANY = "amigocareaba"
CHANNEL_ID = "C07PFAY31FH"  # #zengarden


# ── BambooHR ────────────────────────────────────────────────────────────
def bamboo_get(url: str) -> dict:
    key = os.environ.get("BAMBOOHR_API_KEY", "")
    if not key:
        sys.exit("BAMBOOHR_API_KEY not set")
    token = base64.b64encode(f"{key}:x".encode()).decode()
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Basic {token}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def fetch_widget() -> list[dict]:
    data = bamboo_get(f"https://{COMPANY}.bamboohr.com/api/v1/onboarding/new-hire-widget")
    return data.get("newHires", [])


def fetch_directory() -> dict[str, dict]:
    data = bamboo_get(
        f"https://api.bamboohr.com/api/gateway.php/{COMPANY}/v1/employees/directory"
    )
    return {str(e.get("id")): e for e in data.get("employees", [])}


# ── Slack ───────────────────────────────────────────────────────────────
def slack_clients():
    from slack_sdk import WebClient

    bot = os.environ.get("SLACK_BOT_TOKEN", "")
    user = os.environ.get("SLACK_USER_TOKEN", "")
    if not bot or not user:
        sys.exit("Slack tokens not set")
    return WebClient(token=bot), WebClient(token=user)


def norm(s: str) -> str:
    return " ".join("".join(c for c in s.lower() if c.isalnum() or c.isspace()).split())


def slack_user_map(user_client) -> dict[str, str]:
    """normalized full name -> slack user id (humans only)."""
    out: dict[str, str] = {}
    cursor = None
    while True:
        resp = user_client.users_list(cursor=cursor, limit=200)
        for u in resp["members"]:
            if u.get("deleted") or u.get("is_bot") or u["id"] == "USLACKBOT":
                continue
            prof = u.get("profile", {})
            for name in (prof.get("real_name"), prof.get("display_name"), u.get("real_name")):
                if name:
                    out.setdefault(norm(name), u["id"])
        cursor = resp.get("response_metadata", {}).get("next_cursor") or None
        if not cursor:
            break
    return out


def resolve_mention(person: dict, names_to_id: dict[str, str]) -> tuple[str, bool]:
    """-> (mention text, resolved?)  Tries preferred+last, first+last."""
    last_word = person["last_name"].split()[-1] if person["last_name"] else ""
    for cand in (
        f"{person['first_name']} {person['last_name']}",
        f"{person.get('legal_first', '')} {person['last_name']}",
        f"{person['first_name']} {last_word}",
        f"{person.get('legal_first', '')} {last_word}",
        person["display_name"],
    ):
        uid = names_to_id.get(norm(cand))
        if uid:
            return f"<@{uid}>", True
    return f"*{person['display_name']}*", False


# ── Assembly ────────────────────────────────────────────────────────────
def title_clean(t: str) -> str:
    return re.sub(r"\s*\((?:RBT|BT)\)\s*$", "", t or "").strip() or "Behavior Technician"


def build_person(eeid: str, widget_by_id: dict, directory: dict, pins: dict) -> dict | None:
    """Join widget + directory for one employee. None -> not eligible."""
    d = directory.get(eeid)
    if not d:
        return None  # not an active employee anymore -> silently dropped
    w = widget_by_id.get(eeid, {})
    first = (d.get("preferredName") or w.get("preferredFirstName") or d.get("firstName") or "").strip()
    last = (d.get("lastName") or w.get("lastName") or "").strip()
    # NOTE: BambooHR marks every GTKY item hidden:true by default even
    # while displaying the answers on the company-wide home widget, so
    # the flag carries no opt-out signal (verified 2026-11-22: 19/19
    # items hidden across people whose answers the widget shows). We
    # therefore accept answered items; the question allowlist in
    # welcome_compose.py remains the real privacy gate.
    answers = {}
    for item in (w.get("getToKnowYou") or []):
        q, a = item.get("question", ""), (item.get("employeeResponse") or "").strip()
        if q and a:
            answers[q] = {"text": a}
    person = {
        "eeid": eeid,
        "display_name": f"{first} {last}".strip(),
        "first_name": first,
        "legal_first": (d.get("firstName") or "").strip(),
        "last_name": last,
        "job_title": title_clean(d.get("jobTitle")),
        "pronouns": d.get("pronouns"),
        "answers": answers,
    }
    if eeid in pins:
        person["pin"] = pins[eeid]
    return person


def compose_message(people: list[dict], names_to_id: dict | None) -> tuple[str, list[str], list[str]]:
    blocks, flags, unresolved = [], [], []
    for p in people:
        lines, pflags = compose_person(p)
        if names_to_id is not None:
            mention, ok = resolve_mention(p, names_to_id)
            if not ok:
                unresolved.append(p["eeid"])
            lines[0] = lines[0].replace(f"@{p['display_name']}", mention, 1)
        blocks.append("\n".join(lines))
        flags += [f"eeid {p.get('eeid', p['first_name'])}: {f}" for f in pflags]
    opener = OPENERS.get(len(people), OPENERS[3])
    text = f"\U0001f389 *{opener}*\n\n" + "\n\n".join(blocks) + "\n\nSay hi below \U0001f44b"
    return text, flags, unresolved


# ── State ───────────────────────────────────────────────────────────────
def load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(**updates) -> None:
    state = load_state()
    state.update(updates)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
        f.write("\n")


# ── Main ────────────────────────────────────────────────────────────────
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", help="batch id from welcome_batches.json")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--offline", help="local answers JSON (local preview only)")
    ap.add_argument("--simulate-date", help="YYYY-MM-DD, pretend today is this date")
    args = ap.parse_args()

    with open(BATCHES_FILE) as f:
        spec = json.load(f)

    if args.offline:  # local preview path: no network, prints full text
        with open(args.offline) as f:
            local = json.load(f)
        for b in spec["batches"]:
            people = [local["people"][k] for k in b["local_keys"]]
            text, flags, _ = compose_message(people, None)
            print(f"== batch {b['id']} ({b['date']}) ==\n{text}\n")
            for fl in flags:
                print(f"  ⚠ {fl}")
        return

    today = (
        date.fromisoformat(args.simulate_date)
        if args.simulate_date
        else datetime.now(ET).date()
    )
    state = load_state()
    done = set(state.get("welcome_batches_done", []))

    due = None
    if args.batch:
        due = next((b for b in spec["batches"] if b["id"] == args.batch), None)
        if due is None:
            sys.exit(f"no batch {args.batch} in {BATCHES_FILE}")
    else:
        for b in spec["batches"]:  # oldest due first
            if b["id"] not in done and date.fromisoformat(b["date"]) <= today:
                due = b
                break

    if due is None or due["id"] in done:
        print(f"welcome no_op: nothing due (done={sorted(done)})")
        return
    if not spec.get("armed") and not args.batch:
        print(f"welcome no_op: batch {due['id']} due but spec not armed")
        return
    if today.weekday() >= 5 and not args.batch:
        print("welcome no_op: weekend")
        return

    widget_by_id = {str(h.get("id")): h for h in fetch_widget()}
    directory = fetch_directory()
    pins = spec.get("pins", {})

    people, dropped = [], []
    for eeid in due["people"]:
        p = build_person(str(eeid), widget_by_id, directory, pins)
        (people if p else dropped).append(p or str(eeid))
    if not people:
        print(f"welcome no_op: batch {due['id']} has no eligible people left")
        save_state(welcome_batches_done=sorted(done | {due['id']}))
        return

    bot, user_client = slack_clients()
    names_to_id = slack_user_map(user_client)
    text, flags, unresolved = compose_message(people, names_to_id)

    # Sanitized log only — counts and labels, never answer text.
    print(f"batch {due['id']}: {len(people)} people composed, {len(dropped)} dropped")
    print(f"  widget: {len(widget_by_id)} hires returned")
    sample = next(iter(widget_by_id.values()), None)
    if sample is not None:
        print(f"  widget item keys: {sorted(sample.keys())}")
        gtky = sample.get("getToKnowYou")
        print(f"  sample getToKnowYou type: {type(gtky).__name__}, "
              f"len: {len(gtky) if isinstance(gtky, list) else 'n/a'}")
        if isinstance(gtky, list) and gtky:
            print(f"  sample Q&A keys: {sorted(gtky[0].keys())}")
    for p in people:
        items = widget_by_id.get(p["eeid"], {}).get("getToKnowYou") or []
        hidden = sum(1 for i in items if i.get("hidden"))
        answered = sum(1 for i in items if (i.get("employeeResponse") or "").strip())
        print(f"  eeid {p['eeid']}: usable={len(p['answers'])} "
              f"(items={len(items)}, hidden={hidden}, answered={answered})")
    for eeid, h in widget_by_id.items():
        g = h.get("getToKnowYou")
        n = len(g) if isinstance(g, list) else -1
        print(f"  widget eeid {eeid}: hireDate={h.get('hireDate')}, gtky_items={n}")
    for eeid in dropped:
        print(f"  dropped (not active): eeid {eeid}")
    for eeid in unresolved:
        print(f"  mention unresolved -> bold name: eeid {eeid}")
    print(f"  lines: {len(text.splitlines())}, flags: {len(flags)}")

    if args.dry_run:
        print("DRY RUN: nothing posted")
        return

    bot.chat_postMessage(channel=CHANNEL_ID, text=text, unfurl_links=False)
    welcomed = state.get("welcomed", {})
    for p in people:
        welcomed[p["eeid"]] = today.isoformat()
    save_state(
        welcomed=welcomed,
        welcome_batches_done=sorted(done | {due["id"]}),
        last_welcome_post=datetime.now(ET).isoformat(timespec="seconds"),
    )
    print(f"posted batch {due['id']} to #zengarden")


if __name__ == "__main__":
    main()
