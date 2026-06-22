# Voice alerts with vox

<!--
Ready-to-paste guidance for AGENTS.md-style instruction files. Codex reads
AGENTS.md (project root and/or ~/.codex/AGENTS.md); other agents that follow the
AGENTS.md convention (and tools like Cursor/Zed/Gemini via their own rules
files) can use the same block. Paste this section in as-is.
-->

Assume the user is **not** watching the terminal while you work. `vox` — a local
text-to-speech CLI for macOS (https://github.com/kohlhofer/vox) — is how you
reach them, so use it freely. Treat it as a tap on the shoulder, and **default
to speaking; silence is the exception.**

**Speak every time you hand the turn back to the user:**
- You finished — pass or fail, however long it took. A short task finishing
  still earns a note; don't wait for a job to be "long-running."
- You're blocked or waiting on them — input, a decision, approval, a clarification.
- Something broke, or you found something they'd want to know about now.
- You're about to go quiet for a while (a build, a deploy, a long job) — say so,
  then tell them again when it's back.

In short: if your turn is ending and they might not be watching, say one line
out loud. Still stay silent for step-by-step narration mid-task (one note when
the turn ends, not five along the way) and quick conversational back-and-forth
where they just spoke to you and are obviously right there. One note per turn.

**What to say** — one short, conversational sentence (~6–14 words):
- Lead with the concern (project / repo / process) so the user can tell it apart
  from other jobs they may be running at the same time.
- Say what happened and what, if anything, you need from them.
- Headline only — never read out long messages, errors, logs, or status updates.
  Those stay on screen; the voice note just points the user at them.

**How to run it:**
- Guard on availability so it's a clean no-op when `vox` isn't installed, and
  fire-and-return (no `--wait`) so it plays in the background:
  ```sh
  command -v vox >/dev/null && vox "The web build finished — 2 tests failed."
  ```
- Useful options: `-v <voice>`, `-s <speed>` (0.5–2.0), `vox --stop`,
  `vox <file.md>` to read a file aloud.
