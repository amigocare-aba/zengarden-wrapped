#!/usr/bin/env python3
"""Adversarial integration test for the welcome pipeline.

Tries to break welcome_runner + welcome_compose on the paths most likely
to fail in production. NEVER touches Slack or BambooHR: every network
function is replaced with fixtures. stdlib only. Run: python3 test_welcome_integration.py
"""

import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout

import welcome_runner as wr

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name + (f" — {detail}" if detail and not cond else ""))
    print(("  ok " if cond else "  FAIL ") + name + ("" if cond else f"  [{detail}]"))


# ── Fixtures (fake people only — no real employee data) ────────────────
MONSTER = "Supercalifragilistic-expialidocious-and-then-some-more-token"
WIDGET = [
    {
        "id": 901,
        "preferredFirstName": "Ana",
        "lastName": "Test",
        "hireDate": "2026-11-09",
        "getToKnowYou": [
            {"question": "Do you have a preferred name?", "employeeResponse": "Ana", "hidden": True},
            {"question": "Do you have any pets? if so, what kind? What are their names?",
             "employeeResponse": "Yes, I have a dog named Río-Grande!!", "hidden": True},
            {"question": "What is a fun fact about you?",
             "employeeResponse": "I love coffee , a lot", "hidden": True},
            {"question": "What is your favorite show/movie?",
             "employeeResponse": MONSTER + " here", "hidden": True},
            {"question": "Please list any allergies",
             "employeeResponse": "Peanuts (SHOULD NEVER POST)", "hidden": True},
            {"question": "Do you have any hobbies? If so, what are they?",
             "employeeResponse": "", "hidden": True},
        ],
    },
    {"id": 902, "preferredFirstName": "Bob", "lastName": "Unmatchable",
     "hireDate": "2026-11-09", "getToKnowYou": None},
    {"id": 903, "preferredFirstName": "Tina", "lastName": "Terminated",
     "hireDate": "2026-11-09", "getToKnowYou": None},
]
DIRECTORY = {  # 903 intentionally absent = terminated in BambooHR
    "901": {"id": "901", "firstName": "Anastasia", "preferredName": "Ana",
            "lastName": "Test", "jobTitle": "Behavior Technician (BT)", "pronouns": "She/her"},
    "902": {"id": "902", "firstName": "Bob", "preferredName": None,
            "lastName": "Unmatchable", "jobTitle": "Registered Behavior Technician (RBT)",
            "pronouns": None},
}
SLACK_NAMES = {"ana test": "U901", "anastasia test": "U901"}  # Bob unresolvable
POSTS: list[dict] = []


class FakeBot:
    def chat_postMessage(self, **kw):
        POSTS.append(kw)
        return {"ok": True}


def install_fixtures(tmp: str, armed: bool = True) -> None:
    wr.fetch_widget = lambda: WIDGET
    wr.fetch_directory = lambda: {k: dict(v) for k, v in DIRECTORY.items()}
    wr.slack_clients = lambda: (FakeBot(), object())
    wr.slack_user_map = lambda _c: dict(SLACK_NAMES)
    wr.STATE_FILE = os.path.join(tmp, "state.json")
    wr.BATCHES_FILE = os.path.join(tmp, "batches.json")
    with open(wr.STATE_FILE, "w") as f:  # each scenario starts with clean state
        f.write("{}")
    with open(wr.BATCHES_FILE, "w") as f:
        json.dump({"armed": armed, "batches": [
            {"id": "9", "date": "2026-11-21", "people": ["901", "902", "903"]}
        ], "pins": {}}, f)


def run(*argv: str) -> str:
    sys.argv = ["welcome_runner.py", *argv]
    buf = io.StringIO()
    with redirect_stdout(buf):
        try:
            wr.main()
        except SystemExit as e:
            print(f"[exit {e.code}]")
        except Exception as e:  # scenario 5 injects a crash on purpose
            print(f"[crashed: {e}]")
    return buf.getvalue()


def main() -> None:
    tmp = tempfile.mkdtemp(prefix="zen_qa_")

    print("1) Weekend tick: due batch must NOT post on a Saturday")
    install_fixtures(tmp)
    out = run("--simulate-date", "2026-11-21")
    check("weekend no_op", "weekend" in out and not POSTS)

    print("2) Unarmed spec: scheduled lane must hold fire")
    install_fixtures(tmp, armed=False)
    out = run("--simulate-date", "2026-11-23")
    check("unarmed no_op", "not armed" in out and not POSTS)

    print("3) Monday tick: posts once, drops the terminated person")
    install_fixtures(tmp)
    out = run("--simulate-date", "2026-11-23")
    check("posted exactly once", len(POSTS) == 1, f"posts={len(POSTS)}")
    text = POSTS[0]["text"] if POSTS else ""
    check("terminated dropped from card", "Tina" not in text and "903" not in text)
    check("opener recounted to Two", "Two new amigos" in text, text.splitlines()[0] if text else "no post")
    check("mention resolved for Ana", "<@U901>" in text)
    check("bold fallback for Bob", "*Bob Unmatchable*" in text)
    check("lead 'Yes,' trimmed", '"I have a dog named Río-Grande!!"' in text)
    check("hyphen+unicode survived verbatim", "Río-Grande" in text)
    check("space-before-comma tidied", "coffee, a lot" in text)
    check("monster token refused by guard", MONSTER not in text)
    check("allergy answer never posts", "Peanuts" not in text)
    check("empty answer skipped", '""' not in text)
    check("title suffix stripped", "(BT)" not in text and "(RBT)" not in text)
    bad = [l for l in text.splitlines() if len(l) > wr.MAX_LINE + 4 and not l.startswith("\U0001f389")]
    check("every line fits a phone", not bad, f"long lines: {bad[:2]}")
    check("log sanitized (no answer text)", "Río-Grande" not in out and "Peanuts" not in out)
    with open(wr.STATE_FILE) as f:
        state = json.load(f)
    check("state recorded 901+902 only",
          set(state.get("welcomed", {})) == {"901", "902"} and "9" in state.get("welcome_batches_done", []))

    print("4) Second tick same day: must be a no_op (no double post)")
    out = run("--simulate-date", "2026-11-23")
    check("duplicate tick no_op", len(POSTS) == 1 and "nothing due" in out, f"posts={len(POSTS)}")

    print("5) Crash between post and state-save: document the exposure")
    install_fixtures(tmp)
    POSTS.clear()
    real_save = wr.save_state
    wr.save_state = lambda **kw: (_ for _ in ()).throw(RuntimeError("simulated crash"))
    out = run("--simulate-date", "2026-11-23")
    wr.save_state = real_save
    out2 = run("--simulate-date", "2026-11-23")
    check("KNOWN GAP demonstrated: unsaved state -> repost on next tick",
          len(POSTS) == 2, f"posts={len(POSTS)}")

    print("6) All-gone batch: skip without posting, mark done")
    install_fixtures(tmp)
    POSTS.clear()
    wr.fetch_directory = lambda: {}
    out = run("--simulate-date", "2026-11-23")
    check("empty batch skipped, no post", not POSTS and "no eligible people" in out)
    with open(wr.STATE_FILE) as f:
        check("empty batch marked done", "9" in json.load(f).get("welcome_batches_done", []))

    print(f"\n{'='*52}\nRESULT: {len(PASS)} passed, {len(FAIL)} failed")
    for f_ in FAIL:
        print(f"  FAILED: {f_}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
