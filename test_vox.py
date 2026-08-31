#!/usr/bin/env python3
"""Unit tests for vox's pure logic — Markdown cleaning, chunking, and cloned-voice
routing. No Kokoro / MOSS / audio needed. Run: ./.venv/bin/python test_vox.py  (or pytest)."""

import tempfile
import threading
import types
from pathlib import Path

import numpy as np

import vox
import vox_moss


def test_clean_markdown_strips_structure():
    md = (
        "---\n"
        "title: Notes\n"
        "tags: [a, b]\n"
        "---\n"
        "# Heading\n\n"
        "A line with *emphasis*, _under_, and `code`.\n\n"
        "- bullet one\n"
        "- bullet two\n\n"
        "See [the docs](https://example.com/x) for more.\n\n"
        "---\n\n"
        "> a quote\n"
    )
    out = vox.clean_markdown(md)
    assert "title:" not in out and "tags:" not in out      # frontmatter gone
    assert "#" not in out and "*" not in out and "`" not in out and "_" not in out
    assert "https://example.com" not in out                 # url dropped...
    assert "the docs" in out                                # ...link text kept
    assert "bullet one" in out and "- bullet" not in out    # bullet marker gone
    assert "a quote" in out and ">" not in out
    assert "Heading" in out


def test_clean_markdown_keeps_paragraph_breaks():
    out = vox.clean_markdown("Para one.\n\nPara two.")
    assert "\n\n" in out                                     # blank line preserved


def test_clean_markdown_drops_code_blocks():
    md = "Intro.\n\n```python\nsecret = 1\nprint(secret)\n```\n\nOutro."
    out = vox.clean_markdown(md)
    assert "secret" not in out and "print" not in out
    assert "Intro." in out and "Outro." in out


def test_chunk_splits_long_sentence_under_cap():
    cap = vox.Engine.MAX_CHUNK_CHARS
    long_sentence = ", ".join(["clause number %d here" % i for i in range(40)]) + "."
    chunks = vox.Engine._chunk_text(long_sentence)
    assert chunks, "expected at least one chunk"
    assert all(len(c) <= cap for c in chunks), "every chunk must fit the cap"


def test_chunk_does_not_merge_across_paragraphs():
    chunks = vox.Engine._chunk_text("First para.\n\nSecond para.")
    # the paragraph boundary must fall between chunks, never inside one
    assert not any("First" in c and "Second" in c for c in chunks)


def test_chunk_short_text_is_single_chunk():
    assert vox.Engine._chunk_text("Hello there.") == ["Hello there."]


# -- cloned voices (vox_moss) ------------------------------------------------ #

def test_voice_name_rules():
    for ok in ("alex", "alex-calm", "me_2", "a", "x" * 32):
        assert vox_moss.validate_name(ok) == ok
    for bad in ("", "Alex", "my voice", "../x", "a.b", "-lead", "x" * 33, "af bella"):
        try:
            vox_moss.validate_name(bad)
        except ValueError:
            continue
        raise AssertionError(f"expected {bad!r} to be rejected")


def test_list_voices_only_sees_wav_clips():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / "alex.wav").write_bytes(b"")
        (d / "bea.wav").write_bytes(b"")
        (d / "alex.abc123.codes.json").write_text("[]")      # cache, not a voice
        (d / "notes.txt").write_text("")
        (d / "Bad Name.wav").write_bytes(b"")                 # invalid name is ignored
        assert list(vox_moss.list_voices(d)) == ["alex", "bea"]
        assert vox_moss.is_custom_voice("alex", d) and not vox_moss.is_custom_voice("zed", d)
    assert vox_moss.list_voices(Path("/nonexistent/vox-voices")) == {}


def test_remove_voice_deletes_clip_and_cache():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / "alex.wav").write_bytes(b"")
        (d / "alex.abc123.codes.json").write_text("[]")
        assert vox_moss.remove_voice("alex", d) is True
        assert not any(d.iterdir())
        assert vox_moss.remove_voice("alex", d) is False   # already gone


