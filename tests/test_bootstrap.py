from __future__ import annotations

import json
import os
import platform
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_agent import StubClient, function_call_item, message_item, response

from assistant.app import Session
from assistant.bootstrap import (
    ENV_BEGIN,
    ENV_END,
    Probe,
    load_template,
    needs_tailoring,
    prune_scratch,
    read_state,
    render_manual,
    run_bootstrap,
    tailor_manual,
    write_state,
)
from assistant.config import AssistantConfig
from assistant.main import startup
from imp.adapters import FileSystemAdapter
from imp.agent import Agent
from imp.config import Config
from imp.tools.ask import Ask
from imp.tools.fs import StrReplace

MINIMAL_TEMPLATE = (
    "# manual\n\n"
    "<!-- ENVIRONMENT:BEGIN -->\n"
    "## Environment\n\n"
    "old generated facts\n"
    "<!-- ENVIRONMENT:END -->\n\n"
    "## Operating Manual\n\nkeep this\n"
)


def make_probe(**overrides) -> Probe:
    fields = {
        "os_name": "Linux",
        "arch": "aarch64",
        "os_release": "test os",
        "kernel": "6.1.0",
        "python_version": "3.12.1",
        "python_path": "/usr/bin/python3",
        "tools": {"uv": "not found", "git": "git version 2.39"},
        "shell": "ash",
        "home": "/home/pi",
        "cwd": "/home/pi/assistant",
        "disk": "1.0 GB free of 8.0 GB",
        "ram": "512 MB",
        "workspace": "/home/pi/assistant",
        "has_openai_key": True,
        "has_brave_key": False,
        "network": "reachable",
        "probed_at": "2026-09-22T00:00:00+00:00",
    }
    fields.update(overrides)
    return Probe(**fields)


