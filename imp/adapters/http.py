from __future__ import annotations

import asyncio
import ipaddress
import socket
from types import TracebackType
from typing import Any, Self
from urllib.parse import urlparse

import httpx2 as httpx

from ..config import Config


async def validate_url(url: str) -> None:
    p = urlparse(url)
    if p.scheme not in {"http", "https"} or not p.hostname:
        raise ValueError(f"Only http(s) URLs are allowed: {url}")
    host = p.hostname
    try:
        addrs = {ipaddress.ip_address(host)}
    except ValueError:
        try:
            infos = await asyncio.to_thread(socket.getaddrinfo, host, None)
        except socket.gaierror as e:
            raise ValueError(f"DNS lookup failed for {host}") from e
        addrs = {ipaddress.ip_address(i[4][0]) for i in infos}
    if any(
        a.is_private
        or a.is_loopback
        or a.is_link_local
        or a.is_reserved
        or a.is_unspecified
        for a in addrs
    ):
        raise ValueError(f"Refusing to fetch internal address: {host}")


async def redirect_guard(request: httpx.Request) -> None:
    """httpx request hook: re-run SSRF validation on every redirect hop."""
    await validate_url(str(request.url))


class HttpClient:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.client: httpx.AsyncClient = httpx.AsyncClient(
            timeout=config.network_timeout,
            # httpx has no per-request event hooks — client-level is the only way
            # to observe redirect hops; the guard re-validates each one
            event_hooks={"request": [redirect_guard]},
        )

    async def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        follow_redirects: bool = False,
    ) -> str:
        async with self.client.stream(
            "GET",
            url,
            params=params,
            headers=headers,
            follow_redirects=follow_redirects,
        ) as response:
            response.raise_for_status()
            chunks: list[bytes] = []
            received = 0
            truncated = False
            async for chunk in response.aiter_bytes():
                remaining = self.config.max_http_bytes - received
                if len(chunk) > remaining:
                    chunks.append(chunk[:remaining])
                    truncated = True
                    break
                chunks.append(chunk)
                received += len(chunk)
            encoding = response.encoding or "utf-8"
            text = b"".join(chunks).decode(encoding, errors="replace")
        if truncated:
            text += (
                f"\n[truncated: response exceeded {self.config.max_http_bytes} bytes]"
            )
        return text

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.client.aclose()
