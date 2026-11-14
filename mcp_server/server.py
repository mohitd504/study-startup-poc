"""Study start-up MCP server (Stage 2): the only way agents touch the data.

    python -m mcp_server.server                      Streamable HTTP on :8000/mcp
    python -m mcp_server.server --transport stdio    for clients that launch it

Rules enforced here, whoever the caller is:
  - every write tool validates its inputs and raises a clear ToolError;
  - no tool deletes anything;
  - every write tool call is recorded in agent_actions, in the same
    transaction as the change it makes.

ToolError matters: in MCP SDK 2.x any other exception is treated as a crash
and the client only sees "Error executing tool <name>".
"""

import argparse
import json
import re
from datetime import date
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from common.config import CONFIG_DIR, load_config
from common.db import SCHEMA, connect, now
from common.domain import DOC_STATUSES, DOC_TYPES, MILESTONE_TYPES, SITE_STATUSES, STAFF_ROLES
from common.readiness import missing_for_activation

mcp = MCPServer(
    "study-startup",
    instructions=(
        "Study start-up data: sites, regulatory documents, milestones, staff and the "
        "events that change them. Read study://rules for the decision rules before "
        "acting. Pass event_id and your agent_name to every write tool."
    ),
)

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# fix_record_field may only touch these (entity_type -> table, key column, {field: kind}).
FIXABLE_FIELDS = {
    "site": ("sites", "site_id", {"country": "text", "pi_name": "text", "name": "text"}),
    "milestone": ("site_milestones", "milestone_id", {"planned_date": "date", "actual_date": "date"}),
    "staff": ("site_staff", "staff_id", {"name": "text", "gcp_training_expiry": "date"}),
    "document": ("regulatory_documents", "doc_id", {"expiry_date": "date"}),
}


# --- helpers -----------------------------------------------------------------

def _rules() -> dict:
    return load_config("rules")  # read per call, so edits apply without a restart


def _today() -> date:
    return date.today()


def _days_until(iso: str | None) -> int | None:
    if not iso or not ISO_DATE.match(iso):
        return None
    try:
        return (date.fromisoformat(iso) - _today()).days
    except ValueError:
        return None


def _require_text(name: str, value: str, max_len: int = 1000) -> str:
    value = (value or "").strip()
    if not value:
        raise ToolError(f"{name} must not be empty.")
    if len(value) > max_len:
        raise ToolError(f"{name} is too long ({len(value)} characters, max {max_len}).")
    return value


def _require_choice(name: str, value: str, allowed: list[str]) -> str:
    if value not in allowed:
        raise ToolError(f"Invalid {name} {value!r}. Allowed: {', '.join(allowed)}.")
    return value


def _require_iso_date(name: str, value: str) -> str:
    try:
        if not ISO_DATE.match(value or ""):
            raise ValueError
        date.fromisoformat(value)
    except ValueError:
        raise ToolError(f"{name} must be a real date in YYYY-MM-DD format, got {value!r}.")
    return value


def _require_event(conn, event_id: int | None) -> None:
    if event_id is not None and not conn.execute(
        "SELECT 1 FROM events WHERE event_id = ?", (event_id,)
    ).fetchone():
        raise ToolError(f"Unknown event_id {event_id}.")


def _require_row(conn, table: str, key: str, value) -> dict:
    row = conn.execute(f"SELECT * FROM {table} WHERE {key} = ?", (value,)).fetchone()
    if not row:
        raise ToolError(f"No {table} row with {key} = {value!r}.")
    return dict(row)


def _audit(conn, tool: str, event_id: int | None, agent_name: str, reasoning: str, outcome: str) -> int:
    cur = conn.execute(
        "INSERT INTO agent_actions (event_id, agent_name, action_type, reasoning, outcome, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (event_id, agent_name, f"tool:{tool}", reasoning, outcome, now()),
    )
    return cur.lastrowid