def test_engine_routing():
    route = vox.Engine._use_moss                              # (mode, voice, is a moss voice?)
    assert route("auto", "af_bella", False) is False         # stock Kokoro voice
    assert route("auto", "alex", True) is True               # installed clip
    assert route("auto", "Ava", True) is True                # built-in preset, no --engine needed
    assert route("auto", "alex", False) is False             # unknown name: not ours
    assert route("auto", "af_bella", True) is False          # a stock id always wins in auto
    assert route("clone", "af_bella", False) is True         # forced engine
    assert route("moss", "af_bella", False) is True          # legacy alias still forces it
    assert route("kokoro", "alex", True) is False
    assert route("say", "alex", True) is False


def test_presets_match_case_insensitively_and_are_not_custom():
    assert vox_moss.preset_name("Ava") == "Ava" == vox_moss.preset_name("ava")
    assert vox_moss.is_preset("nathan") and vox_moss.preset_name("zed") is None
    assert not vox_moss.is_preset("Trump")                   # deliberately off the roster
    with tempfile.TemporaryDirectory() as d:
        assert vox_moss.is_moss_voice("Ava", Path(d)) and not vox_moss.is_custom_voice("Ava", Path(d))


def test_speak_falls_back_to_default_voice_when_moss_fails():
    """The 'never silent' promise, end to end through Engine.speak with every
    external effect stubbed: MOSS says it couldn't, Kokoro must be asked for the
    default voice, and nothing real is synthesized or played."""
    eng = vox.Engine(engine="clone", quiet=True)
    eng._speak_moss = lambda text, voice, speed: False
    eng._ensure_loaded = lambda: True
    eng._np = np
    asked = []
    eng._synth_kokoro = lambda text, voice, speed: (asked.append(voice), (np.zeros(0, dtype=np.float32), 24_000))[1]
    eng._say = lambda text, speed: None                     # empty samples route here; stubbed
    eng.speak("hello there", "alex", 1.0)
    assert asked == [vox.DEFAULT_VOICE]


def test_speak_does_not_replay_when_moss_spoke():
    eng = vox.Engine(engine="auto", quiet=True)
    eng._speak_moss = lambda text, voice, speed: True
    eng._ensure_loaded = lambda: (_ for _ in ()).throw(AssertionError("Kokoro must not load"))
    real = vox._is_moss_voice
    vox._is_moss_voice = lambda v: v == "alex"
    try:
        eng.speak("hello", "alex", 1.0)
    finally:
        vox._is_moss_voice = real


def test_player_pads_silence_on_underrun_and_drains():
    p = vox_moss._Player.__new__(vox_moss._Player)          # no real audio device
    p.lock = threading.Lock()
    p.buf = [np.ones((3, 2), dtype=np.float32)]
    p.buffered = 3
    out = np.zeros((5, 2), dtype=np.float32)
    p._cb(out, 5, None, None)
    assert (out[:3] == 1.0).all() and (out[3:] == 0.0).all()   # underrun -> silence
    assert p.buf == [] and p.buffered == 0


def test_player_starts_only_after_prebuffer():
    class FakeStream:
        started = 0
        def start(self): self.started += 1
        def stop(self): pass
        def abort(self): pass
        def close(self): pass
    p = vox_moss._Player.__new__(vox_moss._Player)
    p.sr, p.ch, p.prebuffer = 48_000, 2, 100
    p.lock, p.buf, p.buffered = threading.Lock(), [], 0
    p.aborted = p._started = p._closed = False
    p.stream = FakeStream()
    p.push(np.zeros((60, 2), dtype=np.float32))
    assert p.stream.started == 0                             # 60 < 100 queued: wait
    p.push(np.zeros((60, 2), dtype=np.float32))
    assert p.stream.started == 1                             # 120 >= 100: go
    p.push(np.zeros((60, 2), dtype=np.float32))
    assert p.stream.started == 1                             # never restarted
    p.abort()
    p.finish()                                               # no-op after abort, no error


