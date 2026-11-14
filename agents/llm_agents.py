"""OpenAI mode: specialists are LangGraph ReAct agents that call MCP tools.

The decision rules are rendered into each agent's system prompt from
config/rules.yaml, so changing a threshold there changes the agents too.
The system prompt is identical on every call (the event goes in the user
message), which lets OpenAI's automatic prompt caching apply.
"""

import json
import warnings
from datetime import date

import yaml
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agents.mcp_tools import McpTools
from common.config import load_config

from langgraph.prebuilt import create_react_agent

SPECIALISTS = {
    "document_agent": {
        "title": "Document agent",
        "tools": [
            "get_site", "list_site_documents", "list_site_milestones",
            "update_document_status", "create_notification", "log_agent_action",
        ],
    },
}

PROMPT = """You are the {title} in a clinical trial study start-up operations team. \
You handle these events: {event_types}.

You can read and change data only through your tools. Your agent_name is "{name}": \
pass it, and the event_id, to every tool that accepts them.

For the event you are given:
1. Pull context first: get_site, then the list_site_* tools you need. Decide on the \
current data, not only the event payload; things may have changed since the event was created.
2. Apply the decision rules below and choose exactly one outcome:
   - notify: create_notification with status "pending"
   - flag: create_notification with status "flagged" (medium or high severity)
   - auto_resolve: make the change the rule names, with the write tool
   - log_only: no notification or change, only where a rule says so
   Use exactly the severity and recipient_role the rule gives.
3. Call log_agent_action exactly once: action_type is your outcome, reasoning is 1-3 \
plain sentences citing the facts you checked (dates, counts, statuses), outcome is a short result.
4. After log_agent_action, call no more tools. Reply with one line of JSON: \
{{"outcome": "<outcome>", "summary": "<short summary>"}}

If a tool returns an error, read it, fix the arguments and try again. If you cannot \
complete the event, call log_agent_action with action_type "error" and explain why.

# Decision rules (from config/rules.yaml)
{rules}"""


def system_prompt(name: str) -> str:
    config = load_config("rules")
    event_types = config["routing"][name]
    sections = []
    for event_type in event_types:
        rule = dict(config["rules"][event_type])
        summary = rule.pop("summary")
        settings = f"\nSettings: {yaml.safe_dump(rule, default_flow_style=True).strip()}" if rule else ""
        sections.append(f"## {event_type}\n{summary}{settings}")
    return PROMPT.format(
        title=SPECIALISTS[name]["title"],
        name=name,
        event_types=", ".join(event_types),
        rules="\n\n".join(sections),
    )


def user_message(event: dict) -> str:
    return f"Today is {date.today().isoformat()}. Handle this event:\n{json.dumps(event)}"


def build_model(model_name: str):
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(model=model_name, timeout=60, max_retries=2)


class LlmSpecialist:
    def __init__(self, name: str, model, tools: McpTools):
        self.name = name
        with warnings.catch_warnings():
            # create_react_agent is deprecated in this LangGraph version in favour of
            # langchain.agents.create_agent (a separate package), but works the same.
            warnings.simplefilter("ignore")
            self.agent = create_react_agent(
                model, tools.subset(SPECIALISTS[name]["tools"]), prompt=system_prompt(name)
            )

    async def __call__(self, event: dict, tools: McpTools) -> dict:
        result = await self.agent.ainvoke(
            {"messages": [HumanMessage(user_message(event))]}, {"recursion_limit": 25}
        )
        return summarize(result["messages"])


def summarize(messages: list) -> dict:
    """Work out what the agent did from its message history."""
    statuses = {m.tool_call_id: m.status for m in messages if isinstance(m, ToolMessage)}
    calls, outcome, logged = [], None, False
    usage = {"input": 0, "cached_input": 0, "output": 0}
    for m in messages:
        if not isinstance(m, AIMessage):
            continue
        for call in m.tool_calls:
            ok = statuses.get(call["id"]) != "error"
            calls.append(call["name"] if ok else f"{call['name']}(error)")
            if call["name"] == "log_agent_action" and ok:
                outcome, logged = call["args"].get("action_type"), True
        if m.usage_metadata:
            usage["input"] += m.usage_metadata.get("input_tokens", 0)
            usage["output"] += m.usage_metadata.get("output_tokens", 0)
            usage["cached_input"] += (m.usage_metadata.get("input_token_details") or {}).get("cache_read", 0)

    final = next((m for m in reversed(messages) if isinstance(m, AIMessage) and not m.tool_calls), None)
    summary = _text(final.content) if final else ""
    try:
        parsed = json.loads(summary.strip().splitlines()[-1])
        summary = parsed.get("summary", summary)
        outcome = outcome or parsed.get("outcome")
    except (ValueError, IndexError, AttributeError):
        pass
    return {
        "outcome": outcome or "unknown",
        "summary": summary[:200],
        "decision_logged": logged,  # only a successful log_agent_action counts
        "tool_calls": calls,
        "usage": usage,
    }


def _text(content) -> str:
    if isinstance(content, str):
        return content
    return "".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in content)