def _site_context(conn, site_id: str) -> tuple[dict, list[dict], list[dict], list[dict]]:
    site = _require_row(conn, "sites", "site_id", site_id)
    docs = [dict(r) for r in conn.execute(
        "SELECT * FROM regulatory_documents WHERE site_id = ? ORDER BY doc_id", (site_id,))]
    milestones = [dict(r) for r in conn.execute(
        "SELECT * FROM site_milestones WHERE site_id = ? ORDER BY milestone_id", (site_id,))]
    staff = [dict(r) for r in conn.execute(
        "SELECT * FROM site_staff WHERE site_id = ? ORDER BY staff_id", (site_id,))]
    return site, docs, milestones, staff


def _readiness(docs, milestones, staff) -> dict:
    missing = missing_for_activation(docs, milestones, staff, _rules()["readiness"], _today())
    return {"ready_to_activate": not missing, "missing": missing}


# --- read tools --------------------------------------------------------------

@mcp.tool()
def get_unprocessed_events(limit: int = 10) -> dict[str, Any]:
    """Return the oldest events that have not been processed yet, oldest first.

    Args:
        limit: Maximum number of events to return (1 to 100).
    """
    if not 1 <= limit <= 100:
        raise ToolError("limit must be between 1 and 100.")
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM events WHERE processed = 0 ORDER BY event_id LIMIT ?", (limit,)
        ).fetchall()
        remaining = conn.execute("SELECT COUNT(*) FROM events WHERE processed = 0").fetchone()[0]
    events = [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]
    return {"events": events, "unprocessed_total": remaining}


@mcp.tool()
def get_site(site_id: str) -> dict[str, Any]:
    """Return a site, its study, and whether it is ready to activate (with what is missing).

    Args:
        site_id: Site identifier, e.g. "SITE-003".
    """
    with connect() as conn:
        site, docs, milestones, staff = _site_context(conn, site_id)
        study = dict(conn.execute("SELECT * FROM studies WHERE study_id = ?", (site["study_id"],)).fetchone())
    return {"site": site, "study": study, "readiness": _readiness(docs, milestones, staff), "today": _today().isoformat()}


@mcp.tool()
def list_site_documents(site_id: str) -> dict[str, Any]:
    """List a site's regulatory documents, with days until expiry (negative = expired).

    Args:
        site_id: Site identifier, e.g. "SITE-003".
    """
    with connect() as conn:
        _, docs, _, _ = _site_context(conn, site_id)
    for d in docs:
        d["days_until_expiry"] = _days_until(d["expiry_date"])
    return {"site_id": site_id, "today": _today().isoformat(), "documents": docs}


@mcp.tool()
def list_site_milestones(site_id: str) -> dict[str, Any]:
    """List a site's milestones in order, with days late for unfinished ones past their planned date.

    Args:
        site_id: Site identifier, e.g. "SITE-003".
    """
    with connect() as conn:
        _, _, milestones, _ = _site_context(conn, site_id)
    for m in milestones:
        days = _days_until(m["planned_date"])
        m["planned_date_valid"] = days is not None
        m["days_late"] = -days if days is not None and days < 0 and m["status"] != "Completed" else 0
    return {"site_id": site_id, "today": _today().isoformat(), "milestones": milestones}


@mcp.tool()
def list_site_staff(site_id: str) -> dict[str, Any]:
    """List a site's staff, flagging anyone whose GCP training has expired.

    Args:
        site_id: Site identifier, e.g. "SITE-003".
    """
    with connect() as conn:
        _, _, _, staff = _site_context(conn, site_id)
    for s in staff:
        days = _days_until(s["gcp_training_expiry"])
        s["training_expired"] = days is not None and days < 0
        s["cv_on_file"] = bool(s["cv_on_file"])
    return {"site_id": site_id, "today": _today().isoformat(), "staff": staff}


