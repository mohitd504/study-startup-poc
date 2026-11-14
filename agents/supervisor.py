"""Supervisor loop: poll for new events through MCP and run each through the graph.

- Polls get_unprocessed_events every few seconds.
- Handles several events at once (asyncio, limited by `concurrency`).
- Never starts an event twice: it skips events already in flight, and
  mark_event_processed refuses an event that is already processed.
- If an event fails or times out, it logs the error, marks the event
  processed and carries on. The loop itself does not crash.
"""

import asyncio
import time
from datetime import datetime

from agents.graph import SUPERVISOR, build_graph
from agents.mcp_tools import McpTools, ToolCallError

MAX_POLL_FAILURES = 3


class ConnectionLost(Exception):
    """Polling failed repeatedly: the MCP session is probably gone."""


class Supervisor:
    def __init__(self, tools: McpTools, specialists: dict, settings: dict, routing: dict,
                 prices: dict | None = None, stats: dict | None = None):
        self.tools = tools
        self.graph = build_graph(specialists, tools, routing)
        self.settings = settings
        self.prices = prices  # USD per 1M tokens, for the cost estimate (OpenAI mode)
        self.in_flight: set[int] = set()
        self.tasks: set[asyncio.Task] = set()
        self.stats = stats if stats is not None else {"processed": 0, "errors": 0, "cost": 0.0}

    async def run(self, once: bool = False) -> None:
        """Process events until stopped; with once=True, stop when the backlog is empty."""
        semaphore = asyncio.Semaphore(self.settings["concurrency"])
        failures = 0
        while True:
            try:
                batch = await self.tools.call("get_unprocessed_events", limit=self.settings["batch_size"])
                new = [e for e in batch["events"] if e["event_id"] not in self.in_flight]
                failures = 0
            except Exception as exc:
                failures += 1
                print(f"[supervisor] could not poll for events ({type(exc).__name__}: {exc})")
                if failures >= MAX_POLL_FAILURES:
                    await asyncio.gather(*self.tasks, return_exceptions=True)
                    raise ConnectionLost from exc
                new = []
            for event in new:
                self.in_flight.add(event["event_id"])
                task = asyncio.create_task(self._handle(event, semaphore))
                self.tasks.add(task)
                task.add_done_callback(self.tasks.discard)
            if once and not new and not self.in_flight:
                return
            await asyncio.sleep(self.settings["poll_seconds"])

    async def _handle(self, event: dict, semaphore: asyncio.Semaphore) -> None:
        async with semaphore:
            started = time.monotonic()
            try:
                state = await asyncio.wait_for(
                    self.graph.ainvoke({"event": event}), self.settings["event_timeout_seconds"]
                )
                self.stats["processed"] += 1
                self._print(event, state["route"], state["result"], time.monotonic() - started)
            except Exception as exc:
                self.stats["errors"] += 1
                await self._record_failure(event, exc)
            finally:
                self.in_flight.discard(event["event_id"])

    async def _record_failure(self, event: dict, exc: Exception) -> None:
        reason = "timed out" if isinstance(exc, TimeoutError) else f"{type(exc).__name__}: {exc}"
        print(f"#{event['event_id']:<5} {event['event_type']:<23} ERROR {reason}")
        try:
            await self.tools.call("log_agent_action", event_id=event["event_id"], agent_name=SUPERVISOR,
                                  action_type="error", reasoning=f"Handling failed: {reason}"[:2000],
                                  outcome="Error logged; event skipped")
            await self.tools.call("mark_event_processed", event_id=event["event_id"], agent_name=SUPERVISOR)
        except ToolCallError as inner:  # e.g. it was marked processed before the failure
            print(f"#{event['event_id']:<5} could not record the failure: {inner}")
        except Exception as inner:
            print(f"#{event['event_id']:<5} could not record the failure (MCP unreachable?): {inner}")

    def _print(self, event: dict, route: str, result: dict, seconds: float) -> None:
        lag = (datetime.now() - datetime.fromisoformat(event["created_at"])).total_seconds()
        line = (f"#{event['event_id']:<5} {event['event_type']:<23} -> {route:<20} "
                f"{result['outcome']:<12} {seconds:4.1f}s (picked up + done {lag:4.1f}s after event)")
        usage = result.get("usage")
        if usage:
            line += f"  tokens in {usage['input']} (cached {usage['cached_input']}) out {usage['output']}"
            if self.prices:
                cost = ((usage["input"] - usage["cached_input"]) * self.prices["input"]
                        + usage["cached_input"] * self.prices["cached_input"]
                        + usage["output"] * self.prices["output"]) / 1_000_000
                self.stats["cost"] += cost
                line += f"  ~${cost:.4f}"
        print(line)
        print(f"       {result['summary']}")
