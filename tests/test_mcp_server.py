"""Stage 2 checkpoint: every MCP tool tested on its own, with no agent.

Tools are called through a real MCP client connected in-process, so this
exercises the same schemas, validation and error handling an agent sees.
"""

import pytest
from mcp import Client

from common.db import connect
from generator.live import generate_one, make_context
from generator.seed import run_seed
from mcp_server.server import mcp

READ_TOOLS = {
    "get_unprocessed_events", "get_site", "list_site_documents",
    "list_site_milestones", "list_site_staff", "get_study_summary",
}
WRITE_TOOLS = {
    "update_document_status", "update_site_status", "fix_record_field",
    "create_notification", "log_agent_action", "mark_event_processed",
}


@pytest.fixture(autouse=True)
def fresh_db(capsys):
    run_seed(reset=True)
    capsys.readouterr()  # hide the seed summary


@pytest.fixture
def ctx():
    return make_context()


async def call(tool: str, **args) -> dict:
    """Call a tool and return its structured result; fail the test on a tool error."""
    async with Client(mcp) as client:
        result = await client.call_tool(tool, args)
    assert not result.is_error, result.content[0].text
    return result.structured_content


async def call_error(tool: str, **args) -> str:
    """Call a tool that should fail and return its error message."""
    async with Client(mcp) as client:
        result = await client.call_tool(tool, args)
    assert result.is_error, f"{tool} should have failed but returned {result.structured_content}"
    return result.content[0].text


def actions(event_id=None) -> list[dict]:
    with connect() as conn:
        sql, params = "SELECT * FROM agent_actions", ()
        if event_id is not None:
            sql, params = sql + " WHERE event_id = ?", (event_id,)
        return [dict(r) for r in conn.execute(sql + " ORDER BY action_id", params)]


def doc_of_type(site_id: str, doc_type: str) -> dict:
    with connect() as conn:
        return dict(conn.execute(
            "SELECT * FROM regulatory_documents WHERE site_id = ? AND doc_type = ?", (site_id, doc_type)
        ).fetchone())


# --- the server's surface ------------------------------------------------------

@pytest.mark.anyio
async def test_server_exposes_all_tools_and_resources_and_nothing_deletes():
    async with Client(mcp) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        uris = {str(r.uri) for r in (await client.list_resources()).resources}
        rules = await client.read_resource("study://rules")
    assert names == READ_TOOLS | WRITE_TOOLS
    assert not any("delete" in n or "remove" in n for n in names)
    assert uris == {"study://rules", "study://reference", "study://schema"}
    assert "notify_within_days: 30" in rules.contents[0].text


# --- read tools -----------------------------------------------------------------

@pytest.mark.anyio
async def test_get_unprocessed_events_oldest_first_with_parsed_payload(ctx):
    first = generate_one(ctx, "document_expired", site_id="SITE-001")
    generate_one(ctx, "milestone_delayed", site_id="SITE-002")
    result = await call("get_unprocessed_events", limit=1)
    assert result["unprocessed_total"] == 2
    [event] = result["events"]
    assert event["event_id"] == first["event_id"]
    assert event["payload"]["site_id"] == "SITE-001"  # JSON already decoded
    assert "between 1 and 100" in await call_error("get_unprocessed_events", limit=0)


@pytest.mark.anyio
async def test_get_site_reports_readiness(ctx):
    result = await call("get_site", site_id="SITE-001")
    assert result["site"]["site_id"] == "SITE-001"
    assert result["study"]["study_id"] == result["site"]["study_id"]
    assert result["readiness"]["ready_to_activate"] is False
    assert result["readiness"]["missing"]

    generate_one(ctx, "site_ready_check", site_id="SITE-001", complete=True)
    result = await call("get_site", site_id="SITE-001")
    assert result["readiness"] == {"ready_to_activate": True, "missing": []}

    assert "SITE-999" in await call_error("get_site", site_id="SITE-999")


@pytest.mark.anyio
async def test_list_site_documents_shows_days_until_expiry(ctx):
    event = generate_one(ctx, "document_expiring", site_id="SITE-002", days=12)
    docs = (await call("list_site_documents", site_id="SITE-002"))["documents"]
    assert len(docs) == 7
    [doc] = [d for d in docs if d["doc_id"] == event["payload"]["doc_id"]]
    assert doc["days_until_expiry"] == 12


@pytest.mark.anyio
async def test_list_site_milestones_shows_days_late_and_bad_dates(ctx):
    delayed = generate_one(ctx, "milestone_delayed", site_id="SITE-003", days=9)
    garbage = generate_one(ctx, "record_data_issue", site_id="SITE-003", variant="milestone_date_garbage")
    ms = {m["milestone_id"]: m for m in (await call("list_site_milestones", site_id="SITE-003"))["milestones"]}
    assert len(ms) == 7
    assert ms[delayed["payload"]["milestone_id"]]["days_late"] == 9
    assert ms[garbage["payload"]["entity_id"]]["planned_date_valid"] is False


