from __future__ import annotations

import json

from imp.adapters.session import SessionWriter
from imp.entities import TextMessage


def test_writes_jsonl(tmp_path):
    with SessionWriter(tmp_path) as writer:
        writer.write(TextMessage(role="user", content="hello"))

    files = list((tmp_path / ".imp" / "sessions").glob("*.jsonl"))
    assert len(files) == 1
    lines = files[0].read_text().splitlines()
    assert json.loads(lines[0]) == {"role": "user", "content": "hello"}


def test_disables_when_storage_unavailable(tmp_path, capsys):
    (tmp_path / ".imp").write_text("occupied")  # blocks sessions dir creation
    with SessionWriter(tmp_path) as writer:
        writer.write(TextMessage(role="user", content="x"))  # no-op, no crash
    assert "session persistence disabled" in capsys.readouterr().err
