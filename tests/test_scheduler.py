from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_agent import StubClient, function_call_item, message_item, response

from assistant.jobstore import (
    Job,
    advance,
    compute_next,
    load_jobs,
    next_due,
    save_job,
)
from assistant.scheduler import Scheduler
from imp.tools.ask import Ask
from imp.tools.fs import ReadFile

NOW = datetime(2026, 9, 22, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def close_test_sessions(monkeypatch):
    from assistant.app import Session

    original = Session.open
    writers = []

    def open_session(config, system_prompt):
        session = original(config, system_prompt)
        writers.append(session.writer)
        return session

    monkeypatch.setattr(Session, "open", open_session)
    yield
    for writer in writers:
        writer.__exit__(None, None, None)


def make_job(**overrides) -> Job:
    fields = {
        "id": "j1",
        "prompt": "say hi",
        "at": None,
        "every": None,
        "last_run": None,
        "next_run": None,
        "status": "pending",
    }
    fields.update(overrides)
    return Job(**fields)


def write_job_file(home: Path, job: Job, name: str | None = None) -> None:
    jobs = home / "jobs"
    jobs.mkdir(parents=True, exist_ok=True)
    path = jobs / f"{name or job.id}.json"
    path.write_text(json.dumps(job.to_dict(), indent=2))


class TestNextRunMath:
    def test_at_only_fires_once_at_the_timestamp(self):
        job = make_job(at=NOW + timedelta(hours=1))
        assert compute_next(job, NOW) == NOW + timedelta(hours=1)

    def test_every_only_repeats_from_last_run(self):
        job = make_job(every=3600, last_run=NOW - timedelta(minutes=10))
        assert compute_next(job, NOW) == NOW - timedelta(minutes=10) + timedelta(
            hours=1
        )

    def test_every_without_last_run_starts_from_now(self):
        assert compute_next(make_job(every=600), NOW) == NOW + timedelta(minutes=10)

    def test_explicit_next_run_wins(self):
        job = make_job(
            at=NOW + timedelta(hours=1),
            every=60,
            next_run=NOW + timedelta(days=2),
        )
        assert compute_next(job, NOW) == NOW + timedelta(days=2)

    def test_at_after_last_run_is_ignored(self):
        job = make_job(at=NOW + timedelta(days=1), last_run=NOW, every=None)
        assert compute_next(job, NOW) is None

    def test_overdue_is_still_due(self):
        job = make_job(at=NOW - timedelta(days=1))
        assert compute_next(job, NOW) == NOW - timedelta(days=1)

    def test_no_schedule_means_never(self):
        assert compute_next(make_job(), NOW) is None

    def test_non_pending_is_ignored(self):
        job = make_job(at=NOW - timedelta(hours=1), status="done")
        assert compute_next(job, NOW) is None

    def test_next_due_picks_earliest(self):
        soon = make_job(id="soon", at=NOW + timedelta(minutes=5))
        later = make_job(id="later", at=NOW + timedelta(hours=5))
        done = make_job(id="done", status="done", at=NOW + timedelta(minutes=1))
        due = next_due([later, done, soon], NOW)
        assert due is not None
        job, at = due
        assert job.id == "soon"
        assert at == NOW + timedelta(minutes=5)

    def test_next_due_none_when_nothing_pending(self):
        assert next_due([make_job(status="done", at=NOW)], NOW) is None
        assert next_due([], NOW) is None


class TestLoadSave:
    def test_roundtrip(self, tmp_path):
        job = make_job(
            at=datetime(2026, 9, 23, 8, 0, 0, tzinfo=UTC),
            every=None,
            last_run=None,
            next_run=None,
        )
        save_job(tmp_path, job)
        (tmp_path / "jobs" / "j1.json").exists()
        loaded = load_jobs(tmp_path)
        assert len(loaded) == 1
        assert loaded[0] == job

    def test_naive_timestamps_are_read_as_utc(self, tmp_path):
        write_job_file(
            tmp_path,
            make_job(),
            name="j1",
        )
        path = tmp_path / "jobs" / "j1.json"
        data = json.loads(path.read_text())
        data["at"] = "2026-09-23T08:00:00"  # naive = UTC per D8
        path.write_text(json.dumps(data))
        (loaded,) = load_jobs(tmp_path)
        assert loaded.at is not None
        assert loaded.at.tzinfo is UTC
        assert loaded.at == datetime(2026, 9, 23, 8, 0, 0, tzinfo=UTC)

    def test_skips_malformed_files(self, tmp_path):
        (tmp_path / "jobs").mkdir()
        (tmp_path / "jobs" / "bad.json").write_text("{not json")
        (tmp_path / "jobs" / "incomplete.json").write_text('{"id": "x"}')
        assert load_jobs(tmp_path) == []

    def test_skips_unsafe_ids(self, tmp_path):
        write_job_file(tmp_path, make_job(id="../escape"))
        assert load_jobs(tmp_path) == []

    def test_missing_jobs_dir(self, tmp_path):
        assert load_jobs(tmp_path) == []


class TestAdvance:
    def test_every_reschedules(self):
        job = make_job(every=3600)
        advance(job, NOW)
        assert job.last_run == NOW
        assert job.next_run == NOW + timedelta(hours=1)
        assert job.status == "pending"

    def test_at_one_shot_is_done(self):
        job = make_job(at=NOW)
        advance(job, NOW)
        assert job.status == "done"
        assert job.next_run is None


def make_app(tmp_path, client, bot):
    """A real AssistantApp whose agent runs against a scripted StubClient."""
    from assistant.app import AssistantApp, Session
    from assistant.config import AssistantConfig
    from imp.agent import Agent
    from imp.config import Config

    assistant_config = AssistantConfig(
        bot_token="t", allowed_user_ids=frozenset({7}), home=tmp_path
    )
    imp_config = Config(api_key="k", workspace=tmp_path)
    session = Session.open(imp_config, "system prompt")
    agent = Agent(
        config=imp_config,
        tools={},
        client=client,
        context=session.context,
    )
    from assistant.uploads import Uploads

    uploads = Uploads(bot=bot, inbox=tmp_path / "inbox", chat_id=7)
    return AssistantApp(
        config=imp_config,
        assistant=assistant_config,
        agent=agent,
        session=session,
        bot=bot,
        chat_id=7,
        ask_router=None,
        uploads=uploads,
    )


class _StubBot:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str):
        self.sent.append((chat_id, text))
        return 1


