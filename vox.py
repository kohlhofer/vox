#!/usr/bin/env python3
"""vox — read text out loud with a good neural voice, from anywhere.

A tiny CLI so an AI agent (or any script) can grab your attention or give you a
spoken update:  vox "I need your input on the migration"

Mac only, for now. Quality comes from Kokoro-82M (the same voices local-voice
uses; default `af_bella`) running through mlx-audio on Apple Silicon. Anywhere
that isn't available, it falls back to the built-in macOS `say` voice so it
still works — quality drops, but it never goes silent.

A small daemon starts itself on first use and keeps the model warm, so repeat
calls are near-instant and never talk over each other (everything goes through
one playback queue). Pass --no-daemon to synthesize inline instead.
"""

from __future__ import annotations

import argparse
import atexit
import fcntl
import json
import os
import re
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import wave
from queue import Queue, Empty, Full

# --------------------------------------------------------------------------- #
# Configuration                                                               #
# --------------------------------------------------------------------------- #

KOKORO_REPO = "mlx-community/Kokoro-82M-bf16"   # MLX Kokoro-82M

# Voice id -> human description. American English; Kokoro's better-sounding set.
VOICES = {
    "af_bella":   "female, American, expressive  (default)",
    "af_heart":   "female, American, warm",
    "af_nicole":  "female, American, soft",
    "af_sky":     "female, American, bright",
    "am_michael": "male, American",
    "am_adam":    "male, American",
    "am_onyx":    "male, American, deep",
    "am_puck":    "male, American, playful",
}
DEFAULT_VOICE = "af_bella"
DEFAULT_SPEED = 1.1          # 1.0 = natural pace; >1 = snappier
SPEED_MIN, SPEED_MAX = 0.5, 2.0

SAY_BASE_WPM = 175           # macOS `say` baseline; scaled by speed for fallback

IDLE_TIMEOUT = 600.0         # daemon exits after this many idle seconds
STARTUP_TIMEOUT = 90.0       # client waits this long for a cold daemon (model dl)

_RUNTIME = os.environ.get("XDG_RUNTIME_DIR") or os.path.expanduser("~/.cache/vox")
SOCK_PATH = os.path.join(_RUNTIME, "vox.sock")
LOCK_PATH = os.path.join(_RUNTIME, "vox.lock")
LOG_PATH = os.path.join(_RUNTIME, "daemon.log")


def _eprint(msg: str, quiet: bool = False) -> None:
    """Status to stderr — stdout stays clean for callers that capture it."""
    if not quiet:
        print(msg, file=sys.stderr, flush=True)


def clamp_speed(s: float) -> float:
    return max(SPEED_MIN, min(SPEED_MAX, s))


# --------------------------------------------------------------------------- #
# Engine: synthesis + playback                                                #
# --------------------------------------------------------------------------- #