@mcp.tool()
def get_study_summary(study_id: str) -> dict[str, Any]:
    """Summarise a study: sites by status, open notifications and unprocessed events.

    Args:
        study_id: Study identifier, e.g. "STU-001".
    """
    with connect() as conn:
        study = _require_row(conn, "studies", "study_id", study_id)
        by_status = {r["status"]: r["n"] for r in conn.execute(
            "SELECT status, COUNT(*) AS n FROM sites WHERE study_id = ? GROUP BY status", (study_id,))}
        site_ids = [r[0] for r in conn.execute("SELECT site_id FROM sites WHERE study_id = ?", (study_id,))]
        marks = ",".join("?" * len(site_ids)) or "''"
        notifications = {r["status"]: r["n"] for r in conn.execute(
            f"""SELECT n.status, COUNT(*) AS n FROM notifications n JOIN events e ON e.event_id = n.event_id
                WHERE json_extract(e.payload, '$.site_id') IN ({marks}) OR e.entity_id IN ({marks})
                GROUP BY n.status""", site_ids * 2)}
        unprocessed = conn.execute(
            f"""SELECT COUNT(*) FROM events WHERE processed = 0 AND
                (json_extract(payload, '$.site_id') IN ({marks}) OR entity_id IN ({marks}))""",
            site_ids * 2).fetchone()[0]
    return {
        "study": study,
        "sites_total": len(site_ids),
        "sites_by_status": by_status,
        "notifications_by_status": notifications,
        "unprocessed_events": unprocessed,
    }


# --- write tools -------------------------------------------------------------

@mcp.tool()
def update_document_status(
    doc_id: int, status: str, note: str, event_id: int | None = None, agent_name: str = "manual"
) -> dict[str, Any]:
    """Change a regulatory document's status.

    Rejected increases the document's rejection count and stores the note as the
    reviewer comment. Expired is only allowed once the expiry date has passed.

    Args:
        doc_id: Document id.
        status: One of Not Started, Submitted, Approved, Rejected, Expired.
        note: Why the status is changing (plain language).
        event_id: The event being handled, if any.
        agent_name: Who is making the change.
    """
    _require_choice("status", status, DOC_STATUSES)
    note = _require_text("note", note)
    agent_name = _require_text("agent_name", agent_name, 64)
    with connect() as conn:
        _require_event(conn, event_id)
        doc = _require_row(conn, "regulatory_documents", "doc_id", doc_id)
        if status == "Expired" and not ((_days_until(doc["expiry_date"]) or 0) < 0):
            raise ToolError(
                f"Document {doc_id} cannot be set to Expired: its expiry date "
                f"({doc['expiry_date'] or 'none'}) has not passed."
            )
        if status == "Rejected":
            conn.execute(
                "UPDATE regulatory_documents SET status = ?, reviewer_comment = ?, "
                "rejection_count = rejection_count + 1 WHERE doc_id = ?",
                (status, note, doc_id),
            )
        else:
            conn.execute("UPDATE regulatory_documents SET status = ? WHERE doc_id = ?", (status, doc_id))
        outcome = f"document {doc_id} ({doc['doc_type']}, {doc['site_id']}): {doc['status']} -> {status}"
        _audit(conn, "update_document_status", event_id, agent_name, note, outcome)
    return {"doc_id": doc_id, "previous_status": doc["status"], "status": status, "changed": doc["status"] != status}


@mcp.tool()
def update_site_status(
    site_id: str, status: str, note: str, event_id: int | None = None, agent_name: str = "manual"
) -> dict[str, Any]:
    """Change a site's status.

    Ready To Activate is refused unless the site passes the readiness check
    (the error lists what is missing). Activated is only allowed from
    Ready To Activate.

    Args:
        site_id: Site identifier, e.g. "SITE-003".
        status: One of Identified, Selected, Docs In Progress, Contract In Review,
            Ready To Activate, Activated, On Hold.
        note: Why the status is changing (plain language).
        event_id: The event being handled, if any.
        agent_name: Who is making the change.
    """
    _require_choice("status", status, SITE_STATUSES)
    note = _require_text("note", note)
    agent_name = _require_text("agent_name", agent_name, 64)
    with connect() as conn:
        _require_event(conn, event_id)
        site, docs, milestones, staff = _site_context(conn, site_id)
        if status == "Ready To Activate":
            readiness = _readiness(docs, milestones, staff)
            if not readiness["ready_to_activate"]:
                raise ToolError(
                    f"{site_id} is not ready to activate. Missing: " + "; ".join(readiness["missing"])
                )
        if status == "Activated" and site["status"] != "Ready To Activate":
            raise ToolError(f"{site_id} must be Ready To Activate before Activated (it is {site['status']}).")
        conn.execute("UPDATE sites SET status = ? WHERE site_id = ?", (status, site_id))
        outcome = f"site {site_id}: {site['status']} -> {status}"
        _audit(conn, "update_site_status", event_id, agent_name, note, outcome)
    return {"site_id": site_id, "previous_status": site["status"], "status": status, "changed": site["status"] != status}


