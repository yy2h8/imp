from __future__ import annotations

from pathlib import Path

import pytest

from assistant.tools.send_file import SendFile
from imp.config import Config


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    (outbox / "report.txt").write_text("hello")
    (tmp_path / "secret.env").write_text("KEY=1")
    (tmp_path / ".env").write_text("SECRET=1")
    (tmp_path / "adir").mkdir()
    return tmp_path


@pytest.fixture
def tool(workspace: Path):
    def make(sender) -> SendFile:
        config = Config(api_key="k", workspace=workspace)
        from imp.adapters import FileSystemAdapter

        return SendFile(config=config, fs=FileSystemAdapter(workspace), sender=sender)

    return make


def sent_results(sender):
    return sender.calls


class Sender:
    def __init__(self, message_id: int | None = 42):
        self.message_id = message_id
        self.calls: list[tuple[Path, str]] = []

    async def __call__(self, path: Path, caption: str) -> int | None:
        self.calls.append((path, caption))
        return self.message_id


class TestSandbox:
    async def test_escape_rejected(self, workspace, tool):
        sender = Sender()
        result = await tool(sender).execute("../outside.txt")
        assert result.ok is False
        assert "refused" in result.content
        assert sender.calls == []

    async def test_env_file_rejected(self, workspace, tool):
        sender = Sender()
        result = await tool(sender).execute(".env")
        assert result.ok is False
        assert "skip list" in result.content
        assert sender.calls == []


class TestValidation:
    async def test_missing_file(self, workspace, tool):
        result = await tool(Sender()).execute("outbox/nope.txt")
        assert result.ok is False
        assert "not found" in result.content.lower()

    async def test_directory_rejected(self, workspace, tool):
        result = await tool(Sender()).execute("adir")
        assert result.ok is False
        assert "not a file" in result.content.lower()


class TestSuccess:
    async def test_sends_resolved_path_and_caption(self, workspace, tool):
        sender = Sender()
        result = await tool(sender).execute("outbox/report.txt", caption="the report")
        assert result.ok is True
        assert "Sent" in result.content
        (path, caption) = sender.calls[0]
        assert path == workspace / "outbox" / "report.txt"
        assert caption == "the report"

    async def test_rejection_by_telegram_is_in_band(self, workspace, tool):
        result = await tool(Sender(message_id=None)).execute("outbox/report.txt")
        assert result.ok is False
        assert "rejected" in result.content.lower()

    async def test_no_filesystem_is_in_band_error(self, workspace):
        config = Config(api_key="k", workspace=workspace)
        tool_no_fs = SendFile(config=config, fs=None, sender=Sender())
        result = await tool_no_fs.execute("outbox/report.txt")
        assert result.ok is False
        assert "filesystem" in result.content.lower()