@pytest.mark.anyio
async def test_list_site_staff_flags_expired_training(ctx):
    event = generate_one(ctx, "staff_training_expired", site_id="SITE-004")
    staff = {s["staff_id"]: s for s in (await call("list_site_staff", site_id="SITE-004"))["staff"]}
    assert staff[event["payload"]["staff_id"]]["training_expired"] is True
    assert sum(s["training_expired"] for s in staff.values()) == 1


@pytest.mark.anyio
async def test_get_study_summary(ctx):
    generate_one(ctx, "document_expired", site_id="SITE-001")  # SITE-001 belongs to STU-001
    result = await call("get_study_summary", study_id="STU-001")
    assert result["sites_total"] == 10
    assert sum(result["sites_by_status"].values()) == 10
    assert result["unprocessed_events"] == 1
    assert "STU-404" in await call_error("get_study_summary", study_id="STU-404")


# --- write tools ----------------------------------------------------------------

@pytest.mark.anyio
async def test_update_document_status_records_rejections_and_audits(ctx):
    event = generate_one(ctx, "document_submitted", site_id="SITE-005")
    doc_id = event["payload"]["doc_id"]
    before = doc_of_type("SITE-005", event["payload"]["doc_type"])["rejection_count"]

    result = await call("update_document_status", doc_id=doc_id, status="Rejected", note="Unsigned",
                        event_id=event["event_id"], agent_name="document_agent")
    assert result["changed"] is True
    doc = doc_of_type("SITE-005", event["payload"]["doc_type"])
    assert (doc["status"], doc["reviewer_comment"], doc["rejection_count"]) == ("Rejected", "Unsigned", before + 1)

    [audit] = actions(event["event_id"])
    assert (audit["agent_name"], audit["action_type"]) == ("document_agent", "tool:update_document_status")


@pytest.mark.anyio
async def test_update_document_status_validation(ctx):
    doc = doc_of_type("SITE-005", "GCP certificate")
    assert "Allowed:" in await call_error("update_document_status", doc_id=doc["doc_id"], status="Lost", note="x")
    assert "note must not be empty" in await call_error(
        "update_document_status", doc_id=doc["doc_id"], status="Approved", note="  ")
    assert "doc_id = 99999" in await call_error("update_document_status", doc_id=99999, status="Approved", note="x")
    assert "Unknown event_id" in await call_error(
        "update_document_status", doc_id=doc["doc_id"], status="Approved", note="x", event_id=99999)
    # Expired is only allowed once the expiry date has passed.
    expiring = generate_one(ctx, "document_expiring", site_id="SITE-005", days=10)
    assert "has not passed" in await call_error(
        "update_document_status", doc_id=expiring["payload"]["doc_id"], status="Expired", note="x")
    expired = generate_one(ctx, "document_expired", site_id="SITE-005", days=3)
    await call("update_document_status", doc_id=expired["payload"]["doc_id"], status="Expired", note="Past expiry")
    assert actions() and all(a["action_type"] == "tool:update_document_status" for a in actions())


@pytest.mark.anyio
async def test_update_site_status_guards_ready_to_activate(ctx):
    message = await call_error("update_site_status", site_id="SITE-006", status="Ready To Activate", note="try")
    assert "not ready to activate" in message and "Missing:" in message
    assert "must be Ready To Activate before Activated" in await call_error(
        "update_site_status", site_id="SITE-006", status="Activated", note="try")

    event = generate_one(ctx, "site_ready_check", site_id="SITE-006", complete=True)
    result = await call("update_site_status", site_id="SITE-006", status="Ready To Activate",
                        note="All checks pass", event_id=event["event_id"], agent_name="milestone_site_agent")
    assert result["status"] == "Ready To Activate"
    assert (await call("get_site", site_id="SITE-006"))["site"]["status"] == "Ready To Activate"
    assert "Allowed:" in await call_error("update_site_status", site_id="SITE-006", status="Live", note="x")
    assert [a["action_type"] for a in actions(event["event_id"])] == ["tool:update_site_status"]


