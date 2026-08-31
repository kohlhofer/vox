#!/usr/bin/env python3
"""vox — read text out loud with a good neural voice, from anywhere.

A tiny CLI so an AI agent (or any script) can grab your attention or give you a
spoken update:  vox "I need your input on the migration"

Mac only, for now. Quality comes from Kokoro-82M (the same voices local-voice
uses; default `af_bella`) running through mlx-audio on Apple Silicon. Anywhere
that isn't available, it falls back to the built-in macOS `say` voice so it
still works — quality drops, but it never goes silent.

Your own voice is opt-in: `vox --add-voice NAME clip.m4a` clones it from a short
recording (MOSS-TTS-Nano, CPU, see vox_moss.py) and `-v NAME` speaks with it.

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
import signal
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
ENGINES = ("auto", "kokoro", "clone", "say")  # clone = cloned voices (vox_moss.py); "moss" is a hidden alias
DEFAULT_SPEED = 1.1          # 1.0 = natural pace; >1 = snappier
SPEED_MIN, SPEED_MAX = 0.5, 2.0

SAY_BASE_WPM = 175           # macOS `say` baseline; scaled by speed for fallback

IDLE_TIMEOUT = 600.0         # daemon exits after this many idle seconds
MOSS_IDLE_TIMEOUT = float(os.environ.get("VOX_MOSS_IDLE") or 120.0)
                             # cloned-voice model (~1.4 GB) is dropped after this many idle
                             # seconds; Kokoro and the daemon stay warm, reload costs ~2 s
STARTUP_TIMEOUT = 90.0       # client waits this long for a cold daemon (model dl)

_RUNTIME = os.environ.get("XDG_RUNTIME_DIR") or os.path.expanduser("~/.cache/vox")
SOCK_PATH = os.path.join(_RUNTIME, "vox.sock")
LOCK_PATH = os.path.join(_RUNTIME, "vox.lock")
LOG_PATH = os.path.join(_RUNTIME, "daemon.log")
PID_PATH = os.path.join(_RUNTIME, "vox.pid")   # live daemon's pid; used to reap a wedged one


def _eprint(msg: str, quiet: bool = False) -> None:
    """Status to stderr — stdout stays clean for callers that capture it."""
    if not quiet:
        print(msg, file=sys.stderr, flush=True)


def clamp_speed(s: float) -> float:
    return max(SPEED_MIN, min(SPEED_MAX, s))


def _configure_espeak() -> None:
    """Point Kokoro's phonemizer at the espeak-ng bundled by `espeakng-loader`.

    Kokoro (via misaki → phonemizer) needs an espeak-ng library to pronounce
    out-of-dictionary words — names, abbreviations. Without it those words are
    skipped and the segment drops to the macOS `say` voice. When Homebrew's
    espeak-ng isn't installed (it usually isn't), the `espeakng-loader` wheel
    ships the library and its data; we just have to tell phonemizer where they
    are. Idempotent, and a total no-op if anything's missing so a broken espeak
    never blocks the neural voice from loading."""
    if os.environ.get("PHONEMIZER_ESPEAK_LIBRARY") and os.environ.get("ESPEAK_DATA_PATH"):
        return
    try:
        import espeakng_loader                              # noqa: WPS433
        from phonemizer.backend.espeak.wrapper import EspeakWrapper  # noqa: WPS433

        lib = espeakng_loader.get_library_path()
        data = espeakng_loader.get_data_path()
        os.environ.setdefault("PHONEMIZER_ESPEAK_LIBRARY", lib)
        os.environ.setdefault("ESPEAK_DATA_PATH", data)      # espeak-ng reads this
        EspeakWrapper.set_library(lib)
    except Exception:                                        # noqa: BLE001
        pass                                                 # names may drop to `say`; not fatal


# --------------------------------------------------------------------------- #
# Engine: synthesis + playback                                                #
# --------------------------------------------------------------------------- #

class Engine:
    """Turns text into audio and plays it. Prefers Kokoro; degrades to `say`.

    `engine` is one of: "auto" (pick by voice — Kokoro for stock ids, the
    cloning model for cloned/built-in names, `say` as last resort), "kokoro",
    "clone", or "say". The Kokoro model is loaded lazily on first use and
    cached; if it ever fails to load we remember that and use `say` for the
    rest of the process.
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
        self._moss = None                          # MossEngine, created on first cloned-voice use
        self._moss_last_used = 0.0

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
            _configure_espeak()                              # OOD words (names) need espeak
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
        moss = self._moss                     # capture once: the idle-unload thread may None it
        if moss is not None and moss.loaded:
            return "moss+kokoro" if self._tts is not None else "moss"
        if self._tts is not None:
            return "kokoro"
        if self._kokoro_broken:
            return "say"
        return "kokoro?" if self.mode != "say" else "say"

    # -- Cloned voices (MOSS) ----------------------------------------------- #

    @staticmethod
    def _use_moss(mode: str, voice: str, moss_voice: bool) -> bool:
        """Route a request to the cloned-voice engine? `moss_voice` says whether
        `voice` is one of its (an installed clip or a built-in preset). --engine
        clone forces it; auto picks it for its voices and never for a stock id."""
        if mode in ("clone", "moss"):         # moss: legacy alias, e.g. an old client's request
            return True
        return mode == "auto" and voice not in VOICES and moss_voice

    def _speak_moss(self, text: str, voice: str, speed: float) -> bool:
        """Speak via the cloned-voice engine. False means it couldn't (deps,
        model, unknown voice) and the caller should use another voice."""
        self._moss_last_used = time.monotonic()
        try:
            if self._moss is None:
                import vox_moss                                  # noqa: WPS433
                self._moss = vox_moss.MossEngine(quiet=self.quiet, interrupt=self._interrupt)
            return self._moss.speak(text, voice, speed)
        except Exception as exc:                                  # noqa: BLE001
            _eprint(f"vox: cloned voice failed ({exc}).", self.quiet)
            return False
        finally:
            self._moss_last_used = time.monotonic()

    def unload_moss_if_idle(self, timeout: float = MOSS_IDLE_TIMEOUT, now: float | None = None) -> bool:
        """Drop the cloned-voice engine (and its ~1.4 GB) once it has been idle for
        `timeout` seconds. Never while it is speaking. Returns True if it unloaded."""
        moss = self._moss
        if moss is None:
            return False
        now = time.monotonic() if now is None else now
        if now - self._moss_last_used < timeout:
            return False
        if not moss._lock.acquire(blocking=False):               # mid-sentence: try again later
            return False
        try:
            self._moss = None
            moss.close()
        finally:
            moss._lock.release()
        import gc                                                # noqa: WPS433
        gc.collect()
        return True

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
        moss = self._moss                     # capture once: the idle-unload thread may None it
        if moss is not None:
            moss.stop()
        with self._play_lock:
            if self._current and self._current.poll() is None:
                self._current.terminate()

    # -- Public ------------------------------------------------------------- #

    def speak(self, text: str, voice: str, speed: float, mode: str | None = None) -> None:
        """Speak `text`, blocking until it finishes. Long text is chunked and
        synthesized one piece ahead while the previous piece plays, so there's
        no long pause between sentences. A chunk that Kokoro can't synthesize
        falls back to `say` for that chunk only — it never poisons the rest.
        `mode` overrides the engine for this call (the daemon is started once
        but each request carries its own --engine).
        """
        text = text.strip()
        if not text:
            return
        speed = clamp_speed(speed)
        mode = mode or self.mode
        self._interrupt.clear()
        if self._use_moss(mode, voice, _is_moss_voice(voice)):
            if self._speak_moss(text, voice, speed) or self._interrupt.is_set():
                return
            _eprint(f"vox: using {DEFAULT_VOICE} instead.", self.quiet)
        if voice not in VOICES:                   # never go silent: a name Kokoro doesn't know
            voice = DEFAULT_VOICE                 # (e.g. a clip removed while the job was queued)
        chunks = self._chunk_text(text)
        use_kokoro = mode != "say" and self._ensure_loaded()

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
    __slots__ = ("text", "voice", "speed", "mode", "done")

    def __init__(self, text, voice, speed, mode=None):
        self.text = text
        self.voice = voice
        self.speed = speed
        self.mode = mode
        self.done = threading.Event()


