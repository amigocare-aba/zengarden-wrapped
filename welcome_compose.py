#!/usr/bin/env python3
"""Zen Garden welcome-card composer.

Contract: the composer TRANSPORTS answers, it never writes.
  - Allowed transforms (whitelist): collapse whitespace, wrap at word
    boundaries, wrap in quotes, prefix an emoji keyed to the QUESTION.
  - The reconstruction guard proves nothing else happened: the wrapped
    lines must re-join into the exact cleaned answer, or we refuse to
    compose (no post > wrong post).
  - Answers captured incompletely (truncated) are never composed.
  - Any paraphrase ("Cat mom x3") can only enter via a human edit in the
    review step, stored as an explicit override with an editor name.
"""

from __future__ import annotations

import json
import re
import sys
import textwrap

MAX_LINE = 38  # phone-Slack wraps ~40 chars; stay under it

# Question allowlist, in card priority order: (match prefix, emoji)
# Questions not listed here (allergies, role-at-amigo, ...) never post.
SAFE_QUESTIONS = [
    ("do you have a preferred name", "\U0001f4ac"),   # 💬 shown only if it adds info
    ("do you have any pets", "\U0001f43e"),           # 🐾
    ("what is a fun fact", "✨"),                 # ✨
    ("what is your favorite show/movie", "\U0001f3ac"),  # 🎬
    ("tell us a little about your cultural", "\U0001f30e"),  # 🌎 config: heritage_on
    ("do you have any hobbies", "\U0001f3a8"),        # 🎨
]
MAX_FACTS = 3  # besides the preferred-name line

# Tiny starter list of common typos; only FLAGS for human review, never edits.
REVIEW_TYPOS = {"exited": "excited", "teh": "the", "recieve": "receive"}


class ComposerError(Exception):
    pass


def clean(text: str) -> str:
    text = " ".join(text.split())
    return re.sub(r"\s+([,.!?])", r"\1", text)  # "Kat , Kathy" -> "Kat, Kathy"


# Leading affirmations read oddly without the question shown. Trimming the
# prefix is a pure deletion (suffix preserved by construction), and every
# trim is reported in the review flags.
TRIM_LEAD = re.compile(
    r"^(yes[,.!]?\s+i\s+do[,.!:]?\s+|yes[,.!:]\s+|yeah[,.!:]\s+|yep[,.!:]\s+)",
    re.IGNORECASE,
)


def trim_lead(text: str) -> tuple[str, str | None]:
    m = TRIM_LEAD.match(text)
    if not m:
        return text, None
    rest = text[m.end():]
    if rest and rest[0].islower():
        rest = rest[0].upper() + rest[1:]
    return (rest, m.group(0).strip()) if rest else (text, None)


def compose_fact(emoji: str, answer: str) -> list[str]:
    """Verbatim fact -> short quoted lines. Raises if meaning could drift."""
    text = clean(answer)
    if not text:
        raise ComposerError("empty answer")
    body = textwrap.wrap(text, MAX_LINE, break_long_words=False, break_on_hyphens=False)
    if " ".join(body) != text:  # reconstruction guard
        raise ComposerError(f"guard failed for: {answer!r}")
    if any(len(line) > MAX_LINE for line in body):  # un-wrappable monster token
        raise ComposerError(f"line would exceed mobile width: {answer!r}")
    lines = [f'{emoji} "{body[0]}' if i == 0 else body[i] for i in range(len(body))]
    lines[-1] += '"'
    return lines


def norm_name(s: str) -> str:
    return "".join(c for c in s.lower() if c.isalnum() or c.isspace()).strip()


def skip_preferred(answer: str, first: str, full: str) -> bool:
    a = norm_name(answer)
    return a in {norm_name(first), norm_name(full)} or a == f"preferred name is {norm_name(first)}"


def review_flags(answers: dict) -> list[str]:
    flags = []
    for q, a in answers.items():
        if a.get("truncated"):
            flags.append(f"SKIPPED (incomplete capture): {q[:40]}...")
            continue
        if a.get("skipped_by_guard"):
            flags.append(f"SKIPPED (guard refused to compose): {q[:40]}...")
            continue
        if a.get("trimmed_lead"):
            flags.append(f'trimmed leading "{a["trimmed_lead"]}" from: {q[:35]}...')
        for typo, fix in REVIEW_TYPOS.items():
            if f" {typo} " in f" {clean(a['text']).lower()} ":
                flags.append(f'possible typo in "{q[:30]}...": {typo} -> {fix}?')
    return flags