def test_run_job_delivers_answer_and_reschedules(tmp_path):
    client = StubClient([response([message_item("digest ready")])])
    bot = _StubBot()
    app = make_app(tmp_path, client, bot)
    scheduler = Scheduler(app)
    job = make_job(id="digest", every=3600)

    save_job(tmp_path, job)
    asyncio.run(scheduler.run_job(job))

    assert bot.sent == [(7, "⏰ digest\n\ndigest ready")]
    saved = json.loads((tmp_path / "jobs" / "digest.json").read_text())
    assert saved["status"] == "pending"
    assert saved["last_run"] is not None
    assert saved["next_run"] is not None

    loaded = load_jobs(tmp_path)
    assert loaded[0].next_run is not None
    assert loaded[0].next_run > datetime.now(UTC) - timedelta(seconds=5)


def test_run_job_empty_answer_sends_nothing(tmp_path):
    client = StubClient([response([message_item("")])])
    bot = _StubBot()
    app = make_app(tmp_path, client, bot)

    job = make_job(id="quiet", at=NOW)
    save_job(tmp_path, job)
    asyncio.run(Scheduler(app).run_job(job))

    assert bot.sent == []
    saved = json.loads((tmp_path / "jobs" / "quiet.json").read_text())
    assert saved["status"] == "done"  # one-shot completed


def test_run_job_error_event_marks_error_and_notifies(tmp_path):
    client = StubClient([RuntimeError("model broke")])
    bot = _StubBot()
    app = make_app(tmp_path, client, bot)
    job = make_job(id="doomed", every=60)

    save_job(tmp_path, job)
    asyncio.run(Scheduler(app).run_job(job))

    assert len(bot.sent) == 1
    assert "failed" in bot.sent[0][1]
    assert "model broke" in bot.sent[0][1]
    saved = json.loads((tmp_path / "jobs" / "doomed.json").read_text())
    assert saved["status"] == "error"
    assert saved["next_run"] is None


def test_run_job_runs_in_a_fresh_context(tmp_path):
    """The job never inherits or pollutes the interactive chat (§7)."""
    client = StubClient([response([message_item("answer")])])
    app = make_app(tmp_path, client, bot=_StubBot())

    job = make_job(id="t", at=NOW)
    save_job(tmp_path, job)
    asyncio.run(Scheduler(app).run_job(job))

    chat_messages = app.session.context.messages
    assert [m.role for m in chat_messages] == ["system"]  # untouched
    job_prompt = client.calls[0]["input"][1]  # system, then the job's prompt
    assert job_prompt == {"role": "user", "content": "say hi"}
    assert list((tmp_path / "sessions").glob("*.jsonl"))  # own transcript file


