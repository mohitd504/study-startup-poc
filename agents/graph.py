r"""The LangGraph workflow that handles one event.

    START -> route --(conditional edge on state["route"])--> document_agent ----> finalize -> END
                                                         \-> ...other agents --/
                                                         \-> unhandled -------/

- State: the event, the chosen route, and the specialist's result.
- route (supervisor): looks the event type up in the routing table from rules.yaml.
- specialist nodes: pull context, decide and act through MCP tools.
- finalize (supervisor): makes sure a decision was logged, then marks the
  event processed. mark_event_processed only succeeds once per event.
"""

from typing import Awaitable, Callable, TypedDict

from langgraph.graph import END, START, StateGraph

from agents.mcp_tools import McpTools
from common.config import load_config

SUPERVISOR = "supervisor"

# A specialist takes (event, tools) and returns
# {"outcome", "summary", "decision_logged", ...}.
Specialist = Callable[[dict, McpTools], Awaitable[dict]]


class EventState(TypedDict, total=False):
    event: dict
    route: str
    result: dict


def build_graph(specialists: dict[str, Specialist], tools: McpTools):
    route_by_type = {
        event_type: agent
        for agent, event_types in load_config("rules")["routing"].items()
        for event_type in event_types
    }

    async def route(state: EventState) -> EventState:
        agent = route_by_type.get(state["event"]["event_type"])
        return {"route": agent if agent in specialists else "unhandled"}

    def specialist_node(fn: Specialist):
        async def node(state: EventState) -> EventState:
            return {"result": await fn(state["event"], tools)}
        return node

    async def unhandled(state: EventState) -> EventState:
        event = state["event"]
        reason = (f"No specialist agent handles {event['event_type']} yet "
                  f"(routing: {route_by_type.get(event['event_type'], 'none')}).")
        await tools.call("log_agent_action", event_id=event["event_id"], agent_name=SUPERVISOR,
                         action_type="log_only", reasoning=reason, outcome="Not handled")
        return {"result": {"outcome": "log_only", "summary": "no specialist yet", "decision_logged": True}}

    async def finalize(state: EventState) -> EventState:
        event, result = state["event"], state["result"]
        if not result.get("decision_logged"):
            await tools.call(
                "log_agent_action", event_id=event["event_id"], agent_name=SUPERVISOR, action_type="error",
                reasoning=f"{state['route']} finished without recording a decision. Its last message: "
                          f"{result.get('summary') or '(none)'}",
                outcome="No decision recorded",
            )
            result = {**result, "outcome": "error"}
        await tools.call("mark_event_processed", event_id=event["event_id"], agent_name=SUPERVISOR)
        return {"result": result}

    graph = StateGraph(EventState)
    graph.add_node("route", route)
    for name, fn in specialists.items():
        graph.add_node(name, specialist_node(fn))
        graph.add_edge(name, "finalize")
    graph.add_node("unhandled", unhandled)
    graph.add_node("finalize", finalize)

    graph.add_edge(START, "route")
    graph.add_conditional_edges("route", lambda s: s["route"], [*specialists, "unhandled"])
    graph.add_edge("unhandled", "finalize")
    graph.add_edge("finalize", END)
    return graph.compile()
