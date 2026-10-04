from __future__ import annotations

import re
from typing import Any, ClassVar

from lxml import html
from lxml.html import clean

from .base import Tool, ToolResult


def _clean_html(raw_html: str) -> str:
    tree = html.fromstring(raw_html)

    cleaner = clean.Cleaner(
        scripts=True,
        style=True,
        comments=True,
        forms=True,
        annoying_tags=True,
        remove_unknown_tags=False,
        safe_attrs_only=True,
        page_structure=False,
    )

    cleaner(tree)

    for el in list(tree.xpath("//*[not(normalize-space()) and not(.//img)]")):
        el.drop_tree()

    text = html.tostring(tree, encoding="unicode", pretty_print=False)
    # lazy: collapses whitespace inside <pre> too — fine, this is model context, not fidelity
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\s*\n\s*", "\n", text)


class WebFetch(Tool):
    name = "web_fetch"
    description = "Fetch a specific web page content by URL."
    instructions = (
        "Plain HTTP GET; no JavaScript or login, and internal addresses are refused. "
        "Scripts, styles, and comments are removed. If a page requires interaction, "
        "report the limitation instead of repeating the same fetch."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "url": {
            "type": "string",
            "description": "URL of the web page to fetch.",
        }
    }
    required: ClassVar[list[str]] = ["url"]

    DEFAULT_HEADERS: ClassVar[dict[str, str]] = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    async def execute(self, url: str) -> ToolResult:
        # lazy: a truncation marker landing after a complete </html> is dropped by the
        # parser; the common mid-cut case keeps it. Upgrade: re-append marker in WebFetch.
        raw_html = await self.http.get(
            url, headers=self.DEFAULT_HEADERS, follow_redirects=True
        )
        return ToolResult(ok=True, content=_clean_html(raw_html))
