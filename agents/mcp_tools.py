"""Connects the agent layer to the MCP server. Agents never open the database.

Tools come from langchain-mcp-adapters as LangChain tools. LLM agents use
them directly; rules-mode code uses `McpTools.call`, which returns a tool's
structured result and raises on a tool error.
"""

import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools


class ToolCallError(Exception):
    """An MCP tool returned an error (for example, failed validation)."""


class McpTools:
    def __init__(self, tools: list[BaseTool]):
        self.by_name = {t.name: t for t in tools}

    def subset(self, names: list[str]) -> list[BaseTool]:
        return [self.by_name[n] for n in names]

    async def call(self, name: str, **args) -> dict:
        # Invoking with a tool call returns a ToolMessage: text for a model in
        # .content, and the MCP structured result in .artifact.
        message = await self.by_name[name].ainvoke(
            {"type": "tool_call", "id": f"call_{uuid.uuid4().hex[:12]}", "name": name, "args": args}
        )
        if message.status == "error":
            raise ToolCallError(f"{name}: {_text(message.content)}")
        return message.artifact["structured_content"]


def _text(content) -> str:
    if isinstance(content, str):
        return content
    return " ".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in content)


@asynccontextmanager
async def open_tools(url: str) -> AsyncIterator[McpTools]:
    """Open one MCP session and yield its tools for as long as the block runs.

    client.get_tools() would open a new session for every tool call (about
    0.25 s each, far worse under load); one shared session takes about 0.04 s
    per call and handles concurrent calls.
    """
    client = MultiServerMCPClient({"study": {"url": url, "transport": "streamable_http"}})
    async with client.session("study") as session:
        yield McpTools(await load_mcp_tools(session))
