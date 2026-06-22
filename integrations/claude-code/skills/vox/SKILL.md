---
name: vox-voice-alerts
description: Speak a short spoken alert to the user via the `vox` text-to-speech CLI whenever you hand the turn back to them — a task finished (pass or fail, however long it took), you're blocked and need input/a decision/approval, something broke, or you're about to go quiet on a long job. Assume the user is NOT watching the terminal, so speaking is the default and silence the exception. Use instead of silently printing "done" or "I need input". Requires `vox` on PATH; no-op if it isn't installed.
---

# Voice alerts with vox

`vox` reads text aloud on the user's Mac. **Assume the user is not looking at the
terminal while you work** — `vox` is how you reach them, so use it freely.
Default to speaking; silence is the exception, not the rule.

## Speak every time you hand the turn back

That's the moment the user needs to know to look:

- You **finished** — pass or fail, however long it took. A two-minute task
  finishing still earns a tap on the shoulder; don't wait for "long-running."
- You're **blocked** or waiting on them — input, a decision, approval, a
  clarification.
- Something **broke**, or you found something they'd want to know now.
- You're about to **go quiet** for a while (a build, a deploy, a long job) — say
  so, then tell them again when it's back.

In short: if your turn is ending and they might not be watching, say one line
out loud — anywhere from a plain "done" to "here's what I need from you."

**Still stay silent for:** step-by-step narration mid-task (one note when the
turn ends, not five along the way) and quick conversational back-and-forth where
they just spoke to you and are obviously right there. One note per turn — if two
things matter, combine them into one sentence rather than firing twice.

## What to say — one short, conversational sentence (~6–14 words)

- **Lead with the concern** (project / repo / process) so they can tell it apart
  from other jobs they may be running at the same time.
- Say what happened and what, if anything, you need from them.
- **Headline only.** Never read out long messages, summaries, errors, logs, or
  status — those stay on screen. The voice note just points them at it.

Good:
- `vox "The web build finished — all tests green."`
- `vox "Heads up — the staging migration failed, want me to retry?"`
- `vox "I need your call on the auth approach before I continue."`
- `vox "Starting the deploy — I'll tell you when it's live."`

Too much: reading a full error trace, a paragraph of summary, or every step.

## How to run it

- Guard on availability so it's a clean no-op when `vox` isn't installed:
  ```sh
  command -v vox >/dev/null && vox "The web build finished — 2 tests failed."
  ```
- Fire-and-return — do **not** pass `--wait`; it plays in the background while
  you keep working.
- Options you may want: `-v <voice>` (e.g. `am_onyx`), `-s <speed>` (0.5–2.0),
  `vox --stop` to cut off speech, `vox <file.md>` to read a file aloud.

Install vox itself from https://github.com/kohlhofer/vox if it isn't present.
