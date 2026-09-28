"""markdown: Telegram HTML rendering and fence-aware chunking."""

from __future__ import annotations

import pytest

from assistant.adapters.markdown import MAX_MESSAGE_CHARS, chunks, to_html, units


class TestToHtml:
    def test_plain_text_is_escaped(self):
        assert to_html("a < b & c > d") == "a &lt; b &amp; c &gt; d"

    def test_inline_code(self):
        assert to_html("run `make all` now") == "run <code>make all</code> now"

    def test_inline_code_content_is_escaped(self):
        assert to_html("`<script>`") == "<code>&lt;script&gt;</code>"

    def test_bold(self):
        assert to_html("**bold** and **a b**") == "<b>bold</b> and <b>a b</b>"

    def test_italic_single_asterisks(self):
        assert to_html("a *fine* point") == "a <i>fine</i> point"

    def test_italic_needs_non_space_edges(self):
        assert to_html("2 * 3 * 4") == "2 * 3 * 4"

    def test_snake_case_stays_literal(self):
        assert to_html("some_var_name and __dunder__") == (
            "some_var_name and __dunder__"
        )

    def test_italic_not_glued_to_word_characters(self):
        assert to_html("2*3*4") == "2*3*4"

    def test_link(self):
        assert to_html("see [docs](https://example.com/x?a=1)") == (
            'see <a href="https://example.com/x?a=1">docs</a>'
        )

    def test_link_ampersand_in_url_is_escaped(self):
        assert to_html("[x](https://e.com/?a=1&b=2)") == (
            '<a href="https://e.com/?a=1&amp;b=2">x</a>'
        )

    def test_header_renders_bold(self):
        assert to_html("## The Title") == "<b>The Title</b>"

    def test_fenced_code_block(self):
        text = "before\n```python\nprint('<hi>')\n```\nafter"
        assert to_html(text) == (
            'before\n<pre><code class="language-python">'
            "print('&lt;hi&gt;')</code></pre>\nafter"
        )

    def test_unterminated_fence_flushes(self):
        assert to_html("```\ncode") == "<pre>code</pre>"

    def test_markdown_inside_code_block_stays_literal(self):
        assert to_html("```\n**not bold**\n```") == (
            "<pre>**not bold**</pre>"
        )

    def test_emphasis_inside_code_span_stays_literal(self):
        assert to_html("`**not bold**`") == "<code>**not bold**</code>"

    def test_empty_text(self):
        assert to_html("") == ""


class TestChunks:
    def test_short_text_is_one_chunk(self):
        assert chunks("hello") == ["hello"]

    def test_empty_text_yields_no_chunks(self):
        assert chunks("") == []

    def test_lossless_line_boundaries(self):
        text = "\n".join(f"line {i} " + "x" * 50 for i in range(300))
        parts = chunks(text)
        assert "\n".join(parts) == text  # no fences: nothing added or lost
        assert all(units(part) <= MAX_MESSAGE_CHARS for part in parts)
        assert len(parts) > 1

    def test_never_splits_inside_a_fence(self):
        text = "intro\n```python\n" + "\n".join(f"print({i})" for i in range(400))
        text += "\n```\noutro"
        parts = chunks(text)
        assert len(parts) > 1
        assert parts[0].startswith("intro\n```python\n")
        assert parts[0].endswith("```")  # fence closed at the cut
        assert parts[1].startswith("```python\n")  # and reopened above
        assert parts[-1].endswith("```\noutro")

    def test_each_chunk_converts_to_valid_standalone_html(self):
        text = "```py\n" + "\n".join(f"print({i})" for i in range(500)) + "\n```"
        for part in chunks(text):
            html = to_html(part)
            assert html.count("<pre") == html.count("</pre>")

    def test_oversized_line_is_hard_split(self):
        text = "word " * 3000  # one 15000-unit line, no newlines
        parts = chunks(text)
        assert len(parts) > 1
        assert all(units(part) <= MAX_MESSAGE_CHARS for part in parts)
        assert "".join(part.rstrip() + " " for part in parts).startswith("word")

    def test_rejects_tiny_limits(self):
        with pytest.raises(ValueError):
            chunks("text", limit=1)