class Daemon:
    """Holds the engine, serializes playback through a single worker, and exits
    on its own once idle. The accept loop only enqueues (cheap), so it stays
    responsive while audio plays in the worker thread."""

    def __init__(self, engine: str = "auto"):
        self.engine = Engine(engine=engine, quiet=False)
        self.jobs: "Queue[_Job | None]" = Queue()
        self.last_active = time.monotonic()
        self.busy = False                       # a job is in flight (worker between get and done)
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
                self.busy = True
                self.engine.speak(job.text, job.voice, job.speed, mode=job.mode)
            except Exception:                                 # noqa: BLE001
                pass
            finally:
                self.busy = False
                job.done.set()
                self._touch()

    def _idle_exit_due(self, now: float | None = None) -> bool:
        """Nothing queued, nothing in flight, idle past the timeout. `busy` is what
        keeps a long cloned-voice read alive: its streaming playback never touches
        `engine._current` (that only tracks afplay/say subprocesses)."""
        now = time.monotonic() if now is None else now
        return (now - self.last_active > IDLE_TIMEOUT and self.jobs.empty()
                and not self.busy and self.engine._current is None)

    def _idle_watch(self):
        while True:
            time.sleep(15.0)
            if self.jobs.empty() and not self.busy and self.engine.unload_moss_if_idle():
                _eprint("vox: cloned-voice model released after idling.")
            if self._idle_exit_due():
                _safe_unlink(SOCK_PATH)     # os._exit skips atexit; clean up first
                _safe_unlink(PID_PATH)
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
            mode = req.get("engine")
            if mode == "moss":                  # legacy alias from an older client
                mode = "clone"
            job = _Job(text, req.get("voice", DEFAULT_VOICE), req.get("speed", DEFAULT_SPEED),
                       mode if mode in ENGINES else None)
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
    os.chmod(_RUNTIME, 0o700)                  # runtime dir and socket are this user's only
    lock_fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(lock_fd)                  # another daemon owns it — stand down
        return 0
    try:
        _safe_unlink(SOCK_PATH)            # clear a stale socket from a crash
        server = _Server(SOCK_PATH, _Handler)
        os.chmod(SOCK_PATH, 0o600)         # connecting needs write permission; keep it ours
        server.app = Daemon(engine=engine)  # type: ignore[attr-defined]
        server.app.server = server          # so a `quit` request can shut us down
        with open(PID_PATH, "w") as f:      # record pid so a client can reap us if we wedge
            f.write(str(os.getpid()))
        atexit.register(lambda: (_safe_unlink(SOCK_PATH), _safe_unlink(PID_PATH)))
        try:
            server.serve_forever(poll_interval=0.5)   # keeps --quit responsive
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
            _safe_unlink(SOCK_PATH)
            _safe_unlink(PID_PATH)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
    return 0