class TestProbe:
    def test_take_reports_live_facts(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.delenv("BRAVE_API_KEY", raising=False)
        probe = Probe.take(tmp_path)
        assert probe.os_name == platform.system()
        assert probe.arch
        assert probe.python_version
        assert probe.python_path
        assert probe.workspace == str(tmp_path)
        assert probe.has_openai_key is True
        assert probe.has_brave_key is False  # presence only — never the value
        assert set(probe.tools) == {"uv", "pip", "python3", "git", "curl", "wget"}

    def test_render_contains_facts_but_never_secret_values(self):
        text = make_probe().render()
        for needle in (
            "aarch64",
            "6.1.0",
            "512 MB",
            "OPENAI_API_KEY=yes",
            "BRAVE_API_KEY=no",
        ):
            assert needle in text

    def test_fingerprint_is_stable_for_identical_facts(self):
        assert make_probe().fingerprint() == make_probe().fingerprint()

    def test_fingerprint_ignores_probed_at(self):
        a = make_probe(probed_at="2026-01-01T00:00:00+00:00")
        b = make_probe(probed_at="2027-01-01T00:00:00+00:00")
        assert a.fingerprint() == b.fingerprint()

    def test_fingerprint_ignores_free_disk(self):
        a = make_probe(disk="9.9 GB free of 8.0 GB")
        b = make_probe(disk="1.0 GB free of 8.0 GB")
        assert a.fingerprint() == b.fingerprint()

    def test_fingerprint_changes_when_a_tool_appears(self):
        b = make_probe(tools={"uv": "uv 1.0"})
        assert make_probe().fingerprint() != b.fingerprint()


class TestRenderManual:
    def test_splices_environment_between_markers(self):
        manual = render_manual(MINIMAL_TEMPLATE, "- Fact: value")
        assert "- Fact: value" in manual
        assert "old generated facts" not in manual
        assert manual.index(ENV_BEGIN) < manual.index("- Fact: value")
        assert manual.index("- Fact: value") < manual.index(ENV_END)

    def test_preserves_hand_edits_outside_the_block(self):
        assert "keep this" in render_manual(MINIMAL_TEMPLATE, "- Fact: value")

    @pytest.mark.parametrize(
        "template",
        [
            "no markers here",
            f"{ENV_BEGIN}\nowner instructions without a closing marker",
            f"{ENV_END}\n{ENV_BEGIN}\nowner instructions with reversed markers",
        ],
    )
    def test_malformed_template_returned_verbatim(self, template):
        assert render_manual(template, "- Fact: value") == template

    def test_refresh_does_not_accumulate_blank_lines(self):
        rendered = render_manual(MINIMAL_TEMPLATE, "- Fact: value")
        assert render_manual(rendered, "- Fact: value") == rendered

    def test_header_outside_block_survives(self):
        template = "HEAD\n<!-- ENVIRONMENT:BEGIN -->x<!-- ENVIRONMENT:END -->\nTAIL"
        manual = render_manual(template, "facts")
        assert manual.startswith("HEAD\n")
        assert manual.endswith("TAIL")


class TestState:
    def test_roundtrip_and_merge(self, tmp_path):
        write_state(tmp_path, {"a": 1})
        write_state(tmp_path, {"b": 2})
        assert json.loads((tmp_path / "state.json").read_text()) == {"a": 1, "b": 2}

    def test_missing_file_reads_as_empty(self, tmp_path):
        assert read_state(tmp_path) == {}

    def test_corrupt_state_is_rejected(self, tmp_path):
        (tmp_path / "state.json").write_text("not json")
        with pytest.raises(ValueError, match="state.json"):
            read_state(tmp_path)

    def test_failed_state_replace_keeps_previous_state(self, tmp_path, monkeypatch):
        write_state(tmp_path, {"offset": 12, "pending_requests": ["waiting"]})

        def fail_replace(source, destination):
            raise OSError("disk failure")

        monkeypatch.setattr("assistant.bootstrap.os.replace", fail_replace)
        with pytest.raises(OSError, match="disk failure"):
            write_state(tmp_path, {"offset": 13, "pending_requests": []})
        assert read_state(tmp_path) == {"offset": 12, "pending_requests": ["waiting"]}
        assert list(tmp_path.iterdir()) == [tmp_path / "state.json"]


class TestRunBootstrap:
    @pytest.fixture
    def package_dir(self, tmp_path: Path) -> Path:
        package = tmp_path / "pkg"
        package.mkdir()
        (package / "AGENTS.md.template").write_text(MINIMAL_TEMPLATE)
        return package

    def test_first_run_writes_manual_and_state(self, tmp_path, package_dir):
        home = tmp_path / "home"
        home.mkdir()
        probe = make_probe()

        result = run_bootstrap(home, package_dir, probe=probe)

        assert result.changed is True
        assert result.probe is probe
        assert "512 MB" in (home / "AGENTS.md").read_text()
        state = json.loads((home / "state.json").read_text())
        assert state["fingerprint"] == probe.fingerprint()

    def test_mismatch_rewrites_manual(self, tmp_path, package_dir):
        home = tmp_path / "home"
        home.mkdir()
        run_bootstrap(home, package_dir, probe=make_probe())
        manual_before = (home / "AGENTS.md").read_text()

        # the machine changed underneath the stored manual
        result = run_bootstrap(home, package_dir, probe=make_probe(ram="256 MB"))

        assert result.changed is True
        manual_after = (home / "AGENTS.md").read_text()
        assert manual_after != manual_before
        assert "256 MB" in manual_after
        state = json.loads((home / "state.json").read_text())
        assert state["fingerprint"] == make_probe(ram="256 MB").fingerprint()

    def test_no_op_when_fingerprint_matches(self, tmp_path, package_dir):
        home = tmp_path / "home"
        home.mkdir()
        run_bootstrap(home, package_dir, probe=make_probe())
        (home / "AGENTS.md").write_text("hand-edited manual")

        result = run_bootstrap(home, package_dir, probe=make_probe())

        assert result.changed is False
        assert (home / "AGENTS.md").read_text() == "hand-edited manual"

    @pytest.mark.parametrize("force", [False, True])
    def test_refresh_preserves_owner_edits_and_requires_tailoring(
        self, tmp_path, package_dir, force
    ):
        run_bootstrap(tmp_path, package_dir, probe=make_probe())
        manual = tmp_path / "AGENTS.md"
        manual.write_text(manual.read_text().replace("keep this", "OWNER INSTRUCTION"))
        write_state(tmp_path, {"tailored": True, "offset": 42})

        result = run_bootstrap(
            tmp_path, package_dir, probe=make_probe(ram="256 MB"), force=force
        )

        assert result.changed
        assert "OWNER INSTRUCTION" in manual.read_text()
        assert "256 MB" in manual.read_text()
        assert needs_tailoring(read_state(tmp_path))
        assert read_state(tmp_path)["offset"] == 42

    def test_missing_manual_is_recreated_with_matching_fingerprint(
        self, tmp_path, package_dir
    ):
        probe = make_probe()
        write_state(tmp_path, {"fingerprint": probe.fingerprint(), "tailored": True})

        assert run_bootstrap(tmp_path, package_dir, probe=probe).changed
        assert "512 MB" in (tmp_path / "AGENTS.md").read_text()
        assert needs_tailoring(read_state(tmp_path))

    def test_load_template_missing_raises_runtime_error(self, tmp_path):
        with pytest.raises(RuntimeError, match="Cannot read template"):
            load_template(tmp_path / "no-such-dir")

    def test_unreadable_template_degrades_to_minimal_manual(self, tmp_path):
        """§6: bootstrap failures must not block startup."""
        home = tmp_path / "home"
        home.mkdir()

        result = run_bootstrap(home, package_dir=tmp_path / "no-such-pkg")

        assert result.changed is True
        manual = (home / "AGENTS.md").read_text()
        assert "packaged template was unreadable" in manual
        assert "- Host:" in manual  # the fresh probe facts are still spliced in


class TestPruneScratch:
    def test_removes_only_old_files(self, tmp_path):
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        old = scratch / "old.txt"
        new = scratch / "new.txt"
        old.write_text("old")
        new.write_text("new")
        past = time.time() - 30 * 86400
        os.utime(old, (past, past))

        assert prune_scratch(tmp_path, ttl_days=7) == 1
        assert not old.exists()
        assert new.exists()

    def test_keeps_directories(self, tmp_path):
        (tmp_path / "scratch").mkdir()
        (tmp_path / "scratch" / "keep").mkdir()
        assert prune_scratch(tmp_path, ttl_days=7) == 0
        assert (tmp_path / "scratch" / "keep").is_dir()

    def test_missing_scratch_is_noop(self, tmp_path):
        assert prune_scratch(tmp_path, ttl_days=7) == 0


def test_needs_tailoring():
    assert needs_tailoring({}) is True
    assert needs_tailoring({"tailored": True}) is False


@pytest.fixture
def tailoring_app(tmp_path):
    config = Config(api_key="k", workspace=tmp_path, auto_approve=True)
    session = Session.open(config, "system prompt")
    client = StubClient([])
    fs = FileSystemAdapter(tmp_path)
    app = SimpleNamespace(
        config=config,
        agent=Agent(
            config=config,
            tools={StrReplace.name: StrReplace(config=config, fs=fs)},
            client=client,
            context=session.context,
        ),
    )
    (tmp_path / "AGENTS.md").write_text(MINIMAL_TEMPLATE)
    try:
        yield app
    finally:
        session.writer.__exit__(None, None, None)


async def test_tailor_manual_edits_file_and_marks_success(tailoring_app, tmp_path):
    client = tailoring_app.agent.client
    client.script = [
        response(
            [
                function_call_item(
                    "edit",
                    StrReplace.name,
                    {
                        "path": "AGENTS.md",
                        "old": "keep this",
                        "new": "Use small scripts.",
                    },
                )
            ]
        ),
        response([message_item("Manual adapted.")]),
    ]

    await tailor_manual(tailoring_app, make_probe())

    assert "Use small scripts." in (tmp_path / "AGENTS.md").read_text()
    assert read_state(tmp_path)["tailored"] is True
    assert "512 MB" in client.calls[0]["input"][1]["content"]


@pytest.mark.parametrize("reply", [RuntimeError("provider down"), response([])])
async def test_failed_tailoring_remains_pending(tailoring_app, tmp_path, reply):
    write_state(tmp_path, {"tailored": True})
    tailoring_app.agent.client.script = [reply]

    with pytest.raises(RuntimeError):
        await tailor_manual(tailoring_app, make_probe())

    assert needs_tailoring(read_state(tmp_path))


async def test_tailoring_cannot_ask_before_polling_starts(tailoring_app):
    questions = []

    async def prompt_user(question):
        questions.append(question)
        return "reply"

    ask = Ask(prompt_user=prompt_user)
    tailoring_app.agent.tools[Ask.name] = ask
    client = tailoring_app.agent.client
    client.script = [
        response(
            [function_call_item("question", Ask.name, {"question": "Which shell?"})]
        ),
        response([message_item("Used the probed facts.")]),
    ]

    await tailor_manual(tailoring_app, make_probe())

    assert questions == []
    assert Ask.name not in {tool["name"] for tool in client.calls[0]["tools"]}
    assert client.calls[1]["input"][-1]["output"] == f"Tool not found: {Ask.name}"
    assert tailoring_app.agent.tools[Ask.name] is ask


async def test_startup_retries_failed_tailoring_then_skips_success(
    tailoring_app, tmp_path, monkeypatch
):
    probe = make_probe()
    monkeypatch.setattr(Probe, "take", lambda workspace: probe)
    write_state(tmp_path, {"fingerprint": probe.fingerprint(), "tailored": False})
    config = AssistantConfig(
        bot_token="fake", allowed_user_ids=frozenset({7}), home=tmp_path
    )
    tailoring_app.assistant = config
    client = tailoring_app.agent.client
    client.script = [
        RuntimeError("provider down"),
        response([message_item("Adapted.")]),
    ]
    notices = []

    async def send_message(chat_id, text):
        notices.append(text)
        return 1

    async def send_text(chat_id, text):
        notices.append(text)
        return [1]

    tailoring_app.bot = SimpleNamespace(
        send_message=send_message, send_text=send_text
    )
    builds = []

    @asynccontextmanager
    async def build(config, chat_id):
        builds.append(chat_id)
        yield tailoring_app

    monkeypatch.setattr("assistant.main.build_assistant", build)

    await startup(config, 7, force=False)
    assert len(client.calls) == 1
    assert needs_tailoring(read_state(tmp_path))
    assert any("failed" in notice.lower() for notice in notices)
    await startup(config, 7, force=False)
    assert read_state(tmp_path)["tailored"] is True
    await startup(config, 7, force=False)
    assert len(client.calls) == 2
    assert builds == [7, 7]
