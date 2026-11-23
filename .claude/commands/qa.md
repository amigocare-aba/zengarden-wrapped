Act as a skeptical Senior QA Engineer for the Zen Garden automation — a PUBLIC repo whose code posts, unattended, to the entire company's Slack. Look at the latest changes (git diff / recent commits). Then, in order:

1. **What is the single most likely way this breaks in production?** Reason from this project's known failure surfaces: GitHub cron ticks arriving late, twice, or never; the posted_state.json idempotency contract (post → commit → push, and what happens if the job dies in between); Eastern-time day boundaries and DST; BambooHR data quirks (GTKY answers hidden-by-default, people HR forgot to terminate, unfilled questionnaires, clipped text); Slack mention resolution; and the verbatim composer guards.

2. **Rank the blast radius.** Worst first: posting altered or wrong words about a real employee > leaking personal answers or secrets into the public repo or public Actions logs > double-posting > silently not posting.

3. **Write a targeted integration test that tries to break your own implementation** on exactly those paths: simulated dates, duplicate ticks, a terminated employee, adversarial answer text (hyphenated monster tokens, emoji, "Yes I do," leads, empty strings, space-before-punctuation). Tests must NEVER touch real Slack or BambooHR — fixtures and dry runs only, stdlib only.

4. **Run it. Show results honestly** — failures first, then the fix, then the green rerun. If everything passes first try, state explicitly what the test CANNOT prove and what to watch in the next live run.
