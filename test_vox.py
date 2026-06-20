#!/usr/bin/env python3
"""Unit tests for vox's pure text handling — Markdown cleaning and chunking.
No Kokoro / audio needed. Run: ./.venv/bin/python test_vox.py  (or pytest)."""

import vox


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


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} passed")
