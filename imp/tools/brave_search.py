from __future__ import annotations

import json
from typing import Any, ClassVar

from .base import Tool, ToolResult


class WebSearch(Tool):
    name = "web_search"
    description = "Search the web for a specific query and return the top results."
    parameters: ClassVar[dict[str, Any]] = {
        "query": {
            "type": "string",
            "description": "The search query to use.",
        },
        "num_results": {
            "type": "integer",
            "description": "The number of top search results to return. Defaults to 10.",
        },
    }
    required: ClassVar[list[str]] = ["query"]

    BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"

    async def _brave_search(self, query: str, num_results: int) -> list[str]:
        params = {"q": query, "count": max(1, min(num_results, 20))}
        # override the shared browser Accept header — Brave rejects anything
        # that isn't application/json or */* with 422
        headers = {
            "X-Subscription-Token": self.config.brave_api_key,
            "Accept": "application/json",
        }
        # lazy: if a Brave response ever exceeds the http cap, the appended truncation
        # marker breaks json.loads and surfaces as a normal tool error — acceptable ceiling.
        response = await self.http.get(
            self.BRAVE_SEARCH_URL, params=params, headers=headers
        )
        data = json.loads(response)

        results = []
        for item in data.get("web", {}).get("results", []):
            title = item.get("title", "")
            snippet = item.get("snippet", "")
            link = item.get("url", "")
            results.append(f"{title}\n{snippet}\n{link}")

        return results

    async def execute(self, query: str, num_results: int = 10) -> ToolResult:
        results = await self._brave_search(query, num_results=num_results)
        return ToolResult(ok=True, content="\n\n".join(results))
