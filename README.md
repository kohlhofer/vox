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
is worth it, or it stays silent or talks over every step. The
[`integrations/`](integrations/) directory has ready-to-paste guidance that
encodes that rule (speak whenever you hand the turn back — done, blocked, broke,
or about to go quiet; one short sentence; lead with which job; headline only,
never read out logs). The behavior is the same everywhere; only the file format
differs.

**Claude Code** is the setup I use. Paste the body of
[`claude-code/CLAUDE.md.snippet`](integrations/claude-code/CLAUDE.md.snippet)
into your global `~/.claude/CLAUDE.md` (or a project `CLAUDE.md`). Putting it in
`CLAUDE.md` keeps it always in context, which fires far more reliably than a
skill that only loads when the model thinks it's relevant. There's also a [skill](integrations/claude-code/skills/vox/) if
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

vox can also speak in cloned voices — including your own. See
[Your own voice](#your-own-voice) below.

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

## Your own voice

vox can speak in *your* voice, cloned from a short recording. It's opt-in: until
you add a voice, nothing changes — no extra packages, no model download, no
extra memory. One ground rule: clone only voices whose speaker has agreed to it.

```sh
vox --add-voice me ~/clips/me-reading-a-paragraph.m4a   # once (10–20 s of clean speech)
vox -v me "The build is green. Two tests failed."        # from then on
vox --list-voices                                        # stock voices, yours, and the built-in ones
vox --remove-voice me
```

The cloning model also ships built-in voices of its own — `vox -v Ava`,
`Nathan`, … (`--list-voices` names them all). They need the same one-time setup,
which `--add-voice` does; to use only built-in voices, force it once with
`--engine clone`.

> **Heads up: cloned and built-in voices run on the CPU and cost real memory.**
> One or two cores for as long as one is talking, and about 1.4 GB held while
> the model is loaded — the stock voices take a fraction of a second on the GPU
> and 150–450 MB. A one-sentence alert is a burst you won't notice; reading a
> long document keeps the CPU busy for the whole read and will warm a fanless
> MacBook. The model unloads after 2 idle minutes (`vox --quit` frees it now;
> the next cloned-voice call pays ~2 s to reload). Use your voice for the lines
> that should sound like you, and the default voice for bulk reading.

The first setup installs a few packages into vox's own venv (~80 MB, mostly
[onnxruntime](https://onnxruntime.ai)) and downloads the
[MOSS-TTS-Nano](https://github.com/OpenMOSS/MOSS-TTS-Nano) model (~730 MB, into
`~/.cache/vox/models/`). Any audio format works if `ffmpeg` is installed
(`brew install ffmpeg`); without it, hand vox a 48 kHz WAV.

How it works: the clip is trimmed, peak-normalized (a quiet recording clones
noticeably worse), stored under `~/.config/vox/voices/`, and encoded once into
audio tokens that are prepended to every generation as a prompt. Nothing is
trained, and your voices never leave the machine. Speech streams to the speaker
while it's generated — first sound in well under a second on Apple silicon.
`--speed` doesn't apply to cloned voices (the model has no tempo control). If a
cloned voice can't load, vox uses the default Kokoro voice instead of going
silent. Tuning notes and measurements are in `vox_moss.py`.

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
  -v, --voice NAME         voice (default: af_bella; --list-voices shows all)
  -s, --speed X            speaking speed 0.5–2.0 (default: 1.1); cloned voices ignore it
  -w, --wait               block until speech finishes (default: return once queued)
  -l, --list-voices        list voices and exit
      --add-voice NAME FILE
                           clone a voice from an audio clip and exit
      --remove-voice NAME  delete a cloned voice and exit
      --stop               stop current speech and clear the queue
      --quit               shut down the background voice daemon
      --no-daemon          synthesize inline instead of using the warm daemon
      --engine {auto,kokoro,clone,say}
                           auto (default) picks by voice; the rest force an engine
  -q, --quiet              suppress status messages
```

By default `vox` returns as soon as the text is queued, so an agent can say
"I need your input" and immediately go back to waiting for you. Use `--wait`
when you need the call to block until the words have been spoken.

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
- Runtime state (socket, log) lives under `~/.cache/vox/`; cloned voices under
  `~/.config/vox/voices/` (override with `VOX_VOICES_DIR`); the cloning model
  under `~/.cache/vox/models/` (`VOX_MODELS_DIR`).
- `VOX_MOSS_IDLE=<seconds>` tunes how long the cloning model stays loaded while
  idle (default 120).

## License

[MIT](LICENSE) © Alexander Kohlhofer. The Kokoro-82M model and its voices are
distributed under their own licenses — see
[hexgrad/Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M). Voice cloning
uses MOSS-TTS-Nano (Apache-2.0); its ONNX runtime is vendored under
`vendor/moss_tts_nano/` — see the `NOTICE.md` there.
