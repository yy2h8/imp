from __future__ import annotations

import asyncio
import threading
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


class TestSkillsDirSeam:
    """The assistant seam (spec §3.1): skills live at <home>/skills/, not
    .imp/skills/ — a constructor argument, nothing else moves."""

    def test_default_stays_dot_imp_skills(self, workspace):
        adapter = FileSystemAdapter(workspace)
        assert adapter.skills_dir == ".imp/skills"
        assert adapter.list_skills() == []

    def test_custom_dir_is_discovered(self, workspace):
        skill_dir = workspace / "skills" / "weather"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            '---\nname: weather\ndescription: "forecast lookup"\n---\nbody'
        )
        adapter = FileSystemAdapter(workspace, skills_dir="skills")
        assert adapter.list_skills() == [
            ("weather", "forecast lookup", "skills/weather/SKILL.md")
        ]

    def test_imp_skills_ignored_when_seam_is_redirected(self, workspace):
        legacy = workspace / ".imp" / "skills" / "legacy"
        legacy.mkdir(parents=True)
        (legacy / "SKILL.md").write_text(
            '---\nname: legacy\ndescription: "old location"\n---\nbody'
        )
        adapter = FileSystemAdapter(workspace, skills_dir="skills")
        assert adapter.list_skills() == []


def test_skill_link_outside_is_not_discovered(tmp_path):
    from imp.adapters import FileSystemAdapter

    home = tmp_path / "home"
    skill = home / ".imp/skills/link"
    skill.mkdir(parents=True)
    secret = tmp_path / "secret"
    secret.write_text("---\nname: secret\ndescription: outside\n---")
    (skill / "SKILL.md").symlink_to(secret)
    assert FileSystemAdapter(home).list_skills() == []


def test_bounded_text_and_replace(tmp_path):
    from imp.adapters import FileSystemAdapter

    fs = FileSystemAdapter(tmp_path, max_bytes=16)
    (tmp_path / "huge").write_text("x" * 17)
    with pytest.raises(ValueError, match="limit"):
        fs.read_text_file("huge")
    with pytest.raises(ValueError, match="limit"):
        fs.str_replace("huge", "x", "y")


def test_paged_read_keeps_only_requested_range(tmp_path):
    fs = FileSystemAdapter(tmp_path, max_bytes=16)
    (tmp_path / "many").write_text("line\n" * 10000)
    assert fs.read_text_file("many", start_line=9000, end_line=9001) == "line\nline\n"


async def test_create_stream_writes_each_chunk_before_requesting_next(tmp_path):
    fs = FileSystemAdapter(tmp_path, max_bytes=6)

    async def chunks():
        yield b"abc"
        assert (tmp_path / "stream").stat().st_size == 3
        yield b"def"

    saved = await fs.create_stream("stream", chunks())
    assert saved.read_bytes() == b"abcdef"


@pytest.mark.parametrize("failure", [ValueError, RuntimeError, asyncio.CancelledError])
async def test_create_stream_removes_partial_file_on_failure(tmp_path, failure):
    fs = FileSystemAdapter(tmp_path, max_bytes=3)

    async def chunks():
        yield b"abc"
        if failure is ValueError:
            yield b"d"
        else:
            raise failure("download interrupted")

    with pytest.raises(failure):
        await fs.create_stream("stream", chunks())
    assert not (tmp_path / "stream").exists()


@pytest.mark.parametrize("symlink", [False, True])
async def test_create_stream_preserves_existing_files_and_symlinks(tmp_path, symlink):
    fs = FileSystemAdapter(tmp_path)
    original = tmp_path / "original"
    original.write_bytes(b"keep")
    target = tmp_path / "link" if symlink else original
    if symlink:
        target.symlink_to(original)

    async def chunks():
        pytest.fail("Existing destinations must fail before downloading")
        yield b"replacement"

    with pytest.raises(FileExistsError):
        await fs.create_stream(target, chunks())
    assert original.read_bytes() == b"keep"
    assert target.is_symlink() is symlink


async def test_create_stream_cancelled_while_opening_closes_and_removes_file(
    tmp_path, monkeypatch
):
    fs = FileSystemAdapter(tmp_path)
    entered, release = asyncio.Event(), threading.Event()
    loop, original_open = asyncio.get_running_loop(), Path.open
    handles = []

    def delayed_open(path, *args, **kwargs):
        stream = original_open(path, *args, **kwargs)
        handles.append(stream)
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5)
        return stream

    async def chunks():
        yield b"abc"

    monkeypatch.setattr(Path, "open", delayed_open)
    task = asyncio.create_task(fs.create_stream("stream", chunks()))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert handles[0].closed
    assert not (tmp_path / "stream").exists()


async def test_create_stream_rejects_short_write_and_removes_partial_file(
    tmp_path, monkeypatch
):
    fs = FileSystemAdapter(tmp_path)
    original_open = Path.open

    def short_open(path, *args, **kwargs):
        stream = original_open(path, *args, **kwargs)
        write = stream.write
        stream.write = lambda data: write(data[:1])
        return stream

    async def chunks():
        yield b"abc"

    monkeypatch.setattr(Path, "open", short_open)
    with pytest.raises(OSError, match="Incomplete"):
        await fs.create_stream("stream", chunks())
    assert not (tmp_path / "stream").exists()


def test_open_read_enforces_limit_after_file_growth(tmp_path):
    fs = FileSystemAdapter(tmp_path, max_bytes=6)
    path = tmp_path / "audio"
    path.write_bytes(b"abc")
    with fs.open_read("audio") as stream:
        assert stream.read(3) == b"abc"
        with path.open("ab") as writer:
            writer.write(b"defg")
        assert stream.read(3) == b"def"
        with pytest.raises(ValueError, match="byte limit"):
            stream.read(3)
    assert stream.closed
    with pytest.raises(ValueError, match="byte limit"):
        fs.open_read("audio")


def test_open_read_checks_sandbox_and_regular_file(tmp_path):
    fs = FileSystemAdapter(tmp_path)
    with pytest.raises(ValueError, match="regular file"):
        fs.open_read(tmp_path)
    with pytest.raises(ValueError, match="escapes workspace"):
        fs.open_read("../outside")
