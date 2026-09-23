"""build_assistant(): the composition root. Owns resource lifetimes and the
session holder (current Context + SessionWriter, resettable per D10)."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from openai import AsyncOpenAI

from imp.adapters import FileSystemAdapter, HttpClient, SessionWriter
from imp.agent import Agent, Context, build_system_prompt
from imp.config import Config

from .adapters.telegram import TelegramBot
from .config import AssistantConfig
from .prompt import BASE_PROMPT
from .tools import build_assistant_tools

HOME_DIRS = ("skills", "sessions", "scratch", "scripts", "outbox", "jobs")

RESET_NOTICE = (
    "Context was nearly full — started a fresh session. "
    "The previous transcript is saved on disk."
)


def ensure_home(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    for name in HOME_DIRS:
        (home / name).mkdir(exist_ok=True)


class AskRouter:
    """Routes the owner's next message to a pending `ask` tool call.

    The poll loop hands every mid-turn message to deliver(); when a question
    is outstanding it becomes that ask's answer, otherwise it is held until
    one starts — a message that races the question resolves it instead of
    deadlocking the turn. When the turn ends without asking, take() promotes
    the held message to the next prompt (spec §4.1)."""

    def __init__(self) -> None:
        self._future: asyncio.Future[str] | None = None
        self._held: str | None = None

    def start(self) -> asyncio.Future[str]:
        self._future = asyncio.get_running_loop().create_future()
        if self._held is not None:  # the owner answered before we asked
            held, self._held = self._held, None
            self._future.set_result(held)
        return self._future

    def deliver(self, text: str) -> None:
        """Answer a pending ask, or hold the text for the next one."""
        if self._future is not None and not self._future.done():
            self._future.set_result(text)
        else:
            self._held = text

    def take(self) -> str | None:
        held, self._held = self._held, None
        return held

    def clear(self) -> None:
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


class AssistantApp:
    """imp's Agent plus the assistant home concerns: session lifecycle,
    usage thresholds, and the shared ask router."""

    def __init__(
        self,
        config: Config,
        assistant: AssistantConfig,
        agent: Agent,
        session: Session,
        bot: TelegramBot,
        chat_id: int,
        ask_router: AskRouter,
    ) -> None:
        self.config = config
        self.assistant = assistant
        self.agent = agent
        self.session = session
        self.bot = bot
        self.chat_id = chat_id
        self.ask_router = ask_router

    @property
    def usage(self) -> tuple[int, int]:
        return self.session.context.get_usage()

    def should_reset(self) -> bool:
        used, maximum = self.usage
        return used >= maximum * self.assistant.reset_threshold

    def reset(self) -> str:
        """Hard reset (D10): close the writer, open a fresh timestamped
        transcript, rebuild the context with the same system prompt."""
        self.session.writer.__exit__(None, None, None)
        system_prompt = self.session.context.messages[0].content
        self.session = Session.open(self.config, system_prompt)
        self.agent.context = self.session.context
        return self.session.writer.path.name


@asynccontextmanager
async def build_assistant(assistant_config: AssistantConfig, chat_id: int):
    """Compose the agent for the owner chat; owns http/bot/client lifetimes."""
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise ValueError(
            "OPENAI_API_KEY is not set. Export your provider API key and retry."
        )
    imp_config = Config(api_key=api_key, workspace=assistant_config.home)
    imp_config.auto_approve = True  # D3: the sandbox is the boundary in v1

    ensure_home(assistant_config.home)
    fs = FileSystemAdapter(imp_config.workspace, skills_dir="skills")

    prompt_lock = asyncio.Lock()
    ask_router = AskRouter()

    async def prompt_user(message: str, markdown: bool = True) -> str:
        # D3 seam: mutating-tool approvals auto-approve, so this is reached
        # only by the `ask` tool — send the question, await the owner's next
        # message; an empty answer means "use your best judgment".
        async with prompt_lock:
            future = ask_router.start()
            try:
                await bot.send_message(chat_id, message)
                return await future
            finally:
                ask_router.clear()

    bot = TelegramBot(assistant_config.bot_token)
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
                data = await asyncio.to_thread(path.read_bytes)
                return await bot.send_document(
                    chat_id, data, filename=path.name, caption=caption
                )

            tools = build_assistant_tools(
                config=imp_config,
                fs=fs,
                prompt_user=prompt_user,
                http=http,
                sender=sender,
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
            agent = Agent(
                config=imp_config,
                tools=tools,
                client=openai_client,
                context=session.context,
            )
            yield AssistantApp(
                config=imp_config,
                assistant=assistant_config,
                agent=agent,
                session=session,
                bot=bot,
                chat_id=chat_id,
                ask_router=ask_router,
            )
    finally:
        await bot.close()
