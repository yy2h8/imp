"""Durable memory tools over state.db."""

from __future__ import annotations

from pathlib import Path

import pytest

from assistant.db import STATE_DB_NAME, memory_all, open_db
from assistant.tools.memory import MemoryDelete, MemoryList, MemorySet
from imp.config import Config


@pytest.fixture
async def db(tmp_path):
    conn = await open_db(tmp_path / STATE_DB_NAME)
    yield conn
    await conn.close()


def tools(db):
    config = Config(api_key="k", workspace=Path("."))
    return (
        MemorySet(config=config, db=db),
        MemoryList(config=config, db=db),
        MemoryDelete(config=config, db=db),
    )


async def test_memory_set_and_list_round_trip(db):
    set_tool, list_tool, _ = tools(db)
    stored = await set_tool.execute(key="server", value="cubie, ssh port 2222")
    assert stored.ok
    listed = await list_tool.execute()
    assert listed.ok and "server: cubie, ssh port 2222" in listed.content
    assert await memory_all(db) == [("server", "cubie, ssh port 2222")]


async def test_memory_set_value_cap_returns_tool_error(db):
    set_tool, _, _ = tools(db)
    result = await set_tool.execute(key="large", value="x" * 3000)
    assert not result.ok and "2048" in result.content


async def test_memory_delete_missing_key_returns_error(db):
    _, _, delete_tool = tools(db)
    result = await delete_tool.execute(key="missing")
    assert not result.ok and "missing" in result.content


async def test_memory_digest_reflects_writes_newest_first(db):
    set_tool, _, _ = tools(db)
    await set_tool.execute(key="old", value="first")
    await set_tool.execute(key="new", value="second")
    listed = await tools(db)[1].execute()
    assert listed.content.index("new: second") < listed.content.index("old: first")
