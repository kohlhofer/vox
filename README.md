# vox

`vox` is how your AI coding agent gets your attention: it says one short line out
loud when a job finishes, when it's blocked on you, or when something broke — so
you can stop babysitting the terminal and look back only when there's a reason to.

```sh
command -v vox >/dev/null && vox "The web build finished — 2 tests failed."
```

That one guarded line is the whole pattern. Any agent that can run a shell
command can call `vox`, and the `command -v` guard makes it a clean no-op on
machines where vox isn't installed. The voice is a good neural one
([Kokoro](https://huggingface.co/hexgrad/Kokoro-82M)), so it's pleasant to leave
running in the background while you work on something else.

It's also a plain text-to-speech CLI — pipe it text, point it at a file — but
voice alerts for agents are what it's for, and the rest of this README is built
around that.

## Set it up for your agent

Two steps: install vox, then teach your agent *when* to speak.

### 1. Install

```sh
curl -fsSL https://raw.githubusercontent.com/kohlhofer/vox/main/bootstrap.sh | bash
```

That clones vox into `~/.local/share/vox`, builds a Python venv, installs the
Kokoro deps, installs `espeak-ng` via Homebrew (so Kokoro can pronounce
out-of-dictionary words instead of skipping them or dropping to the macOS `say`
voice), and drops a `vox` launcher in `~/.local/bin`. If that's not on your PATH,
the script tells you. Re-run the same command anytime to update to the latest.

Rather inspect things first, or already have a clone? Install from source:

```sh
git clone https://github.com/kohlhofer/vox.git && cd vox
./install.sh
```

Put the launcher somewhere other than `~/.local/bin` with `VOX_BIN_DIR=/usr/local/bin`
(works with either path); change the checkout location with `VOX_HOME`.

First run downloads the model (~160MB) and is slow. After that it's fast.

### 2. Tell your agent when to speak

The command alone isn't enough — the agent needs a rule for *when* a spoken note
is worth it, or it either stays silent or talks over every step. The
[`integrations/`](integrations/) directory has ready-to-paste guidance that
encodes that rule (speak whenever you hand the turn back — done, blocked, broke,
or about to go quiet; one short sentence; lead with which job; headline only,
never read out logs). The behavior is the same everywhere; only the file format
differs.

**Claude Code** is the setup I use. Paste the body of
[`claude-code/CLAUDE.md.snippet`](integrations/claude-code/CLAUDE.md.snippet)
into your global `~/.claude/CLAUDE.md` (or a project `CLAUDE.md`). Putting it in
`CLAUDE.md` keeps it always in context, which fires far more reliably for
proactive "alert me" behavior than a skill that only loads when the model thinks
it's relevant. There's also a [skill](integrations/claude-code/skills/vox/) if
you'd rather install it that way:

```sh
ln -s "$PWD/integrations/claude-code/skills/vox" ~/.claude/skills/vox
```

**Codex and other AGENTS.md tools** — paste the `## Voice alerts with vox`
section from [`integrations/AGENTS.md`](integrations/AGENTS.md) into your repo or
`~/.codex/AGENTS.md`.

**Cursor / Zed / Gemini CLI / others** — the same text works; drop it into
whichever rules file your tool reads (`.cursor/rules/*.mdc`, `GEMINI.md`, etc.).

Then ask your agent to "run a quick task and tell me out loud when it's done."
You should hear one short spoken note.

## Voices

The default is `af_bella`. If Kokoro can't load (Intel Mac, missing deps,
whatever), vox falls back to the built-in macOS `say` voice — lower quality, but
it never goes silent on you.

```sh
vox --list-voices
vox -v am_onyx -s 0.95 "Heads up, I need your input on the migration."
```

| voice | |
|---|---|
| `af_bella` | female, American, expressive — **default** |
| `af_heart` | female, American, warm |
| `af_nicole` | female, American, soft |
| `af_sky` | female, American, bright |
| `am_michael` | male, American |
| `am_adam` | male, American |
| `am_onyx` | male, American, deep |
| `am_puck` | male, American, playful |

## Why it's fast (the daemon)

The first `vox` call starts a small background daemon that keeps the voice model
warmed up. Later calls just hand it text over a local socket, so they're
near-instant and never talk over each other — everything plays through one
queue. The daemon shuts itself down after 10 minutes idle.

You rarely need to think about it. If you want to:

- `vox --no-daemon "…"` — synthesize inline, no daemon.
- `vox --stop` — interrupt current speech and clear the queue (daemon stays warm).
- `vox --quit` — shut the daemon down now and free the model from memory.
- Daemon log lives at `~/.cache/vox/daemon.log`.

## Reading files

Point vox at a file and it reads it aloud, stripping Markdown first (frontmatter,
headings, list markers, links, emphasis) so it doesn't narrate `#` and URLs:

```sh
vox README.md
vox -f ~/notes/standup.md
echo "piped text works too" | vox
```

Long text is split into sentence-sized pieces and synthesized one ahead of
playback, so it starts within a second or two and pauses naturally between
paragraphs. `vox --stop` halts it mid-read.

## Options

```
vox [text...]              text to speak; a file path is read aloud ('-'/pipe = stdin)
  -f, --file PATH          read this file aloud (Markdown stripped)
  -v, --voice ID           voice id (default: af_bella)
  -s, --speed X            speaking speed 0.5–2.0 (default: 1.1)
  -w, --wait               block until speech finishes (default: return once queued)
  -l, --list-voices        list voices and exit
      --stop               stop current speech and clear the queue
      --quit               shut down the background voice daemon
      --no-daemon          synthesize inline instead of using the warm daemon
      --engine {auto,kokoro,say}
  -q, --quiet              suppress status messages
```

By default `vox` returns as soon as the text is queued, so an agent can say
"I need your input" and immediately go back to waiting for you. Use `--wait`
when you need the call to block until the words have actually been spoken.

## MCP server

For MCP-native agents there's an optional server (`vox_mcp.py`) exposing
`speak_text`, `stop`, and `list_voices` tools over the same engine:

```sh
./.venv/bin/python -m pip install mcp
```

Then register it (stdio transport), e.g. in `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "vox": {
      "command": "/absolute/path/vox/.venv/bin/python",
      "args": ["/absolute/path/vox/vox_mcp.py"]
    }
  }
}
```

## Notes

- The command is `vox` because `speak` is already taken by espeak-ng on many
  machines. Mac only for now: Apple Silicon for the good voices, any Mac for the
  `say` fallback.
- `SPEAK_SAY_VOICE=Daniel vox --engine say "…"` picks a system voice for the
  fallback path.
- Runtime state (socket, log) lives under `~/.cache/vox/`.

## License

[MIT](LICENSE) © Alexander Kohlhofer. The Kokoro-82M model and its voices are
distributed under their own licenses — see
[hexgrad/Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M).
