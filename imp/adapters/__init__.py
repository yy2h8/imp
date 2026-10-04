from __future__ import annotations

from typing import TYPE_CHECKING

from .filesystem import FileSystemAdapter
from .http import HttpClient
from .session import SessionWriter

if TYPE_CHECKING:
    from .ui import UIAdapter


def __getattr__(name: str) -> type[UIAdapter]:
    if name == "UIAdapter":
        from .ui import UIAdapter

        return UIAdapter
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["FileSystemAdapter", "HttpClient", "SessionWriter", "UIAdapter"]
