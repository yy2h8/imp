"""build_assistant(): the composition root. Owns resource lifetimes and the
session holder (current Context + SessionWriter, resettable)."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from openai import AsyncOpenAI

from imp.adapters import FileSystemAdapter, HttpClient, SessionWriter
from imp.agent import Agent, Context, build_system_prompt
from imp.config import Config

from .adapters import SttClient, TelegramBot
from .adapters.telegram import send_text
from .config import OPENROUTER_BASE_URL, AssistantConfig
from .prompt import BASE_PROMPT
from .tools import build_assistant_tools
from .uploads import Uploads

HOME_DIRS = ("skills", "sessions", "scratch", "scripts", "outbox", "jobs", "inbox")

RESET_NOTICE = (
    "Context was nearly full — started a fresh session. "
    "The previous transcript is saved on disk."
)


def ensure_home(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    for name in HOME_DIRS:
        (home / name).mkdir(exist_ok=True)


class AskRouter:
    """Deliver an answer only to a question that is currently pending.

    New prompts belong to PollLoop's persistent FIFO, never to a future ask.
    """

    def __init__(self) -> None:
        self._future: asyncio.Future[str] | None = None

    @property
    def pending(self) -> bool:
        return self._future is not None and not self._future.done()

    def start(self) -> asyncio.Future[str]:
        self._future = asyncio.get_running_loop().create_future()
        return self._future

    @property
    def question(self) -> asyncio.Future[str] | None:
        return self._future if self.pending else None

    def deliver(self, text: str, question: asyncio.Future[str] | None = None) -> bool:
        if not self.pending or (question is not None and self._future is not question):
            return False
        self._future.set_result(text)
        return True

    def clear(self) -> None:
        if self.pending:
            self._future.cancel()
        self._future = None


@dataclass(slots=True)
class Session:
    """The one interactive conversation: current writer + context."""

    writer: SessionWriter
    context: Context

    @classmethod
    def open(cls, config: Config, system_prompt: str) -> Session:
        writer = SessionWriter(config.workspace, sessions_dir="sessions")
        writer.__enter__()
        context = Context(config=config, system_prompt=system_prompt, writer=writer)
        return cls(writer=writer, context=context)


@dataclass(slots=True)
class AssistantApp:
    """imp's Agent plus the assistant home concerns: session lifecycle,
    usage thresholds, the shared ask router, and the upload handler."""

    config: Config
    assistant: AssistantConfig
    agent: Agent
    session: Session
    bot: TelegramBot
    chat_id: int
    ask_router: AskRouter
    uploads: Uploads
    # ponytail: whole-turn lock; finer locks only if delayed jobs become unacceptable.
    execution_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def usage(self) -> tuple[int, int]:
        return self.session.context.get_usage()

    def should_reset(self) -> bool:
        used, maximum = self.usage
        return used >= maximum * self.assistant.reset_threshold

    def build_prompt(self) -> str:
        fs = FileSystemAdapter(
            self.config.workspace,
            skills_dir="skills",
            max_bytes=self.config.max_http_bytes,
        )
        return build_system_prompt(
            str(self.config.workspace),
            fs.list_directory(level=1),
            self.agent.tools,
            fs.list_skills(),
            fs.gather_project_context(),
            base_prompt=BASE_PROMPT,
        )

    def refresh_prompt(self) -> None:
        self.session.context.replace_system_prompt(self.build_prompt())

    def reset(self) -> str:
        """Hard reset: close the writer, open a fresh timestamped
        transcript, rebuild the context from current instructions and skills."""
        self.session.writer.__exit__(None, None, None)
        system_prompt = self.build_prompt()
        self.session = Session.open(self.config, system_prompt)
        self.agent.context = self.session.context
        return self.session.writer.path.name


@asynccontextmanager
async def build_assistant(assistant_config: AssistantConfig, chat_id: int):
    """Compose the agent for the owner chat; owns http/bot/client lifetimes."""
    ensure_home(assistant_config.home)
    imp_config = Config.from_env(workspace=assistant_config.home)
    imp_config.model = os.getenv("OPENAI_MODEL") or "openai/gpt-5-mini"
    imp_config.base_url = OPENROUTER_BASE_URL  # the only provider
    imp_config.auto_approve = (
        True  # trusted host automation with service-account permissions
    )

    fs = FileSystemAdapter(
        imp_config.workspace, skills_dir="skills", max_bytes=imp_config.max_http_bytes
    )

    prompt_lock = asyncio.Lock()
    ask_router = AskRouter()

    async def prompt_user(message: str, markdown: bool = True) -> str:
        # mutating-tool approvals auto-approve, so this is reached
        # only by the `ask` tool — send the question, await the owner's next
        # message; an empty answer means "use your best judgment".
        async with prompt_lock:
            future = ask_router.start()
            try:
                await send_text(bot, chat_id, message)
                return await future
            finally:
                ask_router.clear()

    bot = TelegramBot(assistant_config.bot_token, max_bytes=imp_config.max_http_bytes)
    try:
        async with (
            HttpClient(imp_config) as http,
            AsyncOpenAI(
                api_key=imp_config.api_key,
                base_url=imp_config.base_url,
                timeout=imp_config.network_timeout,
            ) as openai_client,
        ):

            async def sender(path: Path, caption: str) -> str | None:
                data = await asyncio.to_thread(fs.read_bytes, path)
                return await bot.send_document(
                    chat_id, data, filename=path.name, caption=caption
                )

            tools = build_assistant_tools(
                config=imp_config,
                fs=fs,
                prompt_user=prompt_user,
                http=http,
                sender=sender,
                tz=assistant_config.tz,
            )
            system_prompt = build_system_prompt(
                str(imp_config.workspace),
                fs.list_directory(level=1),
                tools,
                fs.list_skills(),
                fs.gather_project_context(),
                base_prompt=BASE_PROMPT,
            )
            session = Session.open(imp_config, system_prompt)
            app = None
            try:
                agent = Agent(
                    config=imp_config,
                    tools=tools,
                    client=openai_client,
                    context=session.context,
                )
                uploads = Uploads(
                    bot=bot,
                    inbox=assistant_config.home / "inbox",
                    fs=fs,
                    stt=SttClient(openai_client, assistant_config.stt_model),
                    chat_id=chat_id,
                )
                app = AssistantApp(
                    config=imp_config,
                    assistant=assistant_config,
                    agent=agent,
                    session=session,
                    bot=bot,
                    chat_id=chat_id,
                    ask_router=ask_router,
                    uploads=uploads,
                )
                yield app
            finally:
                (app.session if app else session).writer.__exit__(None, None, None)
    finally:
        await bot.close()
