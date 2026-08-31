#!/usr/bin/env python3
"""vox_moss — your own voice for vox, via MOSS-TTS-Nano (ONNX Runtime, CPU only).

Opt-in. vox never imports this module until a custom voice is added or used, so
machines that only use the Kokoro voices see no change: no extra packages, no
model download, no memory.

    vox --add-voice alex ~/clips/me-reading-a-paragraph.m4a   # once
    vox -v alex "The build is green."                          # from then on

A voice is one audio clip (10–20 s of clean speech) stored at the codec's native
48 kHz stereo under ~/.config/vox/voices/<name>.wav, plus a small cache of the
clip encoded into audio tokens. Cloning is zero-shot: the tokens are prepended
to every generation as a prompt. Nothing is trained, nothing changes on disk
after the one-time encode.

Speech streams to the speaker while it is generated (first sound well under a
second on Apple silicon); vox's --stop interrupts it like any other voice.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent

VOICES_DIR = Path(os.environ.get("VOX_VOICES_DIR") or "~/.config/vox/voices").expanduser()
MODELS_DIR = Path(os.environ.get("VOX_MODELS_DIR") or "~/.cache/vox/models/moss-tts-nano").expanduser()

VOICE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
SAMPLE_RATE = 48_000                 # the codec's native format; clips are converted on add
CHANNELS = 2
MIN_CLIP_SECONDS, MAX_CLIP_SECONDS = 3.0, 60.0

# Runtime packages the cloned-voice path needs beyond vox's base install. Installed
# into vox's own venv the first time a voice is added (~80 MB, mostly onnxruntime).
REQUIRED = (("onnxruntime", "onnxruntime>=1.20"), ("soundfile", "soundfile"),
            ("sounddevice", "sounddevice"), ("sentencepiece", "sentencepiece"),
            ("huggingface_hub", "huggingface_hub"), ("numpy", "numpy"))

# Tuning, measured on an M3 MacBook Air (16 GB) 2026-08-30. Each generated frame is
# 80 ms of audio; real time needs generation + decode under that.
THREADS = 4          # 2/3/4 ORT threads were equivalent (RTF 0.34–0.38); 6 was slower (0.58)
MAX_TOKENS = 48      # text tokens per chunk; 75 (upstream default) drifted into babble on a
                     # 97-word paragraph (WER 26 %), 48 gave 0 %
DECODE_FRAMES = 4    # frames per codec call: one call costs ~45 ms for 1 frame, ~60 ms for 4
PREBUFFER_MS = 250   # queue this much before playback starts; without it the first ~150 ms crackled
PEAK_DBFS = -1.0     # reference clips are peak-normalized to this on add; a quiet clip clones badly

# Built-in voices of the model (from its manifest at the vendored commit). Reachable with
# `vox -v Ava` or `--engine moss -v Ava`; no clip needed. Names are case-sensitive.
BUILTIN_PRESETS = ("Ava", "Bella", "Adam", "Nathan", "Anon", "Arisa", "Soyo", "Saki", "Mortis",
                   "Umiri", "Mei", "Trump", "Junhao", "Zhiming", "Weiguo", "Xiaoyu", "Yuewen", "Lingyu")


def _eprint(msg: str, quiet: bool = False) -> None:
    if not quiet:
        print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# Voices on disk                                                              #
# --------------------------------------------------------------------------- #

def validate_name(name: str) -> str:
    """Voice names are short, lowercase, filesystem-safe. Returns the name."""
    if not isinstance(name, str) or not VOICE_NAME_RE.match(name):
        raise ValueError(f"voice name {name!r}: use 1–32 lowercase letters, digits, '-' or '_'")
    return name


def list_voices(voices_dir: Path | None = None) -> dict[str, Path]:
    """{name: wav path} for every custom voice, sorted by name."""
    d = voices_dir or VOICES_DIR
    if not d.is_dir():
        return {}
    return {p.stem: p for p in sorted(d.glob("*.wav")) if VOICE_NAME_RE.match(p.stem)}


def voice_path(name: str, voices_dir: Path | None = None) -> Path | None:
    return list_voices(voices_dir).get(name)


def is_custom_voice(name: str, voices_dir: Path | None = None) -> bool:
    return voice_path(name, voices_dir) is not None


def is_preset(name: str) -> bool:
    return name in BUILTIN_PRESETS


def is_moss_voice(name: str, voices_dir: Path | None = None) -> bool:
    """A name this engine can speak with: an installed clip or a built-in preset."""
    return is_custom_voice(name, voices_dir) or is_preset(name)


def _codes_cache_path(wav: Path) -> Path:
    st = wav.stat()
    key = hashlib.sha1(f"{wav.resolve()}|{st.st_size}|{int(st.st_mtime)}".encode()).hexdigest()[:10]
    return wav.with_name(f"{wav.stem}.{key}.codes.json")


def _clip_seconds(path: Path) -> float | None:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        out = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                              "-of", "csv=p=0", str(path)], capture_output=True, text=True, check=True).stdout
        return float(out.strip())
    except (subprocess.CalledProcessError, ValueError):
        return None


def _peak_gain_db(ffmpeg: str, path: Path) -> float:
    """Gain (dB) that brings the clip's peak to PEAK_DBFS; 0.0 if it can't be measured."""
    out = subprocess.run([ffmpeg, "-v", "info", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    m = re.search(r"max_volume:\s*(-?[0-9.]+) dB", out)
    return (PEAK_DBFS - float(m.group(1))) if m else 0.0


def _convert_clip(source: Path, dest: Path) -> None:
    """Any audio file -> 48 kHz stereo 16-bit WAV, leading/trailing silence trimmed and
    peak-normalized (a quiet recording clones measurably worse than a loud one).
    Needs ffmpeg for anything that isn't already a WAV at the right rate."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        trim = ("silenceremove=start_periods=1:start_threshold=-40dB,areverse,"
                "silenceremove=start_periods=1:start_threshold=-40dB,areverse")
        subprocess.run([ffmpeg, "-v", "error", "-y", "-i", str(source), "-ac", str(CHANNELS),
                        "-ar", str(SAMPLE_RATE), "-af", trim, "-c:a", "pcm_s16le", str(dest)], check=True)
        gain = _peak_gain_db(ffmpeg, dest)
        if abs(gain) >= 0.5:
            loud = dest.with_suffix(".norm.wav")
            subprocess.run([ffmpeg, "-v", "error", "-y", "-i", str(dest), "-af", f"volume={gain:.2f}dB",
                            "-c:a", "pcm_s16le", str(loud)], check=True)
            os.replace(loud, dest)
        return
    import numpy as np  # noqa: WPS433
    import soundfile as sf  # noqa: WPS433
    data, sr = sf.read(str(source), dtype="float32", always_2d=True)
    if int(sr) != SAMPLE_RATE:
        raise RuntimeError(f"ffmpeg not found and {source.name} is {sr} Hz; install ffmpeg "
                           f"(brew install ffmpeg) or supply a {SAMPLE_RATE} Hz WAV")
    if data.shape[1] == 1:
        data = np.repeat(data, CHANNELS, axis=1)
    peak = float(np.abs(data).max()) if data.size else 0.0
    if peak > 0:
        data = data * (10 ** (PEAK_DBFS / 20) / peak)
    sf.write(str(dest), data[:, :CHANNELS], SAMPLE_RATE, subtype="PCM_16")


