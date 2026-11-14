"""Test the hello-world MCP tool in-process, with no agent and no HTTP server."""

import pytest
from mcp import Client

from generator.live import generate_one, make_context
from generator.seed import run_seed
from mcp_server.hello import mcp


@pytest.fixture(autouse=True)
def fresh_db():
    run_seed(reset=True)


@pytest.mark.anyio
async def test_count_events_lists_the_tool_and_counts():
    ctx = make_context()
    generate_one(ctx, "document_expired", site_id="SITE-001")
    generate_one(ctx, "document_expired", site_id="SITE-002")
    generate_one(ctx, "milestone_delayed", site_id="SITE-003")

    async with Client(mcp) as client:
        tools = await client.list_tools()
        assert [t.name for t in tools.tools] == ["count_events"]

        result = await client.call_tool("count_events", {})
        assert not result.is_error
        assert result.structured_content == {
            "total": 3,
            "by_type": {"document_expired": 2, "milestone_delayed": 1},
        }


@pytest.mark.anyio
async def test_count_events_rejects_bad_argument_type():
    async with Client(mcp) as client:
        result = await client.call_tool("count_events", {"only_unprocessed": "not-a-bool"})
        assert result.is_error
