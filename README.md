# vox

Read text out loud with a good neural voice, from anywhere on your Mac.

```sh
vox "Build finished — all green."
vox -v am_onyx -s 0.95 "Heads up, I need your input on the migration."
echo "piped text works too" | vox
vox --stop          # cut off whatever is talking
```

It exists so an AI agent (or any script) can get your attention or give you a
spoken update — something you should *hear*, not read off a screen.

## How it sounds

Voices come from [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) running
through [mlx-audio](https://github.com/Blaizzy/mlx-audio) on Apple Silicon. The
default is `af_bella`. If Kokoro can't load (Intel Mac, missing deps, whatever),
it falls back to the built-in macOS `say` voice — lower quality, but it never
goes silent.

```sh
vox --list-voices
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

## Install

```sh
git clone <this repo> vox && cd vox
./install.sh
```

That builds a Python venv, installs the Kokoro deps, and drops a `vox` launcher
in `~/.local/bin`. If that's not on your PATH, the script tells you. Set
`VOX_BIN_DIR=/usr/local/bin ./install.sh` to put the launcher elsewhere.

First run downloads the model (~160MB) and is slow. After that it's fast.

## Why it's fast (the daemon)

The first `vox` call starts a small background daemon that keeps the voice model
warmed up. Later calls just hand it text over a local socket, so they're
near-instant and never talk over each other — everything plays through one
queue. The daemon shuts itself down after 10 minutes idle.

You rarely need to think about it. If you want to:

- `vox --no-daemon "…"` — synthesize inline, no daemon.
- `vox --stop` — interrupt current speech and clear the queue.
- Daemon log lives at `~/.cache/vox/daemon.log`.

## Options

```
vox [text...]              text to speak ('-' or a pipe reads stdin)
  -v, --voice ID           voice id (default: af_bella)
  -s, --speed X            speaking speed 0.5–2.0 (default: 1.1)
  -w, --wait               block until speech finishes (default: return once queued)
  -l, --list-voices        list voices and exit
      --stop               stop current speech and clear the queue
      --no-daemon          synthesize inline instead of using the warm daemon
      --engine {auto,kokoro,say}
  -q, --quiet              suppress status messages
```

By default `vox` returns as soon as the text is queued, so an agent can say
"I need your input" and immediately go back to waiting for you. Use `--wait`
when you need the call to block until the words have actually been spoken.

## Using it from an AI agent

Any agent that can run a shell command can use it:

```sh
vox "I've finished the refactor and I need you to review it."
```

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