def _encode_in_child(wav: Path) -> None:
    """Encode a clip's prompt cache in a child process: the encoder peaks at 2–3 GB of
    memory for a 16 s clip, and that dies with the child instead of staying with vox."""
    r = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--encode", str(wav)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        tail = "\n".join((r.stderr or r.stdout or "").strip().splitlines()[-3:])
        raise RuntimeError(f"encoding {wav.name} failed: {tail or f'exit {r.returncode}'}")


def add_voice(name: str, source: str | os.PathLike, quiet: bool = False,
              voices_dir: Path | None = None, reserved: "set[str] | dict | None" = None) -> Path:
    """Install a custom voice from an audio clip: convert, store, pre-encode. A failure
    at any step leaves no half-installed voice behind; an existing voice of the same
    name is replaced only once the new clip is fully ready. `reserved` names (the
    stock voice ids) are refused so a clip can never shadow, or be shadowed by, one."""
    validate_name(name)
    if reserved and name in reserved:
        raise ValueError(f"'{name}' is a stock voice id; pick another name")
    src = Path(source).expanduser()
    if not src.is_file():
        raise FileNotFoundError(f"no such audio file: {src}")
    d = voices_dir or VOICES_DIR
    d.mkdir(parents=True, exist_ok=True)
    if not ensure_deps(quiet=quiet):
        raise RuntimeError("voice-cloning dependencies could not be installed")
    dest = d / f"{name}.wav"
    tmp = dest.with_suffix(".tmp.wav")
    try:
        _convert_clip(src, tmp)
        secs = _clip_seconds(tmp)
        if secs is not None and secs < MIN_CLIP_SECONDS:
            raise ValueError(f"clip is {secs:.1f}s after trimming silence; use at least {MIN_CLIP_SECONDS:.0f}s of speech")
        if secs is not None and secs > MAX_CLIP_SECONDS:
            _eprint(f"vox: clip is {secs:.0f}s; 10–20 s of clean speech clones best.", quiet)
        os.replace(tmp, dest)
    finally:
        if tmp.exists():
            tmp.unlink()
    for old in d.glob(f"{name}.*.codes.json"):        # caches are keyed on size+mtime; stale ones go
        old.unlink()
    _eprint(f"vox: encoding voice '{name}' (first time also downloads the ~730 MB model)…", quiet)
    try:
        _encode_in_child(dest)
    except Exception:
        dest.unlink(missing_ok=True)                    # no voice that lists but can't speak
        raise
    return dest