def _safe_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _moss():
    """The cloned-voice module, or None if it can't be imported. It imports only
    the standard library at module level, so this is cheap and the Kokoro path
    never depends on it."""
    try:
        import vox_moss                                          # noqa: WPS433
        return vox_moss
    except Exception:                                            # noqa: BLE001
        return None


def _is_moss_voice(voice: str) -> bool:
    """An installed cloned voice or one of the cloning engine's built-in presets."""
    m = _moss()
    return bool(m and m.is_moss_voice(voice))


def custom_voices() -> dict:
    """{name: description} for the cloning engine's voices: installed clips first,
    then its built-in presets. Empty when the module is unavailable."""
    m = _moss()
    if m is None:
        return {}
    out = {name: "cloned voice" for name in m.list_voices()}
    out.update({name: "built-in cloned voice" for name in m.BUILTIN_PRESETS})
    return out


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


def _reap_stale() -> None:
    """Clear a daemon that is present but not serving. A clean exit leaves nothing
    behind, but a *wedged* daemon (alive yet hung) or a socket orphaned by a hard
    crash/reboot makes every client stall on connect — and the respawn stands down
    on the still-held lock. Call this only after a failed ping: if the socket is
    gone there's nothing to do; otherwise SIGTERM the recorded pid (releasing its
    flock) and drop the socket so the next spawn binds cleanly."""
    if not os.path.exists(SOCK_PATH):
        return
    try:
        with open(PID_PATH) as f:
            pid = int(f.read().strip())
        os.kill(pid, signal.SIGTERM)
        for _ in range(20):                 # up to ~2s for a graceful exit
            time.sleep(0.1)
            os.kill(pid, 0)                 # raises ProcessLookupError once it's gone
    except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError):
        pass
    _safe_unlink(SOCK_PATH)
    _safe_unlink(PID_PATH)


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
    if _ping() is None:
        _reap_stale()                       # a wedged/orphaned daemon would stall the respawn
        if not _spawn_daemon(engine, quiet):
            return False
    # On --wait the socket stays open until playback finishes; give it lots of
    # headroom so we never time out mid-sentence and re-speak inline.
    timeout = 600.0 if wait else 10.0
    resp = _request({"cmd": "speak", "text": text, "voice": voice,
                     "speed": speed, "wait": wait, "engine": engine}, timeout=timeout)
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
    print(f"Voices (default: {DEFAULT_VOICE}):\n")
    for vid, desc in VOICES.items():
        print(f"  {vid:<11} {desc}")
    m = _moss()
    if m:
        print("\nCloned voices — run on the CPU, hold ~1.4 GB while loaded, ignore --speed:\n")
        for name in m.list_voices():
            print(f"  {name:<11} your clip")
        print(f"  built-in:   {', '.join(m.BUILTIN_PRESETS)}")
    print("\nClone your own:  vox --add-voice NAME clip.m4a   (10–20 s of clean speech)")
    print("Any Mac also has the built-in `say` voices; set SPEAK_SAY_VOICE to pick one.")


