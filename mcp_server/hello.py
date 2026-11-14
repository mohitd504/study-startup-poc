"""Hello-world MCP server (Stage 1): one tool, served over Streamable HTTP.

    python -m mcp_server.hello

Then open the MCP Inspector (npx @modelcontextprotocol/inspector), choose
transport "Streamable HTTP" and connect to http://127.0.0.1:8000/mcp.

Pinned to MCP SDK 1.x (FastMCP): langchain-mcp-adapters does not support 2.x yet.
"""

import argparse

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel

from common.db import connect

mcp = FastMCP(
    "study-startup-hello",
    instructions="Read-only access to the study start-up events table.",
)


class EventCounts(BaseModel):
    """A typed return value gives the tool an output schema and structured results."""

    total: int
    by_type: dict[str, int]


@mcp.tool()
def count_events(only_unprocessed: bool = False) -> EventCounts:
    """Count events in the events table, broken down by event type.

    Args:
        only_unprocessed: If true, count only events that no agent has handled yet.
    """
    where = "WHERE processed = 0" if only_unprocessed else ""
    with connect() as conn:
        rows = conn.execute(
            f"SELECT event_type, COUNT(*) AS n FROM events {where} "
            "GROUP BY event_type ORDER BY n DESC"
        ).fetchall()
    by_type = {r["event_type"]: r["n"] for r in rows}
    return EventCounts(total=sum(by_type.values()), by_type=by_type)


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m mcp_server.hello")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    print(f"MCP server on http://127.0.0.1:{args.port}/mcp  (Ctrl+C to stop)")
    mcp.settings.port = args.port
    mcp.run("streamable-http")


if __name__ == "__main__":
    main()