def test_add_voice_rejects_reserved_and_bad_clips_without_side_effects():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / "alex.wav").write_bytes(b"old-good-clip")
        src = d / "src.m4a"; src.write_bytes(b"x")
        orig = (vox_moss._convert_clip, vox_moss._clip_seconds, vox_moss.ensure_deps, vox_moss._encode_in_child)
        vox_moss._convert_clip = lambda source, dest: (dest.write_bytes(b"short"), 1.0)[1]  # < MIN
        vox_moss._clip_seconds = lambda path: None
        vox_moss.ensure_deps = lambda **kw: True
        vox_moss._encode_in_child = lambda wav, quiet=False: (_ for _ in ()).throw(AssertionError("must not encode"))
        try:
            for name, err in (("af_bella", "stock voice"), ("alex", "at least")):
                try:
                    vox_moss.add_voice(name, src, quiet=True, voices_dir=d, reserved={"af_bella"})
                except ValueError as exc:
                    assert err in str(exc)
                else:
                    raise AssertionError(f"{name}: expected ValueError")
            assert (d / "alex.wav").read_bytes() == b"old-good-clip"     # untouched
            assert sorted(q.name for q in d.iterdir()) == ["alex.wav", "src.m4a"]  # no leftovers
        finally:
            vox_moss._convert_clip, vox_moss._clip_seconds, vox_moss.ensure_deps, vox_moss._encode_in_child = orig


def _stub_add_voice(convert_bytes=b"ok", secs=12.0, encode=None):
    """Patch add_voice's collaborators; returns the originals for the finally block."""
    orig = (vox_moss._convert_clip, vox_moss._clip_seconds, vox_moss.ensure_deps, vox_moss._encode_in_child)
    vox_moss._convert_clip = lambda source, dest: (dest.write_bytes(convert_bytes), secs)[1]
    vox_moss._clip_seconds = lambda path: None
    vox_moss.ensure_deps = lambda **kw: True
    vox_moss._encode_in_child = encode or (lambda wav, quiet=False: None)
    return orig


def _restore_add_voice(orig):
    vox_moss._convert_clip, vox_moss._clip_seconds, vox_moss.ensure_deps, vox_moss._encode_in_child = orig


def _boom(wav, quiet=False):
    raise RuntimeError("encoder exploded")


def test_add_voice_failure_leaves_no_new_voice_behind():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        src = d / "src.m4a"; src.write_bytes(b"x")
        orig = _stub_add_voice(encode=_boom)
        try:
            try:
                vox_moss.add_voice("alex", src, quiet=True, voices_dir=d)
            except RuntimeError:
                pass
            else:
                raise AssertionError("expected the encode failure to propagate")
            assert not vox_moss.is_custom_voice("alex", d)          # nothing half-installed
            assert sorted(q.name for q in d.iterdir()) == ["src.m4a"]
        finally:
            _restore_add_voice(orig)


def test_add_voice_failed_readd_keeps_the_old_voice():
    """The data-loss case: re-adding an existing name must not destroy the working
    voice when the new clip's encode fails."""
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / "alex.wav").write_bytes(b"old-good-clip")
        (d / "alex.abc123.codes.json").write_text("[[1]]")
        src = d / "src.m4a"; src.write_bytes(b"x")
        orig = _stub_add_voice(convert_bytes=b"new-clip", encode=_boom)
        try:
            try:
                vox_moss.add_voice("alex", src, quiet=True, voices_dir=d)
            except RuntimeError:
                pass
            else:
                raise AssertionError("expected the encode failure to propagate")
            assert (d / "alex.wav").read_bytes() == b"old-good-clip"      # still the old voice
            assert (d / "alex.abc123.codes.json").exists()                 # cache intact too
            assert vox_moss.is_custom_voice("alex", d)
        finally:
            _restore_add_voice(orig)


