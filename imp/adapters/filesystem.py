from __future__ import annotations

import difflib
import re
from pathlib import Path
from typing import ClassVar


def _is_text(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            chunk = fh.read(4096)
        if b"\x00" in chunk:
            return False
        try:
            chunk.decode("utf-8")
            return True
        except UnicodeDecodeError:
            # a multi-byte char may straddle the probe boundary; trim the
            # dangling tail and retry. A short chunk is the whole file, so
            # its decode errors are real.
            if len(chunk) < 4096:
                return False
            return any(_decodable(chunk[:-n]) for n in (1, 2, 3))
    except OSError:
        return False


def _decodable(data: bytes) -> bool:
    try:
        data.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def _fence_block(text: str) -> str:
    """Wrap text in a markdown fence longer than any backtick run inside it,
    so embedded fences cannot terminate the block early."""
    longest = max((len(m.group()) for m in re.finditer(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}markdown\n{text.rstrip()}\n{fence}"


def parse_skill_frontmatter(path: Path) -> tuple[str, str] | None:
    """Extract just the YAML frontmatter from a SKILL.md, without reading
    the full file body into memory."""
    with path.open("r", encoding="utf-8") as f:
        first = f.readline()
        if first.strip() != "---":
            return None  # no frontmatter, skip

        fields: dict[str, str] = {}
        for line in f:
            stripped = line.strip()
            if stripped == "---":
                break
            if ":" not in stripped:
                continue
            key, _, value = stripped.partition(":")
            fields[key.strip()] = value.strip().strip('"').strip("'")
        else:
            return None  # unterminated frontmatter, malformed skill

    if "name" not in fields or "description" not in fields:
        return None

    return fields["name"], fields["description"]


class FileSystemAdapter:
    SKIP_DIRS: ClassVar[set[str]] = {".git", "__pycache__"}
    SKIP_FILES: ClassVar[set[str]] = {".env"}
    CONTEXT_FILES = ("AGENTS.md", "CLAUDE.md")

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()

    def resolve_path(self, value: str | Path, must_exist: bool = False) -> Path:
        path = (self.workspace / value).resolve()
        try:
            rel = path.relative_to(self.workspace)
        except ValueError:
            raise ValueError(f"Path {path} escapes workspace {self.workspace}")

        if must_exist and not path.exists():
            raise FileNotFoundError(f"Path {path} does not exist")

        if any(
            p.lower() in self.SKIP_DIRS or p.lower() in self.SKIP_FILES
            for p in rel.parts
        ):
            raise ValueError(f"Path {path} is in the skip list")

        return path

    def list_directory(
        self,
        path: str | Path | None = None,
        level: int = 3,
        limit: int = 1000,
        *,
        _top: bool = True,
    ) -> list[str]:
        if path is None:
            path = self.workspace
        dir_path = self.resolve_path(path, must_exist=True)
        if not dir_path.is_dir():
            raise NotADirectoryError(f"Path {dir_path} is not a directory")

        entries: list[str] = []
        for entry in sorted(dir_path.iterdir()):
            if (
                entry.name.lower() in self.SKIP_DIRS
                or entry.name.lower() in self.SKIP_FILES
            ):
                continue
            if len(entries) >= limit:
                if _top:
                    entries.append(f"... (listing limited to {limit} entries)")
                break
            entries.append(entry.name + ("/" if entry.is_dir() else ""))
            if entry.is_dir() and level > 1:
                sub_entries = self.list_directory(
                    entry, level=level - 1, limit=limit - len(entries), _top=False
                )
                entries.extend(
                    [f"{entry.name}/{sub_entry}" for sub_entry in sub_entries]
                )
                if len(entries) >= limit:
                    if _top:
                        entries.append(f"... (listing limited to {limit} entries)")
                    break

        return entries

    def list_skills(self) -> list[tuple]:
        skills_dir = self.resolve_path(".imp/skills", must_exist=False)
        if not skills_dir.exists() or not skills_dir.is_dir():
            return []

        skills = []
        for f in skills_dir.glob("*/SKILL.md"):
            if not f.is_file():
                continue
            try:
                meta = parse_skill_frontmatter(f)
            except (OSError, UnicodeDecodeError, ValueError):
                meta = None
            if not meta:
                skills.append(
                    (str(f.parent.name),)
                )  # fallback to directory name if frontmatter is missing or malformed
                continue
            skills.append(meta)
        return skills

    def read_text_file(
        self,
        path: str | Path,
        start_line: int = 1,
        end_line: int | None = None,
        *,
        line_numbers: bool = False,
    ) -> str:
        file_path = self.resolve_path(path, must_exist=True)
        if file_path.is_dir():
            raise IsADirectoryError(f"Path {file_path} is a directory")
        if not _is_text(file_path):
            raise ValueError(f"File {file_path} appears to be binary or non-text")

        with file_path.open("r", encoding="utf-8") as fh:
            lines = fh.readlines()

        total = len(lines)
        start = max(1, start_line)
        end = max(end_line, 0) if end_line is not None else None
        selection = lines[start - 1 : end]

        if not line_numbers:
            return "".join(selection)

        body = "".join(f"{n:>6}: {line}" for n, line in enumerate(selection, start))
        if selection:
            footer = f"(lines {start}-{start + len(selection) - 1} of {total})"
        else:
            footer = f"(no lines in requested range; file has {total} lines)"
        if body and not body.endswith("\n"):
            body += "\n"
        return f"{body}{footer}\n"

    def gather_project_context(self) -> str:
        sections = []
        for name in self.CONTEXT_FILES:
            try:
                text = self.read_text_file(name)
            except (FileNotFoundError, IsADirectoryError, ValueError, OSError):
                continue
            if not text.strip():
                continue
            sections.append(
                f"### From `{name}` (user-provided project instructions):\n"
                f"{_fence_block(text)}"
            )
        return "\n\n".join(sections) if sections else ""

    def write_text_file(self, path: str, content: str) -> str:
        p = self.resolve_path(path)
        existed = p.exists()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        verb = "Overwrote" if existed else "Created"
        return (
            f"{verb} {path} ({len(content)} chars, {content.count(chr(10)) + 1} lines)."
        )

    def str_replace(self, path: str, old: str, new: str) -> str:
        p = self.resolve_path(path, must_exist=True)
        if not p.is_file():
            raise IsADirectoryError(f"Path {p} is not a file")

        old_text = p.read_text(encoding="utf-8")
        count = old_text.count(old)
        if count == 0:
            first = next((ln for ln in old.splitlines() if ln.strip()), "")
            close = difflib.get_close_matches(
                first, old_text.splitlines(), n=3, cutoff=0.6
            )
            hint = f" Closest lines in file: {close}." if close else ""
            raise ValueError(
                f"error: '{old}' not found in {p}.{hint} "
                "Re-read the file to get its exact current content and try again."
            )
        if count > 1:
            raise ValueError(
                f"error: '{old}' occurs {count} times in {p}; it must be unique. "
                "Include more surrounding context to make it unique."
            )

        new_text = old_text.replace(old, new, 1)
        p.write_text(new_text, encoding="utf-8")

        diff_lines = difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f"a/{p.name}",
            tofile=f"b/{p.name}",
            n=3,  # context lines around the change
        )
        return "".join(diff_lines)