def remove_voice(name: str, voices_dir: Path | None = None) -> bool:
    validate_name(name)
    d = voices_dir or VOICES_DIR
    wav = d / f"{name}.wav"
    if not wav.exists():
        return False
    wav.unlink()
    for old in d.glob(f"{name}.*.codes.json"):
        old.unlink()
    return True


# --------------------------------------------------------------------------- #
# Dependencies, installed on demand                                           #
# --------------------------------------------------------------------------- #

def deps_missing() -> list[str]:
    missing = []
    for module, spec in REQUIRED:
        try:
            __import__(module)
        except ImportError:
            missing.append(spec)
    return missing


def ensure_deps(quiet: bool = False, install: bool = True) -> bool:
    missing = deps_missing()
    if not missing:
        return True
    if not install:
        return False
    _eprint(f"vox: installing voice-cloning dependencies ({', '.join(missing)}; ~80 MB, one time)…", quiet)
    r = subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", *missing])
    if r.returncode != 0:
        _eprint("vox: dependency install failed; see pip output above.", quiet)
        return False
    return not deps_missing()


# --------------------------------------------------------------------------- #
# Model                                                                       #
# --------------------------------------------------------------------------- #

class _LazySessions(dict):
    """ORT sessions the streaming path never touches are built on first access.
    Upstream builds all nine eagerly and the shared weight files get loaded once
    per session — about 300 MB of resident memory for nothing."""

    def __init__(self, eager: dict, lazy_paths: dict, builder):
        super().__init__(eager)
        self._lazy_paths, self._builder = lazy_paths, builder

    def __missing__(self, key):
        if key not in self._lazy_paths:
            raise KeyError(key)
        self[key] = self._builder(self._lazy_paths[key])
        return self[key]

    def __contains__(self, key):
        return dict.__contains__(self, key) or key in self._lazy_paths