def test_add_voice_swaps_clip_and_cache_only_when_ready():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / "alex.wav").write_bytes(b"old")
        (d / "alex.old123.codes.json").write_text("[]")
        src = d / "src.m4a"; src.write_bytes(b"x")
        orig = _stub_add_voice(convert_bytes=b"new-clip",
                               encode=lambda wav, quiet=False: vox_moss._codes_cache_path(wav).write_text("[[7]]"))
        try:
            dest = vox_moss.add_voice("alex", src, quiet=True, voices_dir=d)
            assert dest.read_bytes() == b"new-clip"
            cache = vox_moss._codes_cache_path(dest)
            assert cache.exists() and cache.read_text() == "[[7]]"        # cache followed the swap
            assert not (d / "alex.old123.codes.json").exists()             # stale cache gone
            assert not any(q.name.startswith(".alex.new") for q in d.iterdir())
            assert list(vox_moss.list_voices(d)) == ["alex"]
        finally:
            _restore_add_voice(orig)


def test_parser_voice_management_flags():
    p = vox.build_parser()
    a = p.parse_args(["--add-voice", "alex", "clip.m4a"])
    assert a.add_voice == ["alex", "clip.m4a"]
    assert p.parse_args(["--remove-voice", "alex"]).remove_voice == "alex"
    assert p.parse_args(["--engine", "clone", "hi"]).engine == "clone"
    assert p.parse_args(["--engine", "moss", "hi"]).engine == "clone"   # legacy alias


def test_lazy_sessions_build_on_first_access():
    built = []
    class Runtime:                                            # the builder is a bound method,
        def build(self, path):                                # held weakly so it can't pin the runtime
            built.append(path); return 2
    rt = Runtime()
    lazy = vox_moss._LazySessions({"a": 1}, {"b": "path-b"}, rt.build)
    assert "a" in lazy and "b" in lazy and "c" not in lazy
    assert lazy["a"] == 1 and built == []
    assert lazy["b"] == 2 and built == ["path-b"]
    assert lazy["b"] == 2 and built == ["path-b"]            # built once
    try:
        lazy["c"]
    except KeyError:
        pass
    else:
        raise AssertionError("unknown session should raise KeyError")


def test_daemon_idle_exit_needs_quiet_queue_and_no_busy_job():
    d = vox.Daemon.__new__(vox.Daemon)                       # no threads started
    d.engine = types.SimpleNamespace(_current=None)
    d.jobs = vox.Queue()
    d.busy, d.last_active = False, 0.0
    late = vox.IDLE_TIMEOUT + 1
    assert d._idle_exit_due(now=late) is True                # idle: may exit
    d.busy = True
    assert d._idle_exit_due(now=late) is False               # cloned voice mid-read: stay
    d.busy = False
    d.jobs.put(object())
    assert d._idle_exit_due(now=late) is False               # work queued: stay
    d.jobs.get()
    d.engine._current = object()
    assert d._idle_exit_due(now=late) is False               # afplay playing: stay


def test_moss_unloads_only_when_idle_and_not_speaking():
    class FakeMoss:
        def __init__(self): self._lock = threading.Lock(); self.loaded = True; self.closed = False
        def close(self): self.closed = True
    eng = vox.Engine(engine="auto", quiet=True)
    assert eng.unload_moss_if_idle(timeout=10, now=1000.0) is False     # nothing loaded
    eng._moss = FakeMoss(); eng._moss_last_used = 1000.0
    assert eng.unload_moss_if_idle(timeout=10, now=1005.0) is False     # not idle long enough
    assert eng._moss is not None
    eng._moss._lock.acquire()                                            # "speaking"
    assert eng.unload_moss_if_idle(timeout=10, now=1020.0) is False
    eng._moss._lock.release()
    fake = eng._moss
    assert eng.unload_moss_if_idle(timeout=10, now=1020.0) is True      # idle and free: dropped
    assert eng._moss is None and fake.closed                             # and the model released


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} passed")
