from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import tempfile
from typing import IO, Any, ClassVar

from .base import Tool, ToolResult


def _decode(data: bytes | None) -> str:
    # asyncio subprocess is bytes-only (no text=True)
    return (data or b"").decode(errors="replace")


_TAIL_BYTES = 1_000_000  # ~10x agent's 100k-char truncation — invisible downstream


def _read_tail(file: IO[bytes]) -> bytes:
    # lazy: cap the tail rather than loading runaway output (e.g. `yes`);
    # agent-level truncation discards everything beyond this anyway
    file.seek(0, os.SEEK_END)
    size = file.tell()
    file.seek(max(0, size - _TAIL_BYTES))
    return file.read(_TAIL_BYTES)


def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    # kill the whole process group (grandchildren hold our pipes open);
    # Windows has no killpg — plain kill() is no worse than before
    if hasattr(os, "killpg"):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
    else:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()


class RunShell(Tool):
    name = "run_shell"
    description = "Run a shell command at the workspace root with timeout. May change the system. Returns the command's exit code, stdout, and stderr."
    instructions = "Use run_shell when other tools are insufficient. Make sure the command exists and is safe to run before executing it."
    mutating = True
    parameters: ClassVar[dict[str, Any]] = {
        "command": {"type": "string", "description": "The shell command to execute."},
        "timeout": {
            "type": "integer",
            "description": "Timeout in seconds for the command.",
        },
    }
    required: ClassVar[list[str]] = ["command"]

    async def execute(self, command: str, timeout: int | None = None) -> ToolResult:
        if not await self._ask_approval(
            f"Allow run_shell tool to execute command '{command}' (y/n)? "
        ):
            return ToolResult(
                ok=False, content=f"Execution of command '{command}' was not approved."
            )

        if timeout is None:
            timeout = max(1, min(self.config.command_timeout, 600))
        else:
            timeout = max(1, min(timeout, 600))
        # no pipes: asyncio's wait() is pipe-gated, so a backgrounded grandchild
        # holding the pipe open wedges the whole execute(); temp files make
        # wait() resolve on real process exit, output is read after it dies
        with (
            tempfile.TemporaryFile() as out_file,
            tempfile.TemporaryFile() as err_file,
        ):
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=self.config.workspace,
                stdout=out_file,
                stderr=err_file,
                start_new_session=True,  # own process group → killpg reaches grandchildren
            )
            try:
                await asyncio.wait_for(proc.wait(), timeout)
                timed_out = False
            except TimeoutError:
                timed_out = True
                _kill_tree(proc)
                await proc.wait()
            finally:
                if proc.returncode is None:  # cancelled mid-run: don't leak the child
                    _kill_tree(proc)
            stdout = _decode(_read_tail(out_file))
            stderr = _decode(_read_tail(err_file))
        if timed_out:
            return ToolResult(
                ok=False,
                content=f"Command timed out after {timeout}s. Partial output:\n{stdout}{stderr}",
            )

        result = json.dumps(
            {
                "command": command,
                "exit_code": proc.returncode,
                "stdout": stdout,
                "stderr": stderr,
            },
            ensure_ascii=False,
            indent=2,
        )
        return ToolResult(ok=True, content=result)