def load_runtime(quiet: bool = False):
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    from vendor.moss_tts_nano.onnx_tts_runtime import OnnxTtsRuntime, _find_manifest_path  # noqa: WPS433

    class LeanRuntime(OnnxTtsRuntime):
        def _create_sessions(self):
            tts_dir, codec_dir = self.tts_meta_path.parent, self.codec_meta_path.parent
            files, cfiles = self.tts_meta["files"], self.codec_meta["files"]
            eager = {
                "prefill": self._session(tts_dir / files["prefill"]),
                "decode": self._session(tts_dir / files["decode_step"]),
                "codec_decode_step": self._session(codec_dir / cfiles["decode_step"]),
            }
            lazy = {
                "local_decoder": tts_dir / files["local_decoder"],
                "codec_encode": codec_dir / cfiles["encode"],
                "codec_decode": codec_dir / cfiles["decode_full"],
            }
            for name in ("local_greedy_frame", "local_fixed_sampled_frame", "local_cached_step"):
                if files.get(name):
                    lazy[name] = tts_dir / files[name]
            if "local_fixed_sampled_frame" in lazy:               # the default sampler
                eager["local_fixed_sampled_frame"] = self._session(lazy.pop("local_fixed_sampled_frame"))
            return _LazySessions(eager, lazy, self._session)

    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    if _find_manifest_path(MODELS_DIR) is None:
        import logging  # noqa: WPS433
        logging.getLogger("huggingface_hub").setLevel(logging.ERROR)   # "set HF_TOKEN" nag
        _eprint("vox: downloading the voice-cloning model (~730 MB, one time)…", quiet)
    return LeanRuntime(model_dir=MODELS_DIR, thread_count=THREADS,
                       output_dir=Path(tempfile.gettempdir()) / "vox-moss")


def encode_voice(runtime, wav: Path) -> list[list[int]]:
    """Reference clip -> prompt audio codes, cached beside the clip."""
    cache = _codes_cache_path(wav)
    if cache.exists():
        return json.loads(cache.read_text())
    codes = runtime.encode_reference_audio(wav)
    cache.write_text(json.dumps(codes))
    return codes


# --------------------------------------------------------------------------- #
# Playback: callback-driven output stream with a pre-buffer                   #
# --------------------------------------------------------------------------- #

class _Player:
    def __init__(self, sr: int, ch: int, prebuffer_ms: int = PREBUFFER_MS):
        import sounddevice as sd  # noqa: WPS433
        self.sr, self.ch = sr, ch
        self.prebuffer = int(sr * prebuffer_ms / 1000)
        self.lock = threading.Lock()
        self.buf: list = []
        self.buffered = 0
        self.aborted = False
        self._started = False
        self._closed = False
        self.stream = sd.OutputStream(samplerate=sr, channels=ch, dtype="float32",
                                      blocksize=0, latency="high", callback=self._cb)

    def _cb(self, out, frames, _time, _status) -> None:
        filled = 0
        with self.lock:
            while filled < frames and self.buf:
                head = self.buf[0]
                n = min(frames - filled, len(head))
                out[filled:filled + n] = head[:n]
                if n == len(head):
                    self.buf.pop(0)
                else:
                    self.buf[0] = head[n:]
                filled += n
                self.buffered -= n
        if filled < frames:
            out[filled:] = 0

    def push(self, pcm) -> None:
        with self.lock:
            self.buf.append(pcm)
            self.buffered += len(pcm)
            enough = self.buffered >= self.prebuffer
        if enough and not self._started:
            self._started = True
            self.stream.start()

    def finish(self) -> None:
        """Play out whatever is queued, then close. No-op after abort()."""
        if self.aborted:
            return
        if not self._started:
            with self.lock:
                pending = self.buffered > 0
            if pending:
                self._started = True
                self.stream.start()
            else:
                self._close()
                return
        while not self.aborted:
            with self.lock:
                if self.buffered <= 0:
                    break
            time.sleep(0.02)
        if not self.aborted:
            time.sleep(float(self.stream.latency) + 0.15)
        self._close()

    def abort(self) -> None:
        """Stop right now; safe from another thread and before the stream started."""
        self.aborted = True
        with self.lock:
            self.buf.clear()
            self.buffered = 0
        self._close(abort=True)

    def _close(self, abort: bool = False) -> None:
        with self.lock:
            if self._closed:
                return
            self._closed = True
        try:
            if self._started:
                (self.stream.abort if abort else self.stream.stop)()
            self.stream.close()
        except Exception:                                   # noqa: BLE001
            pass