def find_answer(answers: dict, prefix: str) -> dict | None:
    return next((a for q, a in answers.items() if q.lower().startswith(prefix)), None)


def compose_person(p: dict, heritage_on: bool = True) -> tuple[list[str], list[str]]:
    """-> (lines, review_flags) for one person."""
    answers = p.get("answers", {})
    name_line = f"\U0001f44b @{p['display_name']}"

    # Preferred name: single word -> folded into the name line as (Nickname);
    # prose answers stay a quoted fact line; redundant answers are skipped.
    pref_fact = None
    pref = find_answer(answers, "do you have a preferred name")
    if pref and not pref.get("truncated"):
        text, removed = trim_lead(clean(pref["text"]))
        if removed:
            pref["trimmed_lead"] = removed
        if not skip_preferred(text, p["first_name"], p["display_name"]):
            word = text.strip(" .!\"")
            if " " not in word and 0 < len(word) <= 14:
                name_line += f" ({word})"
            else:
                pref_fact = text

    lines = [name_line]
    sub = p["job_title"]
    if p.get("pronouns"):
        sub += f" · {p['pronouns'].lower()}"
    lines.append(sub)
    if pref_fact is not None:
        try:
            lines += compose_fact("\U0001f4ac", pref_fact)
        except ComposerError:
            pref["skipped_by_guard"] = True

    # Select facts: pinned questions always make the cut, the rest fill
    # remaining slots in SAFE_QUESTIONS priority order; display order stays
    # the SAFE_QUESTIONS order either way.
    pinned = [pp.lower() for pp in p.get("pin", [])]
    candidates = []
    for prefix, emoji in SAFE_QUESTIONS[1:]:
        if prefix.startswith("tell us") and not heritage_on and prefix not in pinned:
            continue
        match = find_answer(answers, prefix)
        if not match or match.get("truncated"):
            continue
        candidates.append((prefix, emoji, match))
    if len(candidates) > MAX_FACTS:
        keep_idx = [i for i, c in enumerate(candidates) if c[0] in pinned]
        for i in range(len(candidates)):
            if len(keep_idx) >= MAX_FACTS:
                break
            if i not in keep_idx:
                keep_idx.append(i)
        candidates = [candidates[i] for i in sorted(keep_idx)]

    for prefix, emoji, match in candidates:
        text, removed = trim_lead(clean(match["text"]))
        if removed:
            match["trimmed_lead"] = removed
        try:
            lines += compose_fact(emoji, text)
        except ComposerError:
            match["skipped_by_guard"] = True

    if not answers:
        lines.append("New to the garden — say hi! \U0001f331")
    return lines, review_flags(answers)


OPENERS = {
    1: "A new amigo just joined — welcome them!! \U0001f389",
    2: "Two new amigos just joined — welcome them!! \U0001f389",
    3: "Three new amigos just joined — welcome them!! \U0001f389",
    4: "Four new amigos just joined — welcome them!! \U0001f389",
}


def compose_batch(people: list[dict], heritage_on: bool = True) -> tuple[str, list[str]]:
    n = len(people)
    opener = OPENERS.get(n, f"{n} new amigos just joined — welcome them!! \U0001f389")
    blocks, all_flags = [], []
    for p in people:
        lines, flags = compose_person(p, heritage_on)
        blocks.append("\n".join(lines))
        all_flags += [f"{p['first_name']}: {f}" for f in flags]
    text = f"\U0001f389 *{opener}*\n\n" + "\n\n".join(blocks) + "\n\nSay hi below \U0001f44b"
    for line in text.splitlines():
        if len(line) > MAX_LINE + 4 and not line.startswith("\U0001f389"):
            raise ComposerError(f"line too long for mobile: {line!r}")
    return text, all_flags


def main() -> None:
    with open(sys.argv[1] if len(sys.argv) > 1 else "welcome_queue.json") as f:
        queue = json.load(f)
    for i, batch in enumerate(queue["batches"], 1):
        people = [queue["people"][k] for k in batch]
        text, flags = compose_batch(people, queue.get("heritage_on", True))
        print(f"{'=' * 44}\nBATCH {i} — {', '.join(batch)}\n{'=' * 44}")
        print(text)
        if flags:
            print("\n-- review flags --")
            for fl in flags:
                print(f"  ⚠ {fl}")
        print()


if __name__ == "__main__":
    main()
