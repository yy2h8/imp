from __future__ import annotations

from pathlib import Path

import pytest

from imp.adapters import FileSystemAdapter
from imp.config import Config


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(api_key="test-key", workspace=tmp_path)


@pytest.fixture
def fs(config: Config) -> FileSystemAdapter:
    return FileSystemAdapter(config.workspace)
