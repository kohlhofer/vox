---
name: vox-voice-alerts
description: Speak a short spoken alert to the user via the `vox` text-to-speech CLI when a long-running task finishes, when you need their input/decision/approval, or when something broke — so they hear it even if they're not watching the terminal. Use instead of silently printing "done" or "I need input" when the user may be away. Requires `vox` on PATH; no-op if it isn't installed.
---

# Voice alerts with vox

`vox` reads text aloud on the user's Mac. Use it as a tap on the shoulder when
something needs their attention and they may not be looking at the terminal.

## Speak — and basically only — when

- A long-running task the user is waiting on **finishes** (passed or failed).
- You're **blocked** and need their input, a decision, or approval to continue.
- Something **broke** that they'd want to know about now rather than later.

Stay silent for routine progress, ordinary end-of-turn replies, and anything
they're already watching. Err toward silence — overusing it makes it noise.

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
