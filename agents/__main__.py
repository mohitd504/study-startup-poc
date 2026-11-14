"""Start the agents (the MCP server must already be running).

    python -m agents                      rules mode: free, no API key
    python -m agents --mode openai        LLM agents; needs OPENAI_API_KEY in .env
    python -m agents --once               process the current backlog, then exit
"""

import argparse
import asyncio
import os

from dotenv import load_dotenv

from agents.mcp_tools import open_tools
from agents.supervisor import ConnectionLost, Supervisor
from common.config import load_config

RECONNECT_SECONDS = 3


def specialist_factory(args, settings: dict):
    """Return (async make_specialists(tools), label, prices) for the chosen mode."""
    if args.mode == "rules":
        from agents.rules_engine import RULE_SPECIALISTS

        async def make_rule_specialists(tools):
            return dict(RULE_SPECIALISTS)
        return make_rule_specialists, "rules (no LLM)", None

    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY is not set. Add it to the .env file in the project folder.")
    from agents.llm_agents import build_model, build_specialists
    model_name = args.model or os.environ.get("AGENT_MODEL") or settings["openai"]["model"]
    model = build_model(model_name)
    prices = settings["openai"]["prices"].get(model_name)
    label = f"openai ({model_name})" + ("" if prices else ", no price on file so cost not estimated")

    # LLM agents are bound to the tools of one MCP session, so rebuild them per session.
    async def make_llm_specialists(tools):
        return await build_specialists(model, tools)
    return make_llm_specialists, label, prices


async def main_async(args) -> None:
    settings = load_config("agents")
    if args.concurrency:
        settings["concurrency"] = args.concurrency
    url = os.environ.get("MCP_URL") or settings["mcp_url"]
    make_specialists, label, prices = specialist_factory(args, settings)
    stats = {"processed": 0, "errors": 0, "cost": 0.0}
    connected_once = False
    try:
        while True:
            try:
                async with open_tools(url) as tools:
                    specialists = await make_specialists(tools)
                    routing = (await tools.rules())["routing"]
                    if not connected_once:
                        print(f"Agents running in {label} mode against {url}")
                        print(f"Specialists: {', '.join(specialists)}. Press Ctrl+C to stop.\n")
                    else:
                        print("[supervisor] reconnected to the MCP server")
                    connected_once = True
                    await Supervisor(tools, specialists, settings, routing, prices, stats).run(once=args.once)
                    return
            except ConnectionLost:
                print(f"[supervisor] lost the MCP server; reconnecting every {RECONNECT_SECONDS}s")
            except Exception as exc:
                if not connected_once:
                    raise SystemExit(
                        f"Cannot reach the MCP server at {url} ({type(exc).__name__}). "
                        "Start it first: python -m mcp_server.server"
                    ) from None
            await asyncio.sleep(RECONNECT_SECONDS)
    finally:
        cost = f", estimated cost ${stats['cost']:.4f}" if prices else ""
        print(f"\nProcessed {stats['processed']} events, {stats['errors']} errors{cost}.")


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="python -m agents", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=["rules", "openai"], default="rules")
    parser.add_argument("--model", help="OpenAI model (overrides AGENT_MODEL and config)")
    parser.add_argument("--once", action="store_true", help="process the backlog, then exit")
    parser.add_argument("--concurrency", type=int, help="events handled at the same time")
    args = parser.parse_args()
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
