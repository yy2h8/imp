from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from openai import AsyncOpenAI

from .adapters import FileSystemAdapter, HttpClient, SessionWriter, UIAdapter
from .agent import Agent, Context, build_system_prompt
from .config import Config
from .tools import build_tools


@asynccontextmanager
async def build_agent(config: Config, ui: UIAdapter) -> AsyncIterator[Agent]:
    """Composition root: wire adapters, tools, and the agent.

    Owns the lifetimes of the HttpClient and SessionWriter resources.
    """

    fs = FileSystemAdapter(config.workspace)
    prompt_lock = asyncio.Lock()

    async def prompt_user(message: str, markdown: bool = True) -> str:
        # lock: concurrent asks/approvals must not interleave on the terminal
        async with prompt_lock:
            return await ui.ask(message, markdown)

    with SessionWriter(config.workspace) as session:
        async with (
            HttpClient(config) as http,
            AsyncOpenAI(
                api_key=config.api_key,
                base_url=config.base_url,
                timeout=config.network_timeout,
            ) as openai_client,
        ):
            tools = build_tools(
                config=config, fs=fs, prompt_user=prompt_user, http=http
            )
            system_prompt = build_system_prompt(
                str(config.workspace),
                fs.list_directory(level=1),
                tools,
                fs.list_skills(),
                fs.gather_project_context(),
            )
            context = Context(
                config=config, system_prompt=system_prompt, writer=session
            )
            yield Agent(
                config=config, tools=tools, client=openai_client, context=context
            )
