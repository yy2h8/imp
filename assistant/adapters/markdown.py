"""Markdown → Telegram via telegramify-markdown.

`render` produces (text, entities) — sent with the entities parameter, no
parse_mode, so escaping can never break delivery. `render_long` handles
LLM-length output: entity-safe splitting, and fenced code blocks past
MIN_FILE_LINES lines are extracted as file attachments.
"""

from __future__ import annotations

from dataclasses import dataclass

from telegramify_markdown import convert as _convert
from telegramify_markdown import telegramify as _telegramify
from telegramify_markdown.config import get_runtime_config

_cfg = get_runtime_config()  # process-global; set once at import
_cfg.markdown_symbol.heading_level_1 = ""
_cfg.markdown_symbol.heading_level_2 = ""
_cfg.markdown_symbol.heading_level_3 = ""
_cfg.markdown_symbol.heading_level_4 = ""
_cfg.markdown_symbol.unordered_list_item = "-"

MAX_MESSAGE_CHARS = 4096  # Telegram's UTF-16 message cap
MAX_TEXT_CHARS = 4090  # headroom below the cap for the render_long split
MIN_FILE_LINES = 30  # fenced blocks with at least this many lines → file


@dataclass(slots=True, frozen=True)
class RenderedText:
    text: str
    entities: list[dict]


@dataclass(slots=True, frozen=True)
class RenderedFile:
    file_name: str
    file_data: bytes
    caption: str
    caption_entities: list[dict]


def _entity_dicts(entities) -> list[dict]:
    return [entity.to_dict() for entity in entities]


def render(text: str) -> tuple[str, list[dict]]:
    """Markdown in → (plain text, entity dicts) for one message."""
    rendered, entities = _convert(text)
    return rendered, _entity_dicts(entities)


async def render_long(
    text: str, max_message_length: int = MAX_TEXT_CHARS
) -> list[RenderedText | RenderedFile]:
    """Long markdown → ordered sendable items (texts and file attachments)."""
    items = await _telegramify(
        text,
        max_message_length=max_message_length,
        min_file_lines=MIN_FILE_LINES,
    )
    out: list[RenderedText | RenderedFile] = []
    for item in items:
        kind = getattr(item.content_type, "value", item.content_type)
        if kind == "text":
            out.append(RenderedText(item.text, _entity_dicts(item.entities)))
        else:  # extracted code file or rendered diagram: both go as documents
            out.append(
                RenderedFile(
                    file_name=item.file_name,
                    file_data=bytes(item.file_data),
                    caption=item.caption_text or "",
                    caption_entities=_entity_dicts(
                        item.caption_entities or []
                    ),
                )
            )
    return out
