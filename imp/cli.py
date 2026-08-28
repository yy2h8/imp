from __future__ import annotations

import argparse
import asyncio

from .adapters import UIAdapter
from .agent import Agent
from .app import build_agent
from .config import Config


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="imp",
        description="A compact OpenAI-compatible terminal coding assistant.",
        epilog="Configuration comes from environment variables only; see .env.example.",
    )
    p.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Allow file changes and shell commands without confirmation.",
    )
    return p


async def repl(agent: Agent, ui: UIAdapter) -> None:
    ui.banner()
    while True:
        try:
            prompt = (await ui.prompt()).strip()
        except EOFError:
            break
        except KeyboardInterrupt:
            continue  # Ctrl-C clears the prompt; Ctrl-D quits
        if not prompt:
            continue
        if prompt.lower() in {"exit", "quit"}:
            break
        try:
            async for event in agent.run_turn(prompt):
                ui.render_event(event)
            ui.end_turn(event.token_usage)  # one usage report per turn
        finally:
            ui.stop_spinner()  # Ctrl-C mid-turn must not leave a spinner running


async def _run(config: Config) -> None:
    ui = UIAdapter(config)
    async with build_agent(config, ui) as agent:
        await repl(agent, ui)


def main() -> None:
    args = parser().parse_args()
    try:
        config = Config.from_env(auto_approve=True if args.yes else None)
    except ValueError as e:
        raise SystemExit(f"Configuration error: {e}")
    try:
        asyncio.run(_run(config))
    except KeyboardInterrupt:
        pass  # Ctrl-C mid-turn: subprocess killed, httpx closed, session flushed


if __name__ == "__main__":
    main()
