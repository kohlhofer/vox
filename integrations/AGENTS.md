# Voice alerts with vox

<!--
Ready-to-paste guidance for AGENTS.md-style instruction files. Codex reads
AGENTS.md (project root and/or ~/.codex/AGENTS.md); other agents that follow the
AGENTS.md convention (and tools like Cursor/Zed/Gemini via their own rules
files) can use the same block. Paste this section in as-is.
-->

When something needs the user's attention and they may not be watching the
terminal, speak it aloud with `vox` — a local text-to-speech CLI for macOS
(https://github.com/kohlhofer/vox). Treat it as a tap on the shoulder.

**Speak — and basically only — when:**
- A long-running task the user is waiting on finishes (passed or failed).
- You're blocked and need their input, a decision, or approval to continue.
- Something broke that they'd want to know about now rather than later.

Stay silent for routine progress, ordinary replies, and anything they're already
watching. Overusing it turns a useful signal into noise.

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