@pytest.mark.anyio
async def test_fix_record_field_fixes_allowed_fields_only(ctx):
    event = generate_one(ctx, "record_data_issue", site_id="SITE-007", variant="site_country_blank")
    result = await call("fix_record_field", entity_type="site", entity_id="SITE-007", field="country",
                        value="Spain", event_id=event["event_id"], agent_name="data_quality_agent",
                        reason="City in site name")
    assert (result["previous_value"], result["value"]) == ("", "Spain")
    assert (await call("get_site", site_id="SITE-007"))["site"]["country"] == "Spain"
    [audit] = actions(event["event_id"])
    assert audit["reasoning"] == "City in site name" and "'' -> 'Spain'" in audit["outcome"]

    bad_date = generate_one(ctx, "record_data_issue", site_id="SITE-007", variant="milestone_date_malformed")
    milestone_id = str(bad_date["payload"]["entity_id"])
    assert "YYYY-MM-DD" in await call_error(
        "fix_record_field", entity_type="milestone", entity_id=milestone_id, field="planned_date", value="2026/12/01")
    assert "YYYY-MM-DD" in await call_error(
        "fix_record_field", entity_type="milestone", entity_id=milestone_id, field="planned_date", value="2026-02-30")
    await call("fix_record_field", entity_type="milestone", entity_id=milestone_id, field="planned_date",
               value="2026-12-01")
    # Not on the allow-list: unknown fields, other tables, SQL in the field name.
    assert "Invalid field" in await call_error(
        "fix_record_field", entity_type="site", entity_id="SITE-007", field="status", value="Activated")
    assert "Invalid field" in await call_error(
        "fix_record_field", entity_type="site", entity_id="SITE-007", field="country = 'x'; --", value="x")
    assert "Invalid entity_type" in await call_error(
        "fix_record_field", entity_type="events", entity_id="1", field="processed", value="1")
    assert "must be a number" in await call_error(
        "fix_record_field", entity_type="staff", entity_id="abc", field="name", value="Ann Lee")


@pytest.mark.anyio
async def test_create_notification_notify_and_flag(ctx):
    event = generate_one(ctx, "document_expired", site_id="SITE-008")
    notify = await call("create_notification", event_id=event["event_id"], severity="low",
                        recipient_role="regulatory_specialist", message="Renew soon")
    flag = await call("create_notification", event_id=event["event_id"], severity="high",
                      recipient_role="regulatory_specialist", message="Expired", status="flagged",
                      agent_name="document_agent")
    assert (notify["status"], flag["status"]) == ("pending", "flagged")
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 2
    assert len(actions(event["event_id"])) == 2

    e = event["event_id"]
    assert "medium or high" in await call_error("create_notification", event_id=e, severity="low",
                                                recipient_role="data_manager", message="x", status="flagged")
    assert "Invalid severity" in await call_error("create_notification", event_id=e, severity="urgent",
                                                  recipient_role="data_manager", message="x")
    assert "Invalid recipient_role" in await call_error("create_notification", event_id=e, severity="low",
                                                        recipient_role="ceo", message="x")
    assert "Unknown event_id" in await call_error("create_notification", event_id=99999, severity="low",
                                                  recipient_role="data_manager", message="x")


@pytest.mark.anyio
async def test_log_agent_action_records_decision(ctx):
    event = generate_one(ctx, "document_expiring", site_id="SITE-009", days=45)
    result = await call("log_agent_action", event_id=event["event_id"], agent_name="document_agent",
                        action_type="log_only", reasoning="45 days left, above the 30-day threshold.",
                        outcome="No notification needed")
    [row] = actions(event["event_id"])
    assert row["action_id"] == result["action_id"] and row["action_type"] == "log_only"
    assert "Invalid action_type" in await call_error(
        "log_agent_action", event_id=event["event_id"], agent_name="a", action_type="shrug",
        reasoning="r", outcome="o")
    assert "reasoning must not be empty" in await call_error(
        "log_agent_action", event_id=event["event_id"], agent_name="a", action_type="notify",
        reasoning="", outcome="o")


@pytest.mark.anyio
async def test_mark_event_processed_exactly_once(ctx):
    event = generate_one(ctx, "document_submitted", site_id="SITE-010")
    result = await call("mark_event_processed", event_id=event["event_id"])
    assert result["processed"] is True
    assert (await call("get_unprocessed_events"))["unprocessed_total"] == 0
    assert "already processed" in await call_error("mark_event_processed", event_id=event["event_id"])
    assert "Unknown event_id" in await call_error("mark_event_processed", event_id=99999)
    # Only the successful call is audited.
    assert [a["action_type"] for a in actions(event["event_id"])] == ["tool:mark_event_processed"]
    with connect() as conn:
        assert conn.execute("SELECT processed_at FROM events WHERE event_id = ?",
                            (event["event_id"],)).fetchone()[0]


@pytest.mark.anyio
async def test_failed_writes_change_nothing_and_leave_no_audit_row():
    site = (await call("get_site", site_id="SITE-011"))["site"]
    await call_error("update_site_status", site_id="SITE-011", status="Ready To Activate", note="x")
    assert (await call("get_site", site_id="SITE-011"))["site"]["status"] == site["status"]
    assert actions() == []
