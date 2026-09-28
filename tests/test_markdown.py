"""telegramify-markdown wrappers: entity rendering and long-message split."""

from __future__ import annotations

from assistant.adapters.markdown import (
    MAX_TEXT_CHARS,
    RenderedFile,
    RenderedText,
    render,
    render_long,
)


def test_render_bold_and_code_as_entities():
    text, entities = render("**b** `c`")
    assert text == "b c"
    assert [e["type"] for e in entities] == ["bold", "code"]


def test_render_fenced_block_is_pre_with_language():
    text, entities = render("```js\nvar x = 1\n```")
    pre = next(e for e in entities if e["type"] == "pre")
    assert pre["language"] == "js"
    assert "var x = 1" in text


def test_render_plain_text_has_no_entities():
    text, entities = render("just words")
    assert text == "just words"
    assert entities == []


async def test_render_long_splits_within_limit():
    long_md = "\n\n".join(f"paragraph {i} " + "word " * 60 for i in range(40))
    items = await render_long(long_md)
    assert len(items) >= 2
    assert all(isinstance(i, RenderedText) for i in items)
    for item in items:
        assert len(item.text.encode("utf-16-le")) // 2 <= MAX_TEXT_CHARS


async def test_render_long_extracts_big_code_block_as_file():
    long_md = (
        "explanation\n\n```python\n"
        + "\n".join(f"print({i})" for i in range(300))
        + "\n```"
    )
    items = await render_long(long_md)
    texts = [i for i in items if isinstance(i, RenderedText)]
    files = [i for i in items if isinstance(i, RenderedFile)]
    assert texts and files
    assert files[0].file_name.endswith(".py")
    assert isinstance(files[0].file_data, bytes)
    assert b"print(299)" in files[0].file_data