# --------------------------------------------------------------------------- #
# Doctor                                                                      #
# --------------------------------------------------------------------------- #

# `command -v vox` — the guard the README tells agents to wrap announcements in
# — only proves a file exists and is executable. It says nothing about whether
# the venv behind that launcher still imports. So a half-broken install doesn't
# announce itself; it just goes quiet, at exactly the moment the whole point was
# to speak. --doctor is the check that actually runs things.

DOCTOR_OK, DOCTOR_WARN, DOCTOR_FAIL, DOCTOR_NA = "OK", "WARN", "FAIL", "--"

# Imported by the Kokoro path. Probed with find_spec rather than imported: this
# has to stay fast enough to run before an announcement.
KOKORO_IMPORTS = ("numpy", "mlx_audio", "misaki", "phonemizer",
                  "espeakng_loader", "num2words")


def _kokoro_cached() -> bool:
    """Has the model already been downloaded? Answered by looking for the cache
    directory, never by loading — asking whether vox is healthy must not trigger
    a ~160 MB download as a side effect."""
    root = os.environ.get("HF_HOME") or os.path.expanduser("~/.cache/huggingface")
    return os.path.isdir(os.path.join(root, "hub", "models--" + KOKORO_REPO.replace("/", "--")))


def _doctor_launcher_row() -> tuple[str, str, str]:
    """Does the `vox` on PATH point at this checkout? Anyone with both a working
    copy and an installed one shares a single launcher between them, so edits
    here can appear to do nothing while the shim runs the other tree."""
    import shutil                                             # noqa: WPS433
    here = os.path.dirname(os.path.abspath(__file__))
    found = shutil.which("vox")
    if not found:
        return DOCTOR_WARN, "launcher", "no `vox` on PATH — is ~/.local/bin on it?"
    try:
        with open(found, encoding="utf-8", errors="replace") as fh:
            body = fh.read()
    except OSError as exc:
        return DOCTOR_WARN, "launcher", f"{found} unreadable ({exc})"
    if here in body:
        return DOCTOR_OK, "launcher", f"{found} → this checkout"
    return DOCTOR_WARN, "launcher", f"{found} → a different checkout, not {here}"