class Engine:
    """Turns text into audio and plays it. Prefers Kokoro; degrades to `say`.

    `engine` is one of: "auto" (Kokoro if it loads, else say), "kokoro", "say".
    The Kokoro model is loaded lazily on first use and cached; if it ever fails
    to load we remember that and use `say` for the rest of the process.
    """

    # mlx-audio silently truncates Kokoro output past ~13.8s (its ~510-token
    # limit), so we synthesize a sentence or so at a time and keep each piece
    # well under that — ~180 chars leaves comfortable margin.
    MAX_CHUNK_CHARS = 180

    def __init__(self, engine: str = "auto", quiet: bool = False):
        self.mode = engine
        self.quiet = quiet
        self._tts = None
        self._np = None
        self._kokoro_broken = (engine == "say")   # only a *load* failure latches this
        self._play_lock = threading.Lock()
        self._current: subprocess.Popen | None = None
        self._interrupt = threading.Event()

    # -- Kokoro loading ----------------------------------------------------- #

    def _ensure_loaded(self) -> bool:
        """Load Kokoro once; return True if it's usable. A load failure latches
        the fallback (no point retrying a broken install every call). A failure
        to synthesize one piece of text does NOT — that's handled per chunk."""
        if self._tts is not None:
            return True
        if self._kokoro_broken:
            return False
        try:
            _eprint("vox: warming up the voice (first run downloads ~160MB)…", self.quiet)
            import numpy as np                              # noqa: WPS433
            from mlx_audio.tts.utils import load_model       # noqa: WPS433
            self._np = np
            self._tts = load_model(KOKORO_REPO)
            return True
        except Exception as exc:                              # noqa: BLE001
            self._kokoro_broken = True
            _eprint(f"vox: neural voice unavailable ({exc}); using the system voice.", self.quiet)
            return False

    @property
    def name(self) -> str:
        if self._tts is not None:
            return "kokoro"
        if self._kokoro_broken:
            return "say"
        return "kokoro?" if self.mode != "say" else "say"

    # -- Text chunking ------------------------------------------------------ #

    @classmethod
    def _chunk_text(cls, text: str):
        """Break text into synthesis-sized pieces. Paragraphs (blank-line
        separated) are kept apart so each ends with a natural pause; within a
        paragraph we split on sentence boundaries, sub-split any over-long
        sentence on clause punctuation, then hard-wrap on spaces as a fallback."""
        cap = cls.MAX_CHUNK_CHARS
        out = []
        for para in re.split(r"\n\s*\n", text):
            para = " ".join(para.split())
            if not para:
                continue
            for sent in re.split(r"(?<=[.!?…])\s+", para):
                if not sent:
                    continue
                if len(sent) <= cap:
                    out.append(sent)
                    continue
                buf = ""
                for clause in re.split(r"(?<=[;:,—–])\s+", sent):
                    while len(clause) > cap:                 # clause itself too long
                        cut = clause.rfind(" ", 0, cap)
                        cut = cut if cut > 0 else cap
                        out.append(clause[:cut].strip())
                        clause = clause[cut:].strip()
                    if not buf:
                        buf = clause
                    elif len(buf) + 1 + len(clause) <= cap:
                        buf += " " + clause
                    else:
                        out.append(buf)
                        buf = clause
                if buf:
                    out.append(buf)
        return [c for c in out if c]

    # -- Synthesis ---------------------------------------------------------- #

    # mlx-audio's Kokoro vocoder has a content-dependent off-by-one-frame bug:
    # for certain phoneme->duration alignments an internal op raises a broadcast
    # error. A small change in tempo shifts the alignment and dodges it, so on
    # that specific failure we retry at nearby speeds before giving up a chunk.
    _SPEED_NUDGES = (1.0, 1.07, 0.93, 1.15, 0.86, 1.22)

    def _synth_once(self, text: str, voice: str, speed: float):
        """One Kokoro pass -> (float32 mono samples, sample_rate)."""
        np = self._np
        parts, sr = [], 24_000
        for r in self._tts.generate(text=text, voice=voice, speed=speed, lang_code="a"):
            parts.append(np.asarray(r.audio, dtype=np.float32))
            sr = r.sample_rate
        audio = np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)
        return audio, sr

    def _synth_kokoro(self, text: str, voice: str, speed: float):
        """Synthesize a chunk, retrying at nearby tempos to dodge the alignment
        bug. Raises only if every attempt fails (then the caller uses `say`)."""
        last = None
        for mult in self._SPEED_NUDGES:
            try:
                return self._synth_once(text, voice, clamp_speed(speed * mult))
            except Exception as exc:                          # noqa: BLE001
                if "broadcast" not in str(exc).lower():
                    raise                                     # unrelated failure
                last = exc
        raise last

    # -- Playback ----------------------------------------------------------- #

    def _write_wav(self, samples, sr: int) -> str:
        """float32 [-1,1] mono -> a temp 16-bit PCM wav, returns the path."""
        np = self._np
        clipped = np.clip(samples, -1.0, 1.0)
        pcm = (clipped * 32767.0).astype("<i2").tobytes()
        fd, path = tempfile.mkstemp(suffix=".wav", prefix="vox_")
        os.close(fd)
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(int(sr))
            w.writeframes(pcm)
        return path

    def _afplay(self, path: str) -> None:
        """Play a wav via afplay, tracking the process so stop() can kill it."""
        with self._play_lock:
            self._current = subprocess.Popen(
                ["afplay", path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            proc = self._current
        try:
            proc.wait()
        finally:
            with self._play_lock:
                if self._current is proc:
                    self._current = None

    def _say(self, text: str, speed: float) -> None:
        rate = max(90, int(SAY_BASE_WPM * speed))
        cmd = ["say", "-r", str(rate)]
        v = os.environ.get("SPEAK_SAY_VOICE")
        if v:
            cmd += ["-v", v]
        with self._play_lock:
            self._current = subprocess.Popen(cmd + [text])
            proc = self._current
        try:
            proc.wait()
        finally:
            with self._play_lock:
                if self._current is proc:
                    self._current = None

    def stop_current(self) -> None:
        """Interrupt speech right now: signal any in-flight speak() to bail and
        kill the playing process (no-op if silent)."""
        self._interrupt.set()
        with self._play_lock:
            if self._current and self._current.poll() is None:
                self._current.terminate()

    # -- Public ------------------------------------------------------------- #

    def speak(self, text: str, voice: str, speed: float) -> None:
        """Speak `text`, blocking until it finishes. Long text is chunked and
        synthesized one piece ahead while the previous piece plays, so there's
        no long pause between sentences. A chunk that Kokoro can't synthesize
        falls back to `say` for that chunk only — it never poisons the rest.
        """
        text = text.strip()
        if not text:
            return
        speed = clamp_speed(speed)
        self._interrupt.clear()
        chunks = self._chunk_text(text)
        use_kokoro = self.mode != "say" and self._ensure_loaded()

        if not use_kokoro:
            for chunk in chunks:
                if self._interrupt.is_set():
                    return
                self._say(chunk, speed)
            return

        # Pipeline: a producer thread synthesizes ahead into a small bounded
        # queue (so we never run far ahead of playback or block on interrupt)
        # while this thread plays each piece in order.
        ready: "Queue[tuple[str, str]]" = Queue(maxsize=2)

        def producer():
            for chunk in chunks:
                if self._interrupt.is_set():
                    break
                item = None
                try:
                    samples, sr = self._synth_kokoro(chunk, voice, speed)
                    if samples.size:
                        item = ("wav", self._write_wav(samples, sr))
                except Exception as exc:                      # noqa: BLE001
                    _eprint(f"vox: system voice for one part ({exc}).", self.quiet)
                if item is None:
                    item = ("say", chunk)
                # put with timeout so an interrupt can't deadlock us on a full queue
                while not self._interrupt.is_set():
                    try:
                        ready.put(item, timeout=0.2)
                        break
                    except Full:
                        continue
                else:
                    if item[0] == "wav":
                        _safe_unlink(item[1])
                    break
            ready.put(("end", ""))

        threading.Thread(target=producer, daemon=True).start()
        try:
            while not self._interrupt.is_set():
                kind, payload = ready.get()
                if kind == "end":
                    break
                if kind == "wav":
                    try:
                        self._afplay(payload)
                    finally:
                        _safe_unlink(payload)
                else:
                    self._say(payload, speed)
        finally:
            self._drain_ready(ready)

    @staticmethod
    def _drain_ready(ready: "Queue[tuple[str, str]]") -> None:
        """On interrupt, empty the queue and delete any pre-synthesized wavs so
        the producer unblocks and no temp files leak."""
        while True:
            try:
                kind, payload = ready.get_nowait()
            except Empty:
                return
            if kind == "wav":
                _safe_unlink(payload)


# --------------------------------------------------------------------------- #
# Daemon: one warm engine, one playback queue                                 #
# --------------------------------------------------------------------------- #

class _Job:
    __slots__ = ("text", "voice", "speed", "done")

    def __init__(self, text, voice, speed):
        self.text = text
        self.voice = voice
        self.speed = speed
        self.done = threading.Event()


class Daemon:
    """Holds the engine, serializes playback through a single worker, and exits
    on its own once idle. The accept loop only enqueues (cheap), so it stays
    responsive while audio plays in the worker thread."""

    def __init__(self, engine: str = "auto"):
        self.engine = Engine(engine=engine, quiet=False)
        self.jobs: "Queue[_Job | None]" = Queue()
        self.last_active = time.monotonic()
        self.server = None                      # set by run_daemon; used by `quit`
        threading.Thread(target=self._worker, daemon=True).start()
        threading.Thread(target=self._idle_watch, daemon=True).start()

    def _touch(self):
        self.last_active = time.monotonic()

    def _worker(self):
        while True:
            job = self.jobs.get()
            if job is None:
                return
            try:
                self.engine.speak(job.text, job.voice, job.speed)
            except Exception:                                 # noqa: BLE001
                pass
            finally:
                job.done.set()
                self._touch()

    def _idle_watch(self):
        while True:
            time.sleep(15.0)
            idle = time.monotonic() - self.last_active
            if idle > IDLE_TIMEOUT and self.jobs.empty() and self.engine._current is None:
                _safe_unlink(SOCK_PATH)     # os._exit skips atexit; clean up first
                os._exit(0)

    # -- Request handling --------------------------------------------------- #

    def handle(self, req: dict) -> dict:
        cmd = req.get("cmd")
        self._touch()
        if cmd == "ping":
            return {"ok": True, "pid": os.getpid(), "engine": self.engine.name}
        if cmd == "stop":
            self._flush()
            self.engine.stop_current()
            return {"ok": True}
        if cmd == "quit":
            self._flush()
            self.engine.stop_current()
            if self.server is not None:
                # shutdown() blocks until serve_forever returns, which waits on
                # this handler — so trigger it from another thread and reply now.
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            return {"ok": True}
        if cmd == "speak":
            text = (req.get("text") or "").strip()
            if not text:
                return {"ok": False, "error": "empty text"}
            job = _Job(text, req.get("voice", DEFAULT_VOICE), req.get("speed", DEFAULT_SPEED))
            self.jobs.put(job)
            if req.get("wait"):
                job.done.wait()
            return {"ok": True, "engine": self.engine.name}
        return {"ok": False, "error": f"unknown cmd {cmd!r}"}

    def _flush(self):
        while True:
            try:
                job = self.jobs.get_nowait()
            except Empty:
                break
            if job is not None:
                job.done.set()


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            line = self.rfile.readline()
            if not line:
                return
            req = json.loads(line.decode("utf-8"))
            resp = self.server.app.handle(req)              # type: ignore[attr-defined]
        except Exception as exc:                              # noqa: BLE001
            resp = {"ok": False, "error": str(exc)}
        try:
            self.wfile.write((json.dumps(resp) + "\n").encode("utf-8"))
        except (BrokenPipeError, ConnectionResetError):
            pass


class _Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


def run_daemon(engine: str = "auto") -> int:
    """Bind the socket and serve. An exclusive flock guarantees a single live
    daemon even if two cold clients race to start one; holding the lock means no
    one else is serving, so we can safely clear any stale socket and bind."""
    os.makedirs(_RUNTIME, exist_ok=True)
    lock_fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(lock_fd)                  # another daemon owns it — stand down
        return 0
    try:
        _safe_unlink(SOCK_PATH)            # clear a stale socket from a crash
        server = _Server(SOCK_PATH, _Handler)
        server.app = Daemon(engine=engine)  # type: ignore[attr-defined]
        server.app.server = server          # so a `quit` request can shut us down
        atexit.register(lambda: _safe_unlink(SOCK_PATH))
        try:
            server.serve_forever(poll_interval=0.5)   # keeps --quit responsive
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
            _safe_unlink(SOCK_PATH)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
    return 0


def _safe_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


# --------------------------------------------------------------------------- #
# Client: talk to the daemon, start it if needed                              #
# --------------------------------------------------------------------------- #

def _request(req: dict, timeout: float = 5.0) -> dict | None:
    """One request/response over the socket. None if no daemon is reachable."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(SOCK_PATH)
            s.sendall((json.dumps(req) + "\n").encode("utf-8"))
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
            return json.loads(buf.decode("utf-8")) if buf else None
    except (FileNotFoundError, ConnectionRefusedError, socket.timeout, OSError):
        return None


def _ping() -> dict | None:
    return _request({"cmd": "ping"}, timeout=2.0)


def _spawn_daemon(engine: str, quiet: bool) -> bool:
    """Start the daemon detached and wait until it answers a ping."""
    os.makedirs(_RUNTIME, exist_ok=True)
    log = open(LOG_PATH, "ab")
    subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "--serve", "--engine", engine],
        stdin=subprocess.DEVNULL, stdout=log, stderr=log,
        start_new_session=True, close_fds=True,
    )
    deadline = time.monotonic() + STARTUP_TIMEOUT
    while time.monotonic() < deadline:
        if _ping() is not None:
            # Daemon is up but loads the model on its first job; set expectations
            # so a few seconds of silence before the first words isn't a surprise.
            _eprint("vox: warming up the voice — first words in a few seconds…", quiet)
            return True
        time.sleep(0.4)
    _eprint("vox: voice daemon did not come up in time; speaking inline.", quiet)
    return False


def speak_via_daemon(text, voice, speed, wait, engine, quiet) -> bool:
    if _ping() is None and not _spawn_daemon(engine, quiet):
        return False
    # On --wait the socket stays open until playback finishes; give it lots of
    # headroom so we never time out mid-sentence and re-speak inline.
    timeout = 600.0 if wait else 10.0
    resp = _request({"cmd": "speak", "text": text, "voice": voice,
                     "speed": speed, "wait": wait}, timeout=timeout)
    return bool(resp and resp.get("ok"))


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #

def clean_markdown(md: str) -> str:
    """Strip common Markdown so a file narrates cleanly rather than reading out
    '#', '*', and link URLs: drops YAML frontmatter, fenced code, headings,
    list/quote markers, and horizontal rules; unwraps links to their text and
    removes emphasis. Blank lines survive as paragraph breaks (the chunker
    turns those into natural pauses)."""
    md = re.sub(r"\A﻿?---\r?\n.*?\r?\n---\r?\n", "", md, flags=re.S)  # frontmatter
    md = re.sub(r"```.*?```", "\n", md, flags=re.S)                        # fenced code
    md = re.sub(r"~~~.*?~~~", "\n", md, flags=re.S)
    lines = []
    for line in md.splitlines():
        s = line.rstrip()
        if re.fullmatch(r"\s*[-*_]{3,}\s*", s):      # horizontal rule -> blank
            lines.append("")
            continue
        s = re.sub(r"^\s{0,3}#{1,6}\s+", "", s)       # heading marks
        s = re.sub(r"^\s*>+\s?", "", s)               # blockquote
        s = re.sub(r"^\s*[-*+]\s+", "", s)            # bullet list
        s = re.sub(r"^\s*\d+[.)]\s+", "", s)          # numbered list
        lines.append(s)
    text = "\n".join(lines)
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)   # images -> alt text
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)    # links -> link text
    text = re.sub(r"[*_`~]+", "", text)                     # emphasis / inline code
    text = re.sub(r"[ \t]+", " ", text)                     # collapse spaces (keep \n)
    text = re.sub(r"\n{3,}", "\n\n", text)                 # collapse blank-line runs
    return text.strip()


def _resolve_text(args) -> str:
    """Work out what to say. A file — given with --file, or as a single
    positional argument that happens to be an existing file — is read and
    stripped of Markdown. Otherwise stdin ('-' or a pipe) or the literal
    positional text is spoken as typed. Raises ValueError on a bad --file."""
    path = args.file
    if not path and len(args.text) == 1 and args.text[0] != "-" and os.path.isfile(args.text[0]):
        path = args.text[0]
    if path:
        if not os.path.isfile(path):
            raise ValueError(f"no such file: {path}")
        try:
            with open(path, encoding="utf-8") as f:
                return clean_markdown(f.read())
        except (OSError, UnicodeDecodeError) as exc:
            raise ValueError(f"can't read {path}: {exc}")
    if args.text == ["-"] or (not args.text and not sys.stdin.isatty()):
        return sys.stdin.read()
    return " ".join(args.text)


def _print_voices() -> None:
    print("Available voices (default: %s):\n" % DEFAULT_VOICE)
    for vid, desc in VOICES.items():
        print(f"  {vid:<11} {desc}")
    print("\nAny Mac also has the built-in `say` voices; set SPEAK_SAY_VOICE to pick one.")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="vox",
        description="Read text out loud with a good neural voice (Mac).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  vox \"Build finished — all green.\"\n"
            "  vox -v am_onyx -s 0.95 \"Heads up, I need your input.\"\n"
            "  vox notes.md           # read a file aloud (Markdown stripped)\n"
            "  echo \"piped text\" | vox\n"
            "  vox --stop             # cut off whatever is talking\n"
            "  vox --quit             # shut the background voice daemon down\n"
        ),
    )
    p.add_argument("text", nargs="*",
                   help="text to speak; a file path is read aloud ('-' or a pipe reads stdin)")
    p.add_argument("-f", "--file", metavar="PATH",
                   help="read this file aloud (Markdown is stripped before speaking)")
    p.add_argument("-v", "--voice", default=DEFAULT_VOICE, metavar="ID",
                   help=f"voice id (default: {DEFAULT_VOICE}; --list-voices to see all)")
    p.add_argument("-s", "--speed", type=float, default=DEFAULT_SPEED, metavar="X",
                   help=f"speaking speed {SPEED_MIN}–{SPEED_MAX} (default: {DEFAULT_SPEED})")
    p.add_argument("-w", "--wait", action="store_true",
                   help="block until speech finishes (default: return once queued)")
    p.add_argument("-l", "--list-voices", action="store_true", help="list voices and exit")
    p.add_argument("--stop", action="store_true", help="stop current speech and clear the queue")
    p.add_argument("--quit", action="store_true",
                   help="shut down the background voice daemon (frees the model from memory)")
    p.add_argument("--no-daemon", action="store_true",
                   help="synthesize inline instead of using the warm daemon")
    p.add_argument("--engine", choices=["auto", "kokoro", "say"], default="auto",
                   help="auto (default) = Kokoro if available, else system voice")
    p.add_argument("-q", "--quiet", action="store_true", help="suppress status messages")
    p.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)  # internal
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    if args.serve:
        return run_daemon(engine=args.engine)

    if args.list_voices:
        _print_voices()
        return 0

    if args.stop:
        _request({"cmd": "stop"})          # silently no-op if no daemon
        return 0

    if args.quit:
        resp = _request({"cmd": "quit"})
        _eprint("vox: daemon shut down." if resp else "vox: no daemon running.", args.quiet)
        return 0

    try:
        text = _resolve_text(args).strip()
    except ValueError as exc:
        _eprint(f"vox: {exc}", args.quiet)
        return 2
    if not text:
        _eprint("vox: nothing to say (give text, a file, pipe stdin, or use --list-voices).", args.quiet)
        return 2

    if args.voice not in VOICES and args.engine != "say":
        _eprint(f"vox: unknown voice '{args.voice}'. Try --list-voices.", args.quiet)
        return 2

    speed = clamp_speed(args.speed)
    _eprint(f"vox: \"{text[:60]}{'…' if len(text) > 60 else ''}\"", args.quiet)

    if not args.no_daemon and args.engine != "say":
        if speak_via_daemon(text, args.voice, speed, args.wait, args.engine, args.quiet):
            return 0
        # daemon unreachable -> fall through to inline so we never go silent

    Engine(engine=args.engine, quiet=args.quiet).speak(text, args.voice, speed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