async def test_scheduled_job_cannot_ask_but_interactive_agent_can(tmp_path):
    questions = []

    async def prompt_user(question):
        questions.append(question)
        return "owner answer"

    client = StubClient(
        [
            response(
                [function_call_item("question", Ask.name, {"question": "Which file?"})]
            ),
            response(
                [message_item("Missing the file name; could not complete the job.")]
            ),
        ]
    )
    bot = _StubBot()
    app = make_app(tmp_path, client, bot)
    ask = Ask(prompt_user=prompt_user)
    app.agent.tools = {Ask.name: ask, ReadFile.name: ReadFile()}
    try:
        job = make_job(at=NOW)
        save_job(tmp_path, job)
        await Scheduler(app).run_job(job)

        assert questions == []
        assert {tool["name"] for tool in client.calls[0]["tools"]} == {ReadFile.name}
        output = client.calls[1]["input"][-1]
        assert output["type"] == "function_call_output"
        assert output["output"] == f"Tool not found: {Ask.name}"
        assert "could not complete" in bot.sent[-1][1]
        assert app.agent.tools[Ask.name] is ask
        assert (await app.agent.tools[Ask.name].execute("Interactive question?")).ok
        assert questions == ["Interactive question?"]
    finally:
        app.session.writer.__exit__(None, None, None)


def test_scheduler_cycle_sleeps_when_nothing_due(tmp_path):
    client = StubClient([])
    app = make_app(tmp_path, client, _StubBot())
    scheduler = Scheduler(app)
    future = make_job(id="later", at=datetime.now(UTC) + timedelta(hours=3))
    write_job_file(tmp_path, future)

    async def scenario():
        task = asyncio.create_task(scheduler._cycle())
        await asyncio.sleep(0.1)
        assert not task.done()  # sleeping until the job is due (capped at 1h)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())


def test_scheduler_run_survives_repeated_failures(tmp_path, monkeypatch):
    client = StubClient([])
    app = make_app(tmp_path, client, _StubBot())
    scheduler = Scheduler(app)

    async def boom():
        raise RuntimeError("cycle broke")

    scheduler._cycle = boom
    import assistant.scheduler as scheduler_module

    monkeypatch.setattr(scheduler_module, "IDLE_POLL_S", 0.01)

    async def scenario():
        task = asyncio.create_task(scheduler.run())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())  # reaching here means the loop survived


def test_job_dataclass_defaults():
    job = Job(id="x", prompt="p")
    assert job.status == "pending"
    assert job.at is None and job.every is None
    assert job.to_dict()["status"] == "pending"


def test_job_from_dict_rejects_non_integer_interval():
    with pytest.raises(ValueError):
        Job.from_dict({"id": "x", "prompt": "p", "every": "90"})


def test_job_roundtrip_via_dict():
    job = make_job(at=NOW, every=None, last_run=NOW, next_run=NOW)
    assert Job.from_dict(job.to_dict()) == job


@pytest.mark.parametrize(
    "kwargs",
    [
        {"at": "not a date"},
        {"every": "abc"},
        {"last_run": "garbage"},
    ],
)
def test_job_from_dict_bad_timestamps_raise_valueerror(kwargs):
    with pytest.raises(ValueError):
        Job.from_dict({"id": "x", "prompt": "p", **kwargs})


def test_bad_json_shape_does_not_starve_valid_job(tmp_path):
    save_job(tmp_path, make_job(at=NOW))
    (tmp_path / "jobs/bad.json").write_text("[]")
    assert [job.id for job in load_jobs(tmp_path)] == ["j1"]


def test_first_interval_deadline_survives_reload(tmp_path):
    save_job(tmp_path, make_job(every=60))
    first = load_jobs(tmp_path)[0]
    assert first.next_run is not None
    assert compute_next(load_jobs(tmp_path)[0], first.next_run) == first.next_run


async def test_old_completion_cannot_replace_new_job(tmp_path):
    app = make_app(tmp_path, StubClient([]), _StubBot())
    scheduler = Scheduler(app)
    old = make_job(every=60)
    save_job(tmp_path, old)

    async def replace_during_run(job):
        save_job(tmp_path, make_job(every=60, prompt="replacement"))
        return "old answer"

    scheduler._execute = replace_during_run
    await scheduler.run_job(old)
    assert load_jobs(tmp_path)[0].prompt == "replacement"


