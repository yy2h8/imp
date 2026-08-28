from __future__ import annotations

from pathlib import Path

import pytest

from imp.adapters.filesystem import (
    FileSystemAdapter,
    _fence_block,
    parse_skill_frontmatter,
)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "file.txt").write_text("one\ntwo\nthree\nfour\nfive\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("git config")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "m.pyc").write_text("bytecode")
    (tmp_path / ".env").write_text("SECRET=1")
    return tmp_path


@pytest.fixture
def adapter(workspace: Path) -> FileSystemAdapter:
    return FileSystemAdapter(workspace)


class TestResolvePath:
    def test_relative_path_inside_workspace(self, adapter, workspace):
        assert adapter.resolve_path("sub/file.txt") == workspace / "sub" / "file.txt"

    def test_absolute_path_inside_workspace(self, adapter, workspace):
        assert adapter.resolve_path(workspace / "sub") == workspace / "sub"

    def test_escape_rejected(self, adapter):
        with pytest.raises(ValueError, match="escapes workspace"):
            adapter.resolve_path("../outside.txt")

    @pytest.mark.parametrize(
        "name",
        sorted(FileSystemAdapter.SKIP_DIRS | FileSystemAdapter.SKIP_FILES),
    )
    def test_skipped_entries_rejected(self, adapter, name):
        with pytest.raises(ValueError, match="skip list"):
            adapter.resolve_path(f"{name}/whatever")
        with pytest.raises(ValueError, match="skip list"):
            adapter.resolve_path(f"sub/{name}")

    def test_missing_with_must_exist(self, adapter):
        with pytest.raises(FileNotFoundError):
            adapter.resolve_path("nope.txt", must_exist=True)


class TestListDirectory:
    def test_lists_nested_entries(self, adapter):
        entries = adapter.list_directory(level=2)
        assert "sub/" in entries
        assert "sub/file.txt" in entries

    def test_level_one_is_shallow(self, adapter):
        entries = adapter.list_directory(level=1)
        assert "sub/" in entries
        assert "sub/file.txt" not in entries

    def test_skips_hidden(self, adapter):
        entries = adapter.list_directory()
        assert not any(
            entry.split("/")[0]
            in FileSystemAdapter.SKIP_DIRS | FileSystemAdapter.SKIP_FILES
            for entry in entries
        )

    def test_limit_appends_message(self, adapter, workspace):
        (workspace / "zzz").mkdir()  # a second entry so the limit branch triggers
        entries = adapter.list_directory(level=1, limit=1)
        assert entries == ["sub/", "... (listing limited to 1 entries)"]

    def test_file_rejected(self, adapter):
        with pytest.raises(NotADirectoryError):
            adapter.list_directory("sub/file.txt")


class TestReadTextFile:
    def test_full_file(self, adapter):
        assert adapter.read_text_file("sub/file.txt") == "one\ntwo\nthree\nfour\nfive\n"

    def test_line_range(self, adapter):
        assert adapter.read_text_file("sub/file.txt", 2, 3) == "two\nthree\n"

    def test_binary_rejected(self, workspace, adapter):
        (workspace / "blob.bin").write_bytes(b"\x00\x01\x02")
        with pytest.raises(ValueError, match="binary"):
            adapter.read_text_file("blob.bin")

    def test_utf8_multibyte_at_chunk_boundary(self, workspace, adapter):
        """A multi-byte char straddling the 4096-byte probe must not fail the
        text check (regression: long arrow "⟶" read as binary)."""
        content = "a" * 4094 + "⟶\nmore text\n"
        (workspace / "arrows.txt").write_text(content, encoding="utf-8")
        assert adapter.read_text_file("arrows.txt") == content

    def test_truncated_utf8_rejected(self, workspace, adapter):
        (workspace / "broken.txt").write_bytes(b"ok \xe2\x9f")
        with pytest.raises(ValueError, match="binary"):
            adapter.read_text_file("broken.txt")

    def test_directory_rejected(self, adapter):
        with pytest.raises(IsADirectoryError):
            adapter.read_text_file("sub")

    def test_line_numbers_full(self, adapter):
        assert adapter.read_text_file("sub/file.txt", line_numbers=True) == (
            "     1: one\n     2: two\n     3: three\n     4: four\n     5: five\n"
            "(lines 1-5 of 5)\n"
        )

    def test_line_numbers_range(self, adapter):
        assert adapter.read_text_file("sub/file.txt", 2, 3, line_numbers=True) == (
            "     2: two\n     3: three\n(lines 2-3 of 5)\n"
        )

    def test_line_numbers_out_of_range_reports_total(self, adapter):
        assert adapter.read_text_file("sub/file.txt", 10, 20, line_numbers=True) == (
            "(no lines in requested range; file has 5 lines)\n"
        )

    def test_line_numbers_missing_trailing_newline(self, workspace, adapter):
        (workspace / "partial.txt").write_text("a\nb")
        assert adapter.read_text_file("partial.txt", line_numbers=True) == (
            "     1: a\n     2: b\n(lines 1-2 of 2)\n"
        )


def test_write_then_overwrite(adapter, workspace):
    assert adapter.write_text_file("new.txt", "hello").startswith("Created")
    assert (workspace / "new.txt").read_text() == "hello"
    assert adapter.write_text_file("new.txt", "again").startswith("Overwrote")


class TestStrReplace:
    def test_returns_diff_and_rewrites(self, adapter, workspace):
        diff = adapter.str_replace("sub/file.txt", "two", "TWO")
        assert (workspace / "sub" / "file.txt").read_text().startswith("one\nTWO")
        assert "-two" in diff
        assert "+TWO" in diff

    def test_not_found(self, adapter):
        with pytest.raises(ValueError, match="not found"):
            adapter.str_replace("sub/file.txt", "nope", "x")

    def test_not_found_suggests_close_lines(self, adapter):
        with pytest.raises(ValueError, match=r"Closest lines in file: \['two'\]"):
            adapter.str_replace("sub/file.txt", "twp", "x")

    def test_not_unique(self, adapter):
        with pytest.raises(ValueError, match="unique"):
            adapter.str_replace("sub/file.txt", "\n", "x")


def _write_skill(workspace: Path, body: str) -> Path:
    skill_dir = workspace / "sk"
    skill_dir.mkdir(exist_ok=True)
    path = skill_dir / "SKILL.md"
    path.write_text(body)
    return path


class TestSkillFrontmatter:
    def test_valid(self, workspace):
        path = _write_skill(
            workspace, '---\nname: my-skill\ndescription: "does things"\n---\nbody'
        )
        assert parse_skill_frontmatter(path) == ("my-skill", "does things")

    def test_missing_fields(self, workspace):
        path = _write_skill(workspace, "---\nname: x\n---\nbody")
        assert parse_skill_frontmatter(path) is None

    def test_unterminated(self, workspace):
        path = _write_skill(workspace, "---\nname: x\ndescription: d")
        assert parse_skill_frontmatter(path) is None

    def test_no_frontmatter(self, workspace):
        path = _write_skill(workspace, "just text")
        assert parse_skill_frontmatter(path) is None


def test_fence_block_minimum_fence():
    assert _fence_block("plain").startswith("```markdown\n")


def test_fence_block_grows_past_embedded_backticks():
    block = _fence_block("text with ``` fence")
    assert block.startswith("````markdown\n")


def test_project_context_includes_all_context_files(workspace, adapter):
    for name in FileSystemAdapter.CONTEXT_FILES:
        (workspace / name).write_text(f"{name} body")
    context = adapter.gather_project_context()
    for name in FileSystemAdapter.CONTEXT_FILES:
        assert f"From `{name}`" in context


def test_project_context_empty_without_files(adapter):
    assert adapter.gather_project_context() == ""