@mcp.tool()
def fix_record_field(
    entity_type: str,
    entity_id: str,
    field: str,
    value: str,
    event_id: int | None = None,
    agent_name: str = "manual",
    reason: str = "",
) -> dict[str, Any]:
    """Correct one field on a record (for data quality fixes).

    Only these fields can be fixed:
      site: country, pi_name, name
      milestone: planned_date, actual_date   (YYYY-MM-DD)
      staff: name, gcp_training_expiry       (dates YYYY-MM-DD)
      document: expiry_date                  (YYYY-MM-DD)

    Args:
        entity_type: site, milestone, staff or document.
        entity_id: The record's id (e.g. "SITE-003" or "126").
        field: Field to correct.
        value: New value. Dates must be YYYY-MM-DD.
        event_id: The event being handled, if any.
        agent_name: Who is making the change.
        reason: How the value was worked out (plain language).
    """
    _require_choice("entity_type", entity_type, list(FIXABLE_FIELDS))
    table, key, fields = FIXABLE_FIELDS[entity_type]
    _require_choice(f"field for {entity_type}", field, list(fields))
    value = _require_text("value", value, 200)
    if fields[field] == "date":
        _require_iso_date(field, value)
    agent_name = _require_text("agent_name", agent_name, 64)
    key_value = entity_id if entity_type == "site" else _as_int("entity_id", entity_id)
    with connect() as conn:
        _require_event(conn, event_id)
        row = _require_row(conn, table, key, key_value)
        # table, key and field all come from FIXABLE_FIELDS, never from the caller.
        conn.execute(f"UPDATE {table} SET {field} = ? WHERE {key} = ?", (value, key_value))
        outcome = f"{entity_type} {entity_id}.{field}: {row[field]!r} -> {value!r}"
        _audit(conn, "fix_record_field", event_id, agent_name, reason or "data quality fix", outcome)
    return {"entity_type": entity_type, "entity_id": entity_id, "field": field,
            "previous_value": row[field], "value": value}


