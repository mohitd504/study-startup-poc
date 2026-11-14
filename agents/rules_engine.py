"""Rules mode: specialists that apply config/rules.yaml in plain Python.

No LLM and no API key: free, instant and repeatable. They follow the same
steps as the LLM agents, all through MCP tools:
  1. pull context (site, documents, ...) - current state wins over the event payload;
  2. choose one outcome from the rules: auto_resolve, notify, flag or log_only;
  3. act (notification, status change, fix) and log the reasoning.
"""

from common.config import load_config
from agents.mcp_tools import McpTools

DOCUMENT_AGENT = "document_agent"


class Specialist:
    """Shared helpers for acting on one event as one named agent."""

    def __init__(self, name: str, tools: McpTools, event: dict):
        self.name = name
        self.tools = tools
        self.event = event
        self.payload = event["payload"]
        self.rules = load_config("rules")["rules"]

    async def notify(self, spec: dict, message: str, flagged: bool = False) -> dict:
        return await self.tools.call(
            "create_notification",
            event_id=self.event["event_id"],
            severity=spec["severity"],
            recipient_role=spec["recipient_role"],
            message=message,
            status="flagged" if flagged else "pending",
            agent_name=self.name,
        )

    async def decide(self, outcome: str, reasoning: str, result: str) -> dict:
        await self.tools.call(
            "log_agent_action",
            event_id=self.event["event_id"],
            agent_name=self.name,
            action_type=outcome,
            reasoning=reasoning,
            outcome=result,
        )
        return {"outcome": outcome, "summary": result, "decision_logged": True}


# --- Document agent ------------------------------------------------------------

async def document_agent(event: dict, tools: McpTools) -> dict:
    s = Specialist(DOCUMENT_AGENT, tools, event)
    site = (await tools.call("get_site", site_id=s.payload["site_id"]))["site"]
    docs = (await tools.call("list_site_documents", site_id=site["site_id"]))["documents"]
    doc = next((d for d in docs if d["doc_id"] == s.payload["doc_id"]), None)
    if doc is None:
        return await s.decide("error", f"Document {s.payload['doc_id']} is not on file for {site['site_id']}.",
                              "Could not find the document")
    where = f"{doc['doc_type']} (doc {doc['doc_id']}) at {site['site_id']} {site['name']}"
    handler = {
        "document_submitted": _submitted,
        "document_expiring": _expiring,
        "document_expired": _expired,
        "document_rejected": _rejected,
    }[event["event_type"]]
    return await handler(s, doc, where)


async def _submitted(s: Specialist, doc: dict, where: str) -> dict:
    return await s.decide(
        "log_only",
        f"{where} was submitted and is now {doc['status']}. The rules say to log a submission "
        "without notifying anyone; it is waiting for review.",
        "Logged submission, no action needed",
    )


async def _expiring(s: Specialist, doc: dict, where: str) -> dict:
    rule = s.rules["document_expiring"]
    days = doc["days_until_expiry"]
    if days is None:
        return await s.decide("log_only", f"{where} has no expiry date on file any more.", "Nothing to do")
    if days < 0:  # it has already passed: the expired rule applies
        return await _expired(s, doc, where)
    if days <= rule["notify_within_days"]:
        await s.notify(rule["notify"], f"{where} expires on {doc['expiry_date']} ({days} days). "
                                       "Arrange renewal before it lapses.")
        return await s.decide(
            "notify",
            f"{where} expires in {days} days ({doc['expiry_date']}), within the "
            f"{rule['notify_within_days']}-day threshold, so the {rule['notify']['recipient_role']} "
            f"is notified ({rule['notify']['severity']} severity).",
            f"Notified {rule['notify']['recipient_role']}: expires in {days} days",
        )
    return await s.decide(
        "log_only",
        f"{where} expires in {days} days ({doc['expiry_date']}), more than the "
        f"{rule['notify_within_days']}-day threshold, so no notification yet.",
        f"Logged only: {days} days left",
    )


async def _expired(s: Specialist, doc: dict, where: str) -> dict:
    rule = s.rules["document_expired"]
    days = doc["days_until_expiry"]
    if days is None or days >= 0:
        return await s.decide(
            "log_only",
            f"The event said {where} expired, but its expiry date is now {doc['expiry_date']}, "
            "so it has been renewed since. Nothing to flag.",
            "Already renewed, no action",
        )
    if doc["status"] != rule["set_document_status"]:
        await s.tools.call(
            "update_document_status", doc_id=doc["doc_id"], status=rule["set_document_status"],
            note=f"Expiry date {doc['expiry_date']} has passed.", event_id=s.event["event_id"],
            agent_name=s.name,
        )
    await s.notify(rule["flag"], f"{where} expired on {doc['expiry_date']} ({-days} days ago). "
                                 "The site cannot proceed on an expired document; obtain a renewed copy.",
                   flagged=True)
    return await s.decide(
        "flag",
        f"{where} expired on {doc['expiry_date']}, {-days} days ago. The rules say to always flag an "
        f"expired document ({rule['flag']['severity']} severity) and mark it Expired.",
        f"Flagged to {rule['flag']['recipient_role']}; document set to Expired",
    )


async def _rejected(s: Specialist, doc: dict, where: str) -> dict:
    rule = s.rules["document_rejected"]
    count = doc["rejection_count"]
    reason = doc["reviewer_comment"] or s.payload.get("reason") or "no reason given"
    if count >= rule["flag_at_rejection_count"]:
        await s.notify(rule["flag"], f"{where} has now been rejected {count} times. Latest reason: "
                                     f"{reason}. Please step in with the site.", flagged=True)
        return await s.decide(
            "flag",
            f"{where} has been rejected {count} times, reaching the threshold of "
            f"{rule['flag_at_rejection_count']}, so it is flagged to the {rule['flag']['recipient_role']} "
            f"({rule['flag']['severity']} severity). Latest reason: {reason}.",
            f"Flagged to {rule['flag']['recipient_role']}: rejection #{count}",
        )
    await s.notify(rule["notify"], f"{where} was rejected (rejection {count}). Reason: {reason}. "
                                   "Please correct and resubmit.")
    return await s.decide(
        "notify",
        f"{where} was rejected for the {_ordinal(count)} time, below the flag threshold of "
        f"{rule['flag_at_rejection_count']}, so the {rule['notify']['recipient_role']} is notified with "
        f"the reason: {reason}.",
        f"Notified {rule['notify']['recipient_role']}: rejection #{count}",
    )


def _ordinal(n: int) -> str:
    return {1: "first", 2: "second", 3: "third"}.get(n, f"{n}th")


# Specialists available in rules mode, by agent name (matches routing in rules.yaml).
RULE_SPECIALISTS = {
    DOCUMENT_AGENT: document_agent,
}
