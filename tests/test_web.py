from __future__ import annotations

import socket

import pytest

from imp.adapters.http import validate_url
from imp.tools.fetch import _clean_html


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com",
        "not-a-url",
        "http://127.0.0.1/",
        "http://10.0.0.1/",
        "http://192.168.1.1/",
        "http://169.254.169.254/",
        "http://0.0.0.0/",
        "http://[::1]/",
    ],
)
async def test_refuses_non_public_addresses(url: str):
    with pytest.raises(ValueError):
        await validate_url(url)


async def test_allows_public_ip_literal():
    await validate_url("https://1.1.1.1/")  # literals skip DNS


def addrinfo(ip: str):
    return [(2, 1, 6, "", (ip, 0))]


async def test_hostname_resolving_to_private_refused(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port: addrinfo("10.0.0.1"))
    with pytest.raises(ValueError, match="internal"):
        await validate_url("http://example.com/")


async def test_hostname_resolving_to_public_allowed(monkeypatch):
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda host, port: addrinfo("93.184.216.34")
    )
    await validate_url("http://example.com/")


async def test_dns_failure(monkeypatch):
    def fail(host, port):
        raise socket.gaierror

    monkeypatch.setattr(socket, "getaddrinfo", fail)
    with pytest.raises(ValueError, match="DNS"):
        await validate_url("http://example.com/")


def test_clean_html_strips_noise():
    raw = (
        "<html><head><style>.x{color:red}</style></head><body>"
        "<script>alert(1)</script><!-- comment --><p>Hello   world</p></body></html>"
    )
    text = _clean_html(raw)
    assert "alert(1)" not in text
    assert "color:red" not in text
    assert "Hello world" in text