def _as_int(name: str, value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ToolError(f"{name} must be a number for this entity type, got {value!r}.")


@mcp.tool()
def create_notification(
    event_id: int,
    severity: str,
    recipient_role: str,
    message: str,
    status: str = "pending",
    agent_name: str = "manual",
) -> dict[str, Any]:
    """Create a notification for a role.

    Use status "pending" to notify, or "flagged" to flag an issue for a human
    (flagged needs medium or high severity).

    Args:
        event_id: The event this notification is about.
        severity: low, medium or high.
        recipient_role: site_coordinator, study_manager, regulatory_specialist,
            data_manager or clinical_operations_lead.
        message: What the recipient needs to know or do.
        status: "pending" (notify) or "flagged" (flag).
        agent_name: Who is sending it.
    """
    rules = _rules()
    _require_choice("severity", severity, rules["severities"])
    _require_choice("recipient_role", recipient_role, rules["recipient_roles"])
    _require_choice("status", status, ["pending", "flagged"])
    if status == "flagged" and severity == "low":
        raise ToolError("A flagged notification must have medium or high severity.")
    message = _require_text("message", message)
    agent_name = _require_text("agent_name", agent_name, 64)
    with connect() as conn:
        _require_event(conn, event_id)
        cur = conn.execute(
            "INSERT INTO notifications (event_id, severity, recipient_role, message, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (event_id, severity, recipient_role, message, status, now()),
        )
        notification_id = cur.lastrowid
        _audit(conn, "create_notification", event_id, agent_name, message,
               f"notification {notification_id}: {status} {severity} -> {recipient_role}")
    return {"notification_id": notification_id, "status": status, "severity": severity,
            "recipient_role": recipient_role}


@mcp.tool()
def log_agent_action(
    event_id: int, agent_name: str, action_type: str, reasoning: str, outcome: str
) -> dict[str, Any]:
    """Record an agent's decision about an event, with its reasoning in plain language.

    Call this once per event with the decision you made.

    Args:
        event_id: The event the decision is about.
        agent_name: Who decided.
        action_type: auto_resolve, notify, flag, log_only or error.
        reasoning: Why, citing the facts checked (plain language).
        outcome: Short description of the result.
    """
    _require_choice("action_type", action_type, _rules()["decision_types"])
    agent_name = _require_text("agent_name", agent_name, 64)
    reasoning = _require_text("reasoning", reasoning, 2000)
    outcome = _require_text("outcome", outcome, 500)
    with connect() as conn:
        _require_event(conn, event_id)
        cur = conn.execute(
            "INSERT INTO agent_actions (event_id, agent_name, action_type, reasoning, outcome, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (event_id, agent_name, action_type, reasoning, outcome, now()),
        )
    return {"action_id": cur.lastrowid, "event_id": event_id, "action_type": action_type}


@mcp.tool()
def mark_event_processed(event_id: int, agent_name: str = "supervisor") -> dict[str, Any]:
    """Mark an event as processed. Fails if it was already processed, so no event is handled twice.

    Args:
        event_id: The event to mark.
        agent_name: Who is marking it (normally the supervisor).
    """
    agent_name = _require_text("agent_name", agent_name, 64)
    with connect() as conn:
        _require_event(conn, event_id)
        processed_at = now()
        # The processed = 0 condition makes this safe against two callers racing.
        cur = conn.execute(
            "UPDATE events SET processed = 1, processed_at = ? WHERE event_id = ? AND processed = 0",
            (processed_at, event_id),
        )
        if cur.rowcount == 0:
            raise ToolError(f"Event {event_id} was already processed.")
        _audit(conn, "mark_event_processed", event_id, agent_name, "event handled", f"event {event_id} processed")
    return {"event_id": event_id, "processed": True, "processed_at": processed_at}


# --- resources ---------------------------------------------------------------

@mcp.resource("study://rules", name="decision_rules", mime_type="text/yaml",
              description="Decision rules, routing and allowed values (config/rules.yaml).")
def rules_resource() -> str:
    return (CONFIG_DIR / "rules.yaml").read_text(encoding="utf-8")


@mcp.resource("study://reference", name="reference_data", mime_type="text/yaml",
              description="Reference data, including the city to country map for inferring a blank country.")
def reference_resource() -> str:
    return (CONFIG_DIR / "reference.yaml").read_text(encoding="utf-8")


@mcp.resource("study://schema", name="schema", mime_type="text/markdown",
              description="Database tables and the allowed values for statuses and types.")
def schema_resource() -> str:
    def bullet(name: str, values: list[str]) -> str:
        return f"- **{name}:** " + ", ".join(values)

    return "\n".join([
        "# Study start-up data model",
        "",
        "Agents read and write this data only through this server's tools.",
        "",
        "## Allowed values",
        bullet("Site status", SITE_STATUSES),
        bullet("Milestone types (in order)", MILESTONE_TYPES),
        bullet("Milestone status", ["Pending", "Completed", "Delayed"]),
        bullet("Document types", DOC_TYPES),
        bullet("Document status", DOC_STATUSES),
        bullet("Staff roles", STAFF_ROLES),
        "- **Dates:** YYYY-MM-DD",
        "",
        "## Tables",
        "```sql",
        SCHEMA.strip(),
        "```",
    ])


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m mcp_server.server")
    parser.add_argument("--transport", choices=["streamable-http", "stdio"], default="streamable-http")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if args.transport == "stdio":
        mcp.run("stdio")
    else:
        print(f"MCP server on http://127.0.0.1:{args.port}/mcp  (Ctrl+C to stop)")
        mcp.run("streamable-http", host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
