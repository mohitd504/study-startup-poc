"""Rules mode: specialists that apply the decision rules in plain Python.

No LLM and no API key: free, instant and repeatable. They follow the same
steps as the LLM agents, all through MCP:
  1. read the rules (study://rules) and pull context with tools; the current
     state wins over the event payload, since things may have changed;
  2. choose one outcome from the rules: auto_resolve, notify, flag or log_only;
  3. act (notification, status change, fix) and log the reasoning.
"""

import re
from datetime import datetime

from agents.mcp_tools import McpTools

DOCUMENT_AGENT = "document_agent"
MILESTONE_SITE_AGENT = "milestone_site_agent"
DATA_QUALITY_AGENT = "data_quality_agent"


class Specialist:
    """Shared helpers for acting on one event as one named agent."""

    def __init__(self, name: str, tools: McpTools, event: dict, config: dict):
        self.name = name
        self.tools = tools
        self.event = event
        self.payload = event["payload"]
        self.config = config
        self.rules = config["rules"]

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

    async def set_site_status(self, site_id: str, status: str, note: str) -> dict:
        return await self.tools.call(
            "update_site_status", site_id=site_id, status=status, note=note,
            event_id=self.event["event_id"], agent_name=self.name,
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


async def _start(name: str, event: dict, tools: McpTools) -> tuple[Specialist, dict]:
    s = Specialist(name, tools, event, await tools.rules())
    site = (await tools.call("get_site", site_id=s.payload["site_id"]))
    return s, site


def _site_label(site: dict) -> str:
    return f"{site['site_id']} {site['name']}"


def _ordinal(n: int) -> str:
    return {1: "first", 2: "second", 3: "third"}.get(n, f"{n}th")


# --- Document agent ------------------------------------------------------------

async def document_agent(event: dict, tools: McpTools) -> dict:
    s, site_info = await _start(DOCUMENT_AGENT, event, tools)
    site = site_info["site"]
    docs = (await tools.call("list_site_documents", site_id=site["site_id"]))["documents"]
    doc = next((d for d in docs if d["doc_id"] == s.payload["doc_id"]), None)
    if doc is None:
        return await s.decide("error", f"Document {s.payload['doc_id']} is not on file for {site['site_id']}.",
                              "Could not find the document")
    where = f"{doc['doc_type']} (doc {doc['doc_id']}) at {_site_label(site)}"
    handler = {
        "document_submitted": _doc_submitted,
        "document_expiring": _doc_expiring,
        "document_expired": _doc_expired,
        "document_rejected": _doc_rejected,
    }[event["event_type"]]
    return await handler(s, doc, where)


async def _doc_submitted(s: Specialist, doc: dict, where: str) -> dict:
    return await s.decide(
        "log_only",
        f"{where} was submitted and is now {doc['status']}. The rules say to log a submission "
        "without notifying anyone; it is waiting for review.",
        "Logged submission, no action needed",
    )


async def _doc_expiring(s: Specialist, doc: dict, where: str) -> dict:
    rule = s.rules["document_expiring"]
    days = doc["days_until_expiry"]
    if days is None:
        return await s.decide("log_only", f"{where} has no expiry date on file any more.", "Nothing to do")
    if days < 0:  # it has already passed: the expired rule applies
        return await _doc_expired(s, doc, where)
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


async def _doc_expired(s: Specialist, doc: dict, where: str) -> dict:
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


async def _doc_rejected(s: Specialist, doc: dict, where: str) -> dict:
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


# --- Milestone and site agent --------------------------------------------------

async def milestone_site_agent(event: dict, tools: McpTools) -> dict:
    s, site_info = await _start(MILESTONE_SITE_AGENT, event, tools)
    handler = {
        "milestone_delayed": _milestone_delayed,
        "approval_received": _approval_received,
        "contract_signed": _contract_signed,
        "site_ready_check": _site_ready_check,
    }[event["event_type"]]
    return await handler(s, site_info)


async def _milestone_delayed(s: Specialist, site_info: dict) -> dict:
    rule = s.rules["milestone_delayed"]
    site = site_info["site"]
    milestones = (await s.tools.call("list_site_milestones", site_id=site["site_id"]))["milestones"]
    m = next((m for m in milestones if m["milestone_id"] == s.payload["milestone_id"]), None)
    if m is None:
        return await s.decide("error", f"Milestone {s.payload['milestone_id']} not found for {site['site_id']}.",
                              "Could not find the milestone")
    where = f"Milestone {m['type']} at {_site_label(site)}"
    if m["status"] == "Completed":
        return await s.decide("log_only", f"{where} was completed on {m['actual_date']} after the event was "
                                          "raised, so it is no longer late.", "Already completed, no action")
    if not m["planned_date_valid"]:
        return await s.decide("log_only", f"{where} has an invalid planned date ({m['planned_date']!r}), so "
                                          "lateness cannot be measured; that is a data quality issue.",
                              "Planned date invalid, left to data quality")
    days = m["days_late"]
    if days <= 0:
        return await s.decide("log_only", f"{where} is now planned for {m['planned_date']}, so it is not late.",
                              "Not late any more, no action")
    if days >= rule["flag_at_days_late"]:
        await s.notify(rule["flag"], f"{where} is {days} days late (planned {m['planned_date']}). "
                                     "This is putting site activation at risk.", flagged=True)
        return await s.decide(
            "flag",
            f"{where} was planned for {m['planned_date']} and is {days} days late with no actual date, at or "
            f"over the {rule['flag_at_days_late']}-day threshold, so it is flagged to the "
            f"{rule['flag']['recipient_role']} ({rule['flag']['severity']} severity).",
            f"Flagged to {rule['flag']['recipient_role']}: {days} days late",
        )
    await s.notify(rule["notify"], f"{where} is {days} days late (planned {m['planned_date']}). "
                                   "Please confirm a new date with the site.")
    return await s.decide(
        "notify",
        f"{where} was planned for {m['planned_date']} and is {days} days late, under the "
        f"{rule['flag_at_days_late']}-day threshold, so the {rule['notify']['recipient_role']} is notified "
        f"({rule['notify']['severity']} severity).",
        f"Notified {rule['notify']['recipient_role']}: {days} days late",
    )


async def _approval_received(s: Specialist, site_info: dict) -> dict:
    rule = s.rules["approval_received"]["advance_site_status"]
    site = site_info["site"]
    if site["status"] in rule["from"]:
        await s.set_site_status(site["site_id"], rule["to"], "Ethics approval received.")
        return await s.decide(
            "auto_resolve",
            f"Ethics approval came in for {_site_label(site)} while it was {site['status']}. The rules say to "
            f"move a site in {' or '.join(rule['from'])} forward to {rule['to']}.",
            f"Site moved {site['status']} -> {rule['to']}",
        )
    return await s.decide(
        "log_only",
        f"Ethics approval came in for {_site_label(site)}, which is already {site['status']}, so no status "
        "change is needed.",
        "Approval recorded, no status change",
    )


async def _contract_signed(s: Specialist, site_info: dict) -> dict:
    site, readiness = site_info["site"], site_info["readiness"]
    target = s.rules["site_ready_check"]["on_ready_set_status"]
    if site["status"] in (target, "Activated"):
        return await s.decide("log_only", f"Contract signed for {_site_label(site)}, which is already "
                                          f"{site['status']}.", "No status change needed")
    if readiness["ready_to_activate"]:
        await s.set_site_status(site["site_id"], target, "Contract signed and all readiness checks pass.")
        return await s.decide(
            "auto_resolve",
            f"The contract for {_site_label(site)} is signed and every readiness check now passes (documents "
            f"approved and in date, milestones complete, no expired training), so the site moves to {target}.",
            f"Site moved {site['status']} -> {target}",
        )
    missing = readiness["missing"]
    return await s.decide(
        "log_only",
        f"The contract for {_site_label(site)} is signed, but the site is not ready yet. Still missing: "
        f"{'; '.join(missing)}.",
        f"Contract signed; {len(missing)} item(s) still missing",
    )


async def _site_ready_check(s: Specialist, site_info: dict) -> dict:
    rule = s.rules["site_ready_check"]
    site, readiness = site_info["site"], site_info["readiness"]
    target = rule["on_ready_set_status"]
    if site["status"] in (target, "Activated"):
        return await s.decide("log_only", f"{_site_label(site)} is already {site['status']}.",
                              "Already ready, no action")
    if readiness["ready_to_activate"]:
        await s.set_site_status(site["site_id"], target, "All readiness checks pass.")
        return await s.decide(
            "auto_resolve",
            f"Every required document for {_site_label(site)} is approved and in date, the contract is signed "
            f"and no staff GCP training has expired, so the site moves from {site['status']} to {target}.",
            f"Site moved {site['status']} -> {target}",
        )
    missing = readiness["missing"]
    await s.notify(rule["notify_missing"], f"{_site_label(site)} is not ready to activate. Missing: "
                                           + "; ".join(missing))
    return await s.decide(
        "notify",
        f"{_site_label(site)} is not ready to activate: {len(missing)} item(s) are outstanding, so the "
        f"{rule['notify_missing']['recipient_role']} is notified with the list. First items: "
        f"{'; '.join(missing[:3])}.",
        f"Notified {rule['notify_missing']['recipient_role']}: {len(missing)} item(s) missing",
    )


# --- Data quality agent --------------------------------------------------------

async def data_quality_agent(event: dict, tools: McpTools) -> dict:
    s, site_info = await _start(DATA_QUALITY_AGENT, event, tools)
    if event["event_type"] == "staff_training_expired":
        return await _training_expired(s, site_info)
    return await _data_issue(s, site_info)


async def _training_expired(s: Specialist, site_info: dict) -> dict:
    rule = s.rules["staff_training_expired"]
    site = site_info["site"]
    staff = (await s.tools.call("list_site_staff", site_id=site["site_id"]))["staff"]
    person = next((p for p in staff if p["staff_id"] == s.payload["staff_id"]), None)
    if person is None:
        return await s.decide("error", f"Staff member {s.payload['staff_id']} not found at {site['site_id']}.",
                              "Could not find the staff member")
    who = f"{person['name'] or 'staff #' + str(person['staff_id'])} ({person['role']}) at {_site_label(site)}"
    if not person["training_expired"]:
        return await s.decide("log_only", f"GCP training for {who} now runs to {person['gcp_training_expiry']}, "
                                          "so it has been renewed since the event.", "Already renewed, no action")
    await s.notify(rule["flag"], f"GCP training for {who} expired on {person['gcp_training_expiry']}. They "
                                 "must not perform trial duties until it is renewed.", flagged=True)
    return await s.decide(
        "flag",
        f"GCP training for {who} expired on {person['gcp_training_expiry']}. The rules say to always flag "
        f"expired training ({rule['flag']['severity']} severity) to the {rule['flag']['recipient_role']}.",
        f"Flagged to {rule['flag']['recipient_role']}: training expired",
    )


async def _data_issue(s: Specialist, site_info: dict) -> dict:
    rule = s.rules["record_data_issue"]
    site = site_info["site"]
    entity, entity_id, field = s.payload["entity_type"], s.payload["entity_id"], s.payload["field"]
    current = await _current_value(s, site, entity, entity_id, field)
    where = f"{entity} {entity_id} field {field} at {_site_label(site)}"

    if current["status"] == "missing":
        return await s.decide("error", f"Could not find {entity} {entity_id} for {site['site_id']}.",
                              "Record not found")
    if current["status"] == "ok":
        return await s.decide("log_only", f"{where} is now {current['value']!r}, which is valid, so it has "
                                          "already been corrected.", "Already fixed, no action")

    fixed, how = await _infer(s, site, entity, field, current["value"], rule)
    if fixed is not None:
        await s.tools.call("fix_record_field", entity_type=entity, entity_id=str(entity_id), field=field,
                           value=fixed, event_id=s.event["event_id"], agent_name=s.name, reason=how)
        return await s.decide(
            "auto_resolve",
            f"{where} was {current['value']!r}. {how} The rules allow fixing an inferable value automatically.",
            f"Fixed {field}: {current['value']!r} -> {fixed!r}",
        )
    await s.notify(rule["notify"], f"{where} is {current['value']!r} ({s.payload.get('issue', 'invalid')}) and "
                                   f"the correct value cannot be worked out. {how} Please correct it at source.")
    return await s.decide(
        "notify",
        f"{where} is {current['value']!r} and cannot be inferred: {how} So the "
        f"{rule['notify']['recipient_role']} is notified ({rule['notify']['severity']} severity).",
        f"Notified {rule['notify']['recipient_role']}: cannot infer {field}",
    )


async def _current_value(s: Specialist, site: dict, entity: str, entity_id, field: str) -> dict:
    """Re-read the field now. status: ok (valid), bad (still needs fixing) or missing."""
    if entity == "site":
        value = site.get(field) or ""
        return {"status": "ok" if value.strip() else "bad", "value": value}
    if entity == "milestone":
        rows = (await s.tools.call("list_site_milestones", site_id=site["site_id"]))["milestones"]
        row = next((r for r in rows if r["milestone_id"] == int(entity_id)), None)
        if row is None:
            return {"status": "missing", "value": None}
        valid = row["planned_date_valid"] if field == "planned_date" else bool(row.get(field))
        return {"status": "ok" if valid else "bad", "value": row.get(field)}
    if entity == "staff":
        rows = (await s.tools.call("list_site_staff", site_id=site["site_id"]))["staff"]
        row = next((r for r in rows if r["staff_id"] == int(entity_id)), None)
        if row is None:
            return {"status": "missing", "value": None}
        value = row.get(field) or ""
        return {"status": "ok" if str(value).strip() else "bad", "value": value}
    return {"status": "bad", "value": None}


async def _infer(s: Specialist, site: dict, entity: str, field: str, value, rule: dict) -> tuple[str | None, str]:
    """Return (fixed value, explanation) or (None, why it cannot be inferred)."""
    if entity == "site" and field == "country":
        cities = (await s.tools.read_yaml("study://reference"))["city_country"]
        for city, country in cities.items():
            if re.search(rf"\b{re.escape(city)}\b", site["name"] or "", re.IGNORECASE):
                return country, f"The site name {site['name']!r} contains the city {city}, which is in {country}."
        return None, f"The site name {site['name']!r} does not contain a known city."
    if field.endswith("_date") and isinstance(value, str):
        for fmt in rule["inferable_date_formats"]:
            try:
                fixed = datetime.strptime(value.strip(), fmt).date().isoformat()
                return fixed, f"{value!r} is an unambiguous date in {fmt} format, reformatted to YYYY-MM-DD."
            except ValueError:
                continue
        return None, f"{value!r} is not a date in any safely convertible format."
    return None, f"There is no reliable source for the missing {field}."


# Specialists available in rules mode, by agent name (matches routing in rules.yaml).
RULE_SPECIALISTS = {
    DOCUMENT_AGENT: document_agent,
    MILESTONE_SITE_AGENT: milestone_site_agent,
    DATA_QUALITY_AGENT: data_quality_agent,
}