def _doctor_rows() -> list[tuple[str, str, str]]:
    """Probe the install. Each row is (status, label, detail)."""
    import importlib.util                                     # noqa: WPS433
    import platform as platform_mod                           # noqa: WPS433
    import shutil                                             # noqa: WPS433

    rows: list[tuple[str, str, str]] = []

    ver = sys.version_info
    # requirements.txt is resolved against 3.13; older interpreters pull
    # different wheels and mlx-audio isn't proven on them.
    rows.append((DOCTOR_OK if (ver.major, ver.minor) >= (3, 13) else DOCTOR_WARN,
                 "python",
                 f"{ver.major}.{ver.minor}.{ver.micro} ({sys.executable})"))

    system, machine = platform_mod.system(), platform_mod.machine()
    arch_ok = system == "Darwin" and machine == "arm64"
    rows.append((DOCTOR_OK if arch_ok else DOCTOR_WARN, "platform",
                 f"{system} {machine}" if arch_ok else
                 f"{system} {machine} — the neural voice needs macOS on Apple Silicon"))

    rows.append(_doctor_launcher_row())

    try:
        missing = [m for m in KOKORO_IMPORTS if importlib.util.find_spec(m) is None]
    except Exception as exc:                                  # noqa: BLE001
        missing = [f"<probe failed: {exc}>"]
    rows.append((DOCTOR_OK if not missing else DOCTOR_FAIL, "kokoro deps",
                 "all present" if not missing else
                 f"missing {', '.join(missing)} — pip install -r requirements.txt"))

    cached = _kokoro_cached()
    rows.append((DOCTOR_OK if cached else DOCTOR_NA, "model cache",
                 f"{KOKORO_REPO} present" if cached else
                 f"{KOKORO_REPO} not downloaded yet (~160 MB on first speak)"))

    say = shutil.which("say")
    rows.append((DOCTOR_OK if say else DOCTOR_FAIL, "say fallback",
                 say or "no `say` — nothing to fall back on"))

    return rows


def _doctor_verdict(rows: list[tuple[str, str, str]]) -> tuple[int, str]:
    """Exit 0 whenever vox can speak *somehow* — the neural voice is preferred,
    but `say` still counts as working. Exit 1 only when nothing can talk, so
    `vox --doctor` is usable as the health probe `command -v vox` can't be."""
    status = {label: st for st, label, _ in rows}
    if status.get("kokoro deps") == DOCTOR_OK and status.get("platform") == DOCTOR_OK:
        return 0, "vox can speak with the neural voice."
    if status.get("say fallback") == DOCTOR_OK:
        return 0, "vox can speak, but only with the system `say` voice."
    return 1, "vox cannot speak on this machine."