class _Interrupted(Exception):
    pass


# --------------------------------------------------------------------------- #
# Engine                                                                      #
# --------------------------------------------------------------------------- #

class MossEngine:
    """Speaks in a cloned voice, streaming to the speaker while generating.

    Loads the model lazily on first use; a load failure latches so a broken
    install isn't retried on every alert (the caller falls back to a Kokoro voice).
    """

    def __init__(self, quiet: bool = False, interrupt: threading.Event | None = None):
        self.quiet = quiet
        self._interrupt = interrupt or threading.Event()
        self._runtime = None
        self._broken = False
        self._codes: dict[str, list] = {}       # keyed by cache path / preset, so a re-added clip is fresh
        self._player: _Player | None = None
        self._lock = threading.Lock()
        self._emitted_any = False

    @property
    def loaded(self) -> bool:
        return self._runtime is not None

    def _ensure_loaded(self) -> bool:
        if self._runtime is not None:
            return True
        if self._broken:
            return False
        missing = deps_missing()
        if missing:
            _eprint(f"vox: cloned voices need {', '.join(missing)} — run `vox --add-voice` once to install.", self.quiet)
            self._broken = True
            return False
        try:
            _eprint("vox: warming up the cloned voice…", self.quiet)
            self._runtime = load_runtime(self.quiet)
            return True
        except Exception as exc:                            # noqa: BLE001
            self._broken = True
            _eprint(f"vox: cloned voice unavailable ({exc}).", self.quiet)
            return False

    def _prompt_codes(self, voice: str) -> list[list[int]]:
        wav = voice_path(voice)
        if wav is not None:
            cache = _codes_cache_path(wav)                  # embeds size+mtime: a replaced clip is a new key
            key = str(cache)
            if key not in self._codes:
                if not cache.exists():                      # e.g. voices copied from another machine
                    _encode_in_child(wav)
                self._codes[key] = json.loads(cache.read_text())
            return self._codes[key]
        if not is_preset(voice):
            raise ValueError("no such cloned voice or preset")
        key = f"preset:{voice}"
        if key not in self._codes:
            self._codes[key] = self._runtime.resolve_prompt_audio_codes(voice=voice, prompt_audio_path=None)
        return self._codes[key]

    def stop(self) -> None:
        """Called from vox's stop path; the generation loop also watches the event."""
        self._interrupt.set()
        p = self._player
        if p is not None:
            p.abort()

    def speak(self, text: str, voice: str, speed: float = 1.0) -> bool:
        """Speak `text` in `voice`; blocks until done. Returns False only if nothing
        was said (engine not loaded, unknown voice, failure before the first sound)
        so the caller can use another voice. A failure after playback began is
        reported and counts as spoken — replaying the whole text in another voice
        would be worse than a truncated sentence. `speed` is accepted for interface
        parity; the model has no tempo control."""
        text = " ".join(text.split())
        if not text:
            return True
        with self._lock:
            if not self._ensure_loaded():
                return False
            try:
                codes = self._prompt_codes(voice)
            except Exception as exc:                        # noqa: BLE001
                _eprint(f"vox: voice '{voice}' unavailable ({exc}).", self.quiet)
                return False
            if self._interrupt.is_set():
                return True
            self._emitted_any = False
            try:
                self._stream(text, codes)
            except _Interrupted:
                pass
            except Exception as exc:                        # noqa: BLE001
                if not self._emitted_any:
                    _eprint(f"vox: cloned voice failed before speaking ({exc}).", self.quiet)
                    return False
                _eprint(f"vox: cloned voice cut off mid-way ({exc}).", self.quiet)
            return True

    def _stream(self, text: str, codes: list[list[int]]) -> None:
        import numpy as np  # noqa: WPS433
        from vendor.moss_tts_nano.ort_cpu_runtime import _resolve_stream_decode_frame_budget  # noqa: WPS433

        rt = self._runtime
        sr = int(rt.codec_meta["codec_config"]["sample_rate"])
        ch = int(rt.codec_meta["codec_config"]["channels"])
        prepared = rt.prepare_synthesis_text(text=text, voice="", enable_wetext=False,
                                             enable_normalize_tts_text=True)
        chunks = rt.split_voice_clone_text(str(prepared["text"]), max_tokens=MAX_TOKENS)
        player = _Player(sr, ch)
        self._player = player
        # Utterance-scoped, deliberately NOT reset per chunk: the decode budget is a
        # function of how far ahead of real time the whole utterance is.
        st = {"first": None, "emitted": 0, "decodes": 0}
        try:
            for ci, chunk in enumerate(chunks):
                if self._interrupt.is_set():
                    raise _Interrupted
                rows = rt.build_voice_clone_request_rows(codes, rt.encode_text(chunk))
                rt.codec_streaming_session.reset()
                pending: list[list[int]] = []

                def flush(force: bool) -> None:
                    if not pending:
                        return
                    ramp = min(DECODE_FRAMES, st["decodes"] + 1)
                    budget = max(ramp, _resolve_stream_decode_frame_budget(st["emitted"], sr, st["first"]))
                    if not force and len(pending) < budget:
                        return
                    n = len(pending) if force else min(len(pending), budget)
                    frame_chunk = pending[:n]
                    del pending[:n]
                    out = rt.codec_streaming_session.run_frames(frame_chunk)
                    st["decodes"] += 1
                    if out is None:
                        return
                    audio, length = out
                    if length <= 0:
                        return
                    if st["first"] is None:
                        st["first"] = time.perf_counter()
                    st["emitted"] += length
                    player.push(np.ascontiguousarray(audio[0, :, :length].T, dtype=np.float32))
                    self._emitted_any = True

                def on_frame(_all, _i, frame) -> None:
                    if self._interrupt.is_set():
                        raise _Interrupted
                    pending.append(list(frame))
                    flush(False)

                try:
                    rt.generate_audio_frames(rows, on_frame=on_frame)
                    flush(True)
                finally:
                    rt.codec_streaming_session.reset()
                if ci < len(chunks) - 1:
                    pause = int(sr * rt.estimate_voice_clone_inter_chunk_pause_seconds(chunk))
                    if pause > 0:
                        player.push(np.zeros((pause, ch), dtype=np.float32))
                        st["emitted"] += pause
        except _Interrupted:
            player.abort()
            raise
        finally:
            if not player.aborted:
                player.finish()
            self._player = None


# --------------------------------------------------------------------------- #
# Child-process entry: encode one clip and exit                               #
# --------------------------------------------------------------------------- #

def main(argv=None) -> int:
    import argparse  # noqa: WPS433
    p = argparse.ArgumentParser(prog="vox_moss", description="vox cloned-voice helper")
    p.add_argument("--encode", metavar="WAV", help="encode a 48 kHz reference clip into its prompt cache and exit")
    p.add_argument("-q", "--quiet", action="store_true")
    args = p.parse_args(argv)
    if args.encode:
        rt = load_runtime(args.quiet)
        encode_voice(rt, Path(args.encode))
        return 0
    p.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