async def test_first_and_second_fire_through_cycles(tmp_path, monkeypatch):
    import assistant.jobstore as store
    import assistant.scheduler as module

    clock = [NOW]

    class Clock:
        @classmethod
        def now(cls, tz):
            return clock[0]

    monkeypatch.setattr(module, "datetime", Clock)
    monkeypatch.setattr(store, "datetime", Clock)
    # Timestamp parsing still uses real datetime.
    monkeypatch.setattr(
        store,
        "_parse_ts",
        lambda value: datetime.fromisoformat(value) if value else None,
    )
    app = make_app(tmp_path, StubClient([]), _StubBot())
    scheduler = Scheduler(app)
    starts = []

    async def execute(job):
        starts.append(clock[0])
        clock[0] += timedelta(seconds=15)
        return "done"

    scheduler._execute = execute
    job = make_job(every=60)
    save_job(tmp_path, job)
    clock[0] = NOW + timedelta(seconds=60)
    await scheduler._cycle()
    assert starts == [NOW + timedelta(seconds=60)]
    assert load_jobs(tmp_path)[0].next_run == NOW + timedelta(seconds=135)
    clock[0] = NOW + timedelta(seconds=135)
    await scheduler._cycle()
    assert len(starts) == 2


async def test_running_job_recovery_never_executes_again(tmp_path):
    job = make_job(at=NOW, status="running")
    save_job(tmp_path, job)
    app = make_app(tmp_path, StubClient([]), _StubBot())
    scheduler = Scheduler(app)

    async def stop():
        raise asyncio.CancelledError

    scheduler._cycle = stop
    with pytest.raises(asyncio.CancelledError):
        await scheduler.run()
    saved = load_jobs(tmp_path)[0]
    assert saved.status == "error"
    assert "Interrupted" in saved.result
    assert app.agent.client.calls == []
    assert "Interrupted" in app.bot.sent[0][1]


async def test_delivery_failure_preserves_result_without_rerun(tmp_path):
    class FailedBot:
        async def send_message(self, *args):
            return None

    app = make_app(
        tmp_path, StubClient([response([message_item("valuable result")])]), FailedBot()
    )
    job = make_job(at=NOW)
    save_job(tmp_path, job)
    await Scheduler(app).run_job(job)
    saved = load_jobs(tmp_path)[0]
    assert saved.status == "done"
    assert saved.result == "valuable result"
    assert saved.delivery_error
    assert saved.transcript
    assert next_due([saved], NOW) is None


async def test_cancelled_job_is_not_resurrected(tmp_path):
    from assistant.tools.schedule import UnscheduleJob

    app = make_app(tmp_path, StubClient([]), _StubBot())
    scheduler = Scheduler(app)
    job = make_job(every=60)
    save_job(tmp_path, job)

    async def cancel_during_run(job):
        result = await UnscheduleJob(config=app.config).execute(job.id)
        assert result.ok
        return "finished in flight"

    scheduler._execute = cancel_during_run
    await scheduler.run_job(job)
    assert load_jobs(tmp_path)[0].status == "cancelled"


async def test_identical_replacement_has_new_revision(tmp_path):
    from assistant.jobstore import save_if_current

    old = make_job(every=60)
    save_job(tmp_path, old)
    revision = old.revision
    replacement = make_job(every=60)
    save_job(tmp_path, replacement)
    old.status = "done"
    assert not save_if_current(tmp_path, old, revision)
    assert load_jobs(tmp_path)[0].revision == replacement.revision


@pytest.mark.parametrize(
    "data",
    [
        {"id": "x", "prompt": "p"},
        {"id": "x", "prompt": "p", "at": NOW.isoformat(), "every": 60},
        {"id": "x", "prompt": [], "every": 60},
    ],
)
def test_invalid_job_schema_rejected(data):
    with pytest.raises(ValueError):
        Job.from_dict(data)


async def test_scheduler_future_wait_is_bounded(tmp_path, monkeypatch):
    import assistant.scheduler as module

    app = make_app(tmp_path, StubClient([]), _StubBot())
    save_job(tmp_path, make_job(at=datetime.now(UTC) + timedelta(hours=3)))
    delays = []

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    await Scheduler(app)._cycle()
    assert delays == [module.IDLE_POLL_S]