def _print_doctor() -> int:
    rows = _doctor_rows()
    code, summary = _doctor_verdict(rows)
    print("vox doctor\n")
    for status, label, detail in rows:
        print(f"  {status:<4}  {label:<12}  {detail}")
    print(f"\n{summary}")
    return code


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="vox",
        description="Read text out loud with a good neural voice (Mac).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  vox \"Build finished — all green.\"\n"
            "  vox -v am_onyx -s 0.95 \"Heads up, I need your input.\"\n"
            "  vox --add-voice me ~/clip.m4a && vox -v me \"In my own voice.\"\n"
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
    p.add_argument("-v", "--voice", default=DEFAULT_VOICE, metavar="NAME",
                   help=f"voice (default: {DEFAULT_VOICE}; --list-voices shows all)")
    p.add_argument("-s", "--speed", type=float, default=DEFAULT_SPEED, metavar="X",
                   help=f"speaking speed {SPEED_MIN}–{SPEED_MAX} (default: {DEFAULT_SPEED}); "
                        "cloned voices ignore it")
    p.add_argument("-w", "--wait", action="store_true",
                   help="block until speech finishes (default: return once queued)")
    p.add_argument("-l", "--list-voices", action="store_true", help="list voices and exit")
    p.add_argument("--doctor", action="store_true",
                   help="check this install and exit (0 = can speak, 1 = cannot)")
    p.add_argument("--add-voice", nargs=2, metavar=("NAME", "FILE"),
                   help="clone a voice from an audio clip (10–20 s of clean speech, "
                        "with the speaker's consent) and exit")
    p.add_argument("--remove-voice", metavar="NAME", help="delete a cloned voice and exit")
    p.add_argument("--stop", action="store_true", help="stop current speech and clear the queue")
    p.add_argument("--quit", action="store_true",
                   help="shut down the background voice daemon (frees the model from memory)")
    p.add_argument("--no-daemon", action="store_true",
                   help="synthesize inline instead of using the warm daemon")
    p.add_argument("--engine", choices=list(ENGINES), default="auto",
                   type=lambda s: "clone" if s == "moss" else s,   # accept the old name quietly
                   help="auto (default) picks by voice — Kokoro for stock ids, the cloning "
                        "model for cloned and built-in names; kokoro/clone/say force one")
    p.add_argument("-q", "--quiet", action="store_true", help="suppress status messages")
    p.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)  # internal
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    if args.serve:
        return run_daemon(engine=args.engine)

    if args.doctor:
        return _print_doctor()

    if args.list_voices:
        _print_voices()
        return 0

    if args.add_voice or args.remove_voice:
        import vox_moss                                          # noqa: WPS433
        try:
            if args.add_voice:
                name, src = args.add_voice
                path = vox_moss.add_voice(name, src, quiet=args.quiet, reserved=VOICES)
                _eprint(f"vox: voice '{name}' ready ({path}). Try: vox -v {name} \"Hello.\"", args.quiet)
            else:
                gone = vox_moss.remove_voice(args.remove_voice)
                _eprint(f"vox: voice '{args.remove_voice}' " + ("removed." if gone else "not found."), args.quiet)
            return 0
        except Exception as exc:                                  # noqa: BLE001
            _eprint(f"vox: {exc}", args.quiet)
            return 2

    if args.stop:
        resp = _request({"cmd": "stop"})
        _eprint("vox: stopped." if resp else "vox: nothing to stop (no daemon running).", args.quiet)
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

    voice = args.voice
    moss_voice = voice not in VOICES and _is_moss_voice(voice)
    if voice not in VOICES and not moss_voice and args.engine not in ("say", "clone"):
        _eprint(f"vox: unknown voice '{voice}'. Try --list-voices.", args.quiet)
        return 2
    if args.engine == "clone" and not moss_voice:
        what = "a stock Kokoro voice" if voice in VOICES else "not a cloned voice"
        _eprint(f"vox: '{voice}' is {what}; --engine clone speaks cloned or built-in voices "
                "(vox --add-voice, or see --list-voices).", args.quiet)
        return 2
    if moss_voice and args.engine in ("kokoro", "say"):
        _eprint(f"vox: '{voice}' is a cloned voice; --engine {args.engine} can't speak it — "
                f"using {DEFAULT_VOICE} instead (drop --engine to hear it).", args.quiet)

    speed = clamp_speed(args.speed)
    if Engine._use_moss(args.engine, voice, moss_voice):
        m = _moss()
        if m is not None and m.deps_missing():
            # Installing the extra packages (and later the ~730 MB model) is the
            # opt-in boundary. Cross it only on an explicit --engine clone or for a
            # user who already has cloned voices — never because some text talked an
            # agent into trying `vox -v Ava`.
            if args.engine == "clone" or m.list_voices():
                m.ensure_deps(quiet=args.quiet)      # foreground, not from inside the daemon
            else:
                _eprint(f"vox: '{voice}' needs the cloning model, which isn't set up — "
                        "run vox --add-voice once (or force --engine clone) to install it. "
                        f"Using {DEFAULT_VOICE} instead.", args.quiet)
                voice = DEFAULT_VOICE
        if voice != DEFAULT_VOICE and speed != DEFAULT_SPEED:
            _eprint("vox: --speed doesn't apply to cloned voices (the model has no tempo control).",
                    args.quiet)
    _eprint(f"vox: \"{text[:60]}{'…' if len(text) > 60 else ''}\"", args.quiet)

    if not args.no_daemon and args.engine != "say":
        if speak_via_daemon(text, voice, speed, args.wait, args.engine, args.quiet):
            return 0
        # daemon unreachable -> fall through to inline so we never go silent

    Engine(engine=args.engine, quiet=args.quiet).speak(text, voice, speed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
