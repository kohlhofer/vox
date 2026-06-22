# Integrations — let your AI agent use vox

Drop-in pieces so a coding agent will speak up through `vox` when a long job
finishes or it needs your input — instead of you having to watch the terminal.

The behavioral guidance is the same everywhere (assume the user isn't watching
the terminal, so speak up whenever you hand the turn back — done, blocked, broke,
or about to go quiet; one short sentence; lead with which job it's about;
headline only, never read out long output). Only the file format differs per tool.

> Prerequisite: install vox first (see the [main README](../README.md)) so the
> `vox` command is on your PATH. Every integration guards on `command -v vox`,
> so it's a harmless no-op on machines where vox isn't installed.

## Claude Code

Two options — pick one:

- **Skill** (`claude-code/skills/vox/`) — loaded when the model judges it
  relevant. Install by copying or symlinking it into your skills dir:
  ```sh
  ln -s "$PWD/claude-code/skills/vox" ~/.claude/skills/vox        # user-wide
  # or, per project:  ln -s "$PWD/claude-code/skills/vox" <project>/.claude/skills/vox
  ```
- **Always-on instruction** (`claude-code/CLAUDE.md.snippet`) — more reliable
  for proactive "alert me" behavior since it's always in context. Paste its body
  into `~/.claude/CLAUDE.md` (or a project `CLAUDE.md`).

## Codex & other AGENTS.md tools

`AGENTS.md` is the cross-tool instruction convention used by Codex (it reads
`AGENTS.md` in the repo root and `~/.codex/AGENTS.md`) and a growing set of other
agents. Paste the `## Voice alerts with vox` section from
[`AGENTS.md`](AGENTS.md) into your project's `AGENTS.md` (or the global one).

## Cursor / Zed / Gemini CLI / others

These read their own rules files (`.cursor/rules/*.mdc`, `GEMINI.md`, etc.) but
the content is identical — paste the body of [`AGENTS.md`](AGENTS.md) into
whichever instruction file your tool uses. Check your tool's docs for the exact
path.

## Testing it

After wiring one up, ask your agent to "run a quick task and tell me out loud
when it's done." You should hear a short spoken note. Tune voice and speed with
`vox -v <voice> -s <speed>`; `vox --list-voices` shows the options.
