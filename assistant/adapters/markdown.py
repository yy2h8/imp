"""Markdown → Telegram HTML rendering and message-size chunking.

Telegram understands a small HTML subset (parse_mode="HTML"), not markdown.
`to_html` escapes everything and then converts the constructs Telegram
supports; unknown syntax degrades to literal text, so rendering can never
break delivery. `chunks` splits markdown on line boundaries without cutting
fenced code blocks apart (forced splits close and reopen the fence), so every
chunk converts to standalone HTML.

Underscore emphasis is deliberately not converted: identifiers such as
snake_case must stay literal.
"""

from __future__ import annotations

import re
from html import escape

MAX_MESSAGE_CHARS = 4096  # Telegram's UTF-16 message cap

_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_HEADER = re.compile(r"^ {0,3}(#{1,6})\s+(.+?)\s*$")
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_BOLD = re.compile(r"\*\*(?=[^\s*])([^*\n]*[^\s*])\*\*")
_ITALIC = re.compile(r"(?<![\w*])\*(?=[^\s*])([^*\n]*[^\s*])\*(?![\w*])")
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
_LANGUAGE = re.compile(r"[\w+-]{1,32}")


def units(text: str) -> int:
    """UTF-16 code units: Telegram's size currency."""
    return len(text.encode("utf-16-le")) // 2


def _closes(fence: str, line: str) -> bool:
    match = _FENCE.match(line)
    return (
        match is not None
        and match.group(1)[0] == fence.lstrip()[0]
        and not match.group(2).strip()
    )


def _scan_fence(fence: str | None, line: str) -> str | None:
    """Fence state after a whole line: the opening fence line while inside
    a block, else None."""
    if fence is not None:
        return None if _closes(fence, line) else fence
    match = _FENCE.match(line)
    return match.group(0).rstrip() if match else None


def _pre(code: str, fence: str) -> str:
    match = _FENCE.match(fence)
    language = match.group(2).strip() if match else ""
    inner = escape(code, quote=False)
    if _LANGUAGE.fullmatch(language):
        return f'<pre><code class="language-{language}">{inner}</code></pre>'
    return f"<pre>{inner}</pre>"


def _link_tag(match: re.Match) -> str:
    # the URL is already &-escaped; attributes additionally need quotes gone
    url = match.group(2).replace('"', "&quot;")
    return f'<a href="{url}">{match.group(1)}</a>'


def _tags(text: str) -> str:
    """Escape plain markdown, then convert emphasis, code and links."""
    out = []
    for index, part in enumerate(_INLINE_CODE.split(text)):
        if index % 2:
            out.append(f"<code>{escape(part, quote=False)}</code>")
            continue
        part = escape(part, quote=False)
        part = _BOLD.sub(r"<b>\1</b>", part)
        part = _ITALIC.sub(r"<i>\1</i>", part)
        part = _LINK.sub(_link_tag, part)
        out.append(part)
    return "".join(out)


def _line_html(line: str) -> str:
    header = _HEADER.match(line)
    if header:
        return f"<b>{_tags(header.group(2))}</b>"
    return _tags(line)


def to_html(text: str) -> str:
    """Render markdown as Telegram HTML (parse_mode="HTML")."""
    out: list[str] = []
    fence: str | None = None
    code: list[str] = []
    for line in text.split("\n"):
        if fence is not None:
            if _closes(fence, line):
                out.append(_pre("\n".join(code), fence))
                fence, code = None, []
            else:
                code.append(line)
            continue
        match = _FENCE.match(line)
        if match:
            fence = match.group(0).rstrip()
            code = []
        else:
            out.append(_line_html(line))
    if fence is not None:  # unterminated block: flush what there is
        out.append(_pre("\n".join(code), fence))
    return "\n".join(out)


def _take(line: str, room: int) -> tuple[str, str]:
    """Split ``line`` after ``room`` UTF-16 units (room >= 2 admits any char)."""
    size = 0
    for index, char in enumerate(line):
        char_units = 2 if ord(char) > 0xFFFF else 1
        if size + char_units > room:
            return line[:index], line[index:]
        size += char_units
    return line, ""


def chunks(text: str, limit: int = MAX_MESSAGE_CHARS) -> list[str]:
    """Split markdown into chunks of at most ``limit`` UTF-16 units, cutting
    only on line boundaries and never inside a fenced code block: forced
    splits close the fence and reopen it in the next chunk. Oversized single
    lines are hard-split; empty input yields no chunks."""
    if limit < 2:
        raise ValueError("chunk limit must be at least 2")
    parts: list[str] = []
    buf: list[str] = []
    size = 0
    fence: str | None = None
    for line in text.split("\n"):
        if buf and size + 1 + units(line) > limit:
            if fence is None:
                parts.append("\n".join(buf))
                buf, size = [], 0
            else:  # cut lands inside a code block: close it, reopen above
                parts.append("\n".join(buf) + "\n" + fence.lstrip()[:3])
                buf, size = [fence], units(fence)
        while line and size + (1 if buf else 0) + units(line) > limit:
            room = max(limit - size - (1 if buf else 0), 2)
            head, line = _take(line, room)
            if fence is not None:
                parts.append("\n".join(buf + [head]) + "\n" + fence.lstrip()[:3])
                buf, size = [fence], units(fence)
            else:
                parts.append("\n".join(buf + [head]))
                buf, size = [], 0
        buf.append(line)
        size += (1 if len(buf) > 1 else 0) + units(line)
        fence = _scan_fence(fence, line)
    if buf:
        parts.append("\n".join(buf))
    return [part for part in parts if part]
