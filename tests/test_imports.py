from __future__ import annotations

import subprocess
import sys


def test_core_imports_leave_terminal_ui_unloaded():
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
from imp.agent import Agent, Context
from imp.adapters import FileSystemAdapter, HttpClient, SessionWriter
assert 'imp.adapters.ui' not in sys.modules
assert 'prompt_toolkit' not in sys.modules
assert 'rich.console' not in sys.modules
from imp.adapters import UIAdapter
from imp.adapters.ui import UIAdapter as DirectUIAdapter
assert UIAdapter is DirectUIAdapter
""",
        ],
        check=True,
    )
