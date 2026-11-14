"""Stage 3: supervisor + Document agent end to end, through a real MCP server.

The MCP server runs as its own process (on a test port, against the test
database), exactly as in production. The OpenAI code path is exercised with a
scripted stand-in model, so these tests need no API key.
"""

import asyncio
import os
import subprocess
import sys
import time
import urllib.request
from datetime import date
from pathlib import Path

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from agents.llm_agents import LlmSpecialist, system_prompt
from agents.mcp_tools import open_tools
from agents.rules_engine import RULE_SPECIALISTS
from agents.supervisor import Supervisor
from common.config import load_config
from common.db import connect
from generator.live import generate_one, make_context
from generator.seed import run_seed

PORT = 8765
URL = f"http://127.0.0.1:{PORT}/mcp"
SETTINGS = {"poll_seconds": 0.2, "batch_size": 20, "concurrency": 4, "event_timeout_seconds": 20}


@pytest.fixture(scope="module", autouse=True)
def mcp_server():
    root = Path(__file__).resolve().parent.parent
    proc = subprocess.Popen(
        [sys.executable, "-m", "mcp_server.server", "--port", str(PORT)],
        cwd=root, env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            urllib.request.urlopen(URL, timeout=1)
        except urllib.error.HTTPError:
            break  # the server is up (it rejects a plain GET)
        except OSError:
            time.sleep(0.2)
    yield
    proc.terminate()
    proc.wait(timeout=10)


@pytest.fixture(autouse=True)
def fresh_db(capsys):
    run_seed(reset=True)
    capsys.readouterr()


async def run_supervisor(specialists_for):
    async with open_tools(URL) as tools:
        config = await tools.rules()
        specialists = specialists_for(tools, config)
        await Supervisor(tools, specialists, SETTINGS, config["routing"]).run(once=True)


def rule_specialists(tools, config):
    return dict(RULE_SPECIALISTS)


def llm_specialist(model):
    return lambda tools, config: {"document_agent": LlmSpecialist("document_agent", model, tools, config, {})}


def decisions() -> dict[int, tuple[str, str]]:
    with connect() as conn:
        return {r[0]: (r[1], r[2]) for r in conn.execute(
            "SELECT event_id, agent_name, action_type FROM agent_actions WHERE action_type NOT LIKE 'tool:%'")}


def notifications() -> dict[int, tuple[str, str, str]]:
    with connect() as conn:
        return {r[0]: (r[1], r[2], r[3]) for r in conn.execute(
            "SELECT event_id, status, severity, recipient_role FROM notifications")}


def processed_counts() -> dict[int, int]:
    with connect() as conn:
        return dict(conn.execute(
            "SELECT event_id, COUNT(*) FROM agent_actions WHERE action_type = 'tool:mark_event_processed' "
            "GROUP BY event_id").fetchall())


def unprocessed() -> int:
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM events WHERE processed = 0").fetchone()[0]


@pytest.mark.anyio
async def test_rules_mode_document_agent_follows_every_rule():
    ctx = make_context()
    e = {
        "submitted": generate_one(ctx, "document_submitted", site_id="SITE-001"),
        "expiring_soon": generate_one(ctx, "document_expiring", site_id="SITE-002", days=12),
        "expiring_later": generate_one(ctx, "document_expiring", site_id="SITE-003", days=45),
        "expired": generate_one(ctx, "document_expired", site_id="SITE-004", days=6),
        "rejected_1": generate_one(ctx, "document_rejected", site_id="SITE-005", rejections=1),
        "rejected_3": generate_one(ctx, "document_rejected", site_id="SITE-006", rejections=3),
        "other": generate_one(ctx, "milestone_delayed", site_id="SITE-007", days=9),
    }
    ids = {k: v["event_id"] for k, v in e.items()}

    await run_supervisor(rule_specialists)

    d, n = decisions(), notifications()
    assert d[ids["submitted"]] == ("document_agent", "log_only")
    assert d[ids["expiring_soon"]] == ("document_agent", "notify")
    assert n[ids["expiring_soon"]] == ("pending", "low", "regulatory_specialist")
    assert d[ids["expiring_later"]] == ("document_agent", "log_only")
    assert d[ids["expired"]] == ("document_agent", "flag")
    assert n[ids["expired"]] == ("flagged", "high", "regulatory_specialist")
    assert d[ids["rejected_1"]] == ("document_agent", "notify")
    assert n[ids["rejected_1"]] == ("pending", "low", "site_coordinator")
    assert d[ids["rejected_3"]] == ("document_agent", "flag")
    assert n[ids["rejected_3"]] == ("flagged", "high", "study_manager")
    assert d[ids["other"]] == ("milestone_site_agent", "flag")  # 9 days late
    assert set(n) == {ids["expiring_soon"], ids["expired"], ids["rejected_1"], ids["rejected_3"], ids["other"]}
    with connect() as conn:
        status = conn.execute("SELECT status FROM regulatory_documents WHERE doc_id = ?",
                              (e["expired"]["payload"]["doc_id"],)).fetchone()[0]
    assert status == "Expired"
    assert unprocessed() == 0
    assert set(processed_counts().values()) == {1}


class ScriptedModel(FakeMessagesListChatModel):
    """Stands in for ChatOpenAI: replies with a fixed list of messages, including tool calls."""

    def bind_tools(self, tools, **kwargs):
        return self


def tool_call(name: str, **args) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"call_{name}"}])


@pytest.mark.anyio
async def test_openai_code_path_runs_tools_through_mcp():
    event = generate_one(make_context(), "document_expired", site_id="SITE-008", days=4)
    eid, doc_id = event["event_id"], event["payload"]["doc_id"]
    script = [
        tool_call("get_site", site_id="SITE-008"),
        tool_call("list_site_documents", site_id="SITE-008"),
        tool_call("update_document_status", doc_id=doc_id, status="Expired", note="Expired 4 days ago",
                  event_id=eid, agent_name="document_agent"),
        tool_call("create_notification", event_id=eid, severity="high", recipient_role="regulatory_specialist",
                  message="Document expired", status="flagged", agent_name="document_agent"),
        tool_call("log_agent_action", event_id=eid, agent_name="document_agent", action_type="flag",
                  reasoning="Expired 4 days ago; the rules always flag an expired document.", outcome="Flagged"),
        AIMessage(content='{"outcome": "flag", "summary": "Flagged expired document"}'),
    ]
    model = ScriptedModel(responses=script)

    await run_supervisor(llm_specialist(model))

    assert decisions()[eid] == ("document_agent", "flag")
    assert notifications()[eid] == ("flagged", "high", "regulatory_specialist")
    assert processed_counts() == {eid: 1}


@pytest.mark.anyio
async def test_agent_that_forgets_to_log_is_recorded_as_error():
    eid = generate_one(make_context(), "document_submitted", site_id="SITE-009")["event_id"]
    model = ScriptedModel(responses=[AIMessage(content="All done!")])

    await run_supervisor(llm_specialist(model))

    assert decisions()[eid] == ("supervisor", "error")
    assert processed_counts() == {eid: 1}


@pytest.mark.anyio
async def test_failing_agent_is_logged_and_the_loop_carries_on():
    ctx = make_context()
    bad = generate_one(ctx, "document_submitted", site_id="SITE-010")["event_id"]
    good = generate_one(ctx, "document_expiring", site_id="SITE-011", days=50)["event_id"]
    calls = []

    async def flaky_agent(event, tools):
        calls.append(event["event_id"])
        if event["event_id"] == bad:
            raise RuntimeError("model service unavailable")
        return await RULE_SPECIALISTS["document_agent"](event, tools)

    await run_supervisor(lambda tools, config: {"document_agent": flaky_agent})

    d = decisions()
    assert d[bad] == ("supervisor", "error")
    assert d[good] == ("document_agent", "log_only")
    assert sorted(calls) == [bad, good]
    assert unprocessed() == 0


@pytest.mark.anyio
async def test_two_supervisors_never_process_an_event_twice():
    ctx = make_context()
    for i in range(12):
        generate_one(ctx, "document_submitted", site_id=f"SITE-{i % 20 + 1:03d}")

    await asyncio.gather(
        run_supervisor(rule_specialists),
        run_supervisor(rule_specialists),
    )

    counts = processed_counts()
    assert len(counts) == 12 and set(counts.values()) == {1}
    assert unprocessed() == 0


@pytest.mark.anyio
async def test_prompts_are_built_from_rules_served_by_mcp():
    async with open_tools(URL) as tools:
        config, reference = await tools.rules(), await tools.read_yaml("study://reference")
    doc = system_prompt("document_agent", config, reference)
    for event_type in ["document_submitted", "document_rejected", "document_expiring", "document_expired"]:
        assert f"## {event_type}" in doc
    assert "notify_within_days: 30" in doc and "flag_at_rejection_count: 3" in doc and '"document_agent"' in doc

    site = system_prompt("milestone_site_agent", config, reference)
    assert "flag_at_days_late: 7" in site and "ready to activate" in site and "Contract Signed" in site

    quality = system_prompt("data_quality_agent", config, reference)
    assert "Madrid: Spain" in quality and "%Y/%m/%d" in quality


# --- Stage 4: Milestone and site agent, Data quality agent ----------------------

def site_with_status(*statuses: str) -> str:
    marks = ",".join("?" * len(statuses))
    with connect() as conn:
        return conn.execute(f"SELECT site_id FROM sites WHERE status IN ({marks}) ORDER BY site_id",
                            statuses).fetchone()[0]


def site_status(site_id: str) -> str:
    with connect() as conn:
        return conn.execute("SELECT status FROM sites WHERE site_id = ?", (site_id,)).fetchone()[0]


@pytest.mark.anyio
async def test_milestone_site_agent_follows_every_rule():
    ctx = make_context()
    early_site = site_with_status("Identified", "Selected")
    e = {
        "late_3": generate_one(ctx, "milestone_delayed", site_id="SITE-001", days=3),
        "late_9": generate_one(ctx, "milestone_delayed", site_id="SITE-002", days=9),
        "approval_early": generate_one(ctx, "approval_received", site_id=early_site),
        "ready": generate_one(ctx, "site_ready_check", site_id="SITE-004", complete=True),
        "not_ready": generate_one(ctx, "site_ready_check", site_id="SITE-005", complete=False),
        "contract_not_ready": generate_one(ctx, "contract_signed", site_id="SITE-006"),
    }
    ids = {k: v["event_id"] for k, v in e.items()}

    await run_supervisor(rule_specialists)

    d, n = decisions(), notifications()
    assert d[ids["late_3"]] == ("milestone_site_agent", "notify")
    assert n[ids["late_3"]] == ("pending", "low", "study_manager")
    assert d[ids["late_9"]] == ("milestone_site_agent", "flag")
    assert n[ids["late_9"]] == ("flagged", "high", "clinical_operations_lead")
    assert d[ids["approval_early"]] == ("milestone_site_agent", "auto_resolve")
    assert site_status(early_site) == "Docs In Progress"
    assert d[ids["ready"]] == ("milestone_site_agent", "auto_resolve")
    assert site_status("SITE-004") == "Ready To Activate"
    assert d[ids["not_ready"]] == ("milestone_site_agent", "notify")
    assert n[ids["not_ready"]] == ("pending", "low", "study_manager")
    assert d[ids["contract_not_ready"]] == ("milestone_site_agent", "log_only")
    assert unprocessed() == 0


@pytest.mark.anyio
async def test_contract_signed_on_a_ready_site_activates_it():
    ctx = make_context()
    generate_one(ctx, "site_ready_check", site_id="SITE-007", complete=True)
    with connect() as conn:  # make it ready except for the contract, then sign the contract
        conn.execute("UPDATE site_milestones SET status = 'Pending', actual_date = NULL "
                     "WHERE site_id = 'SITE-007' AND type = 'Contract Signed'")
        conn.execute("UPDATE events SET processed = 1")
    eid = generate_one(ctx, "contract_signed", site_id="SITE-007")["event_id"]

    await run_supervisor(rule_specialists)

    assert decisions()[eid] == ("milestone_site_agent", "auto_resolve")
    assert site_status("SITE-007") == "Ready To Activate"


@pytest.mark.anyio
async def test_data_quality_agent_follows_every_rule():
    ctx = make_context()
    e = {
        "country": generate_one(ctx, "record_data_issue", site_id="SITE-008", variant="site_country_blank"),
        "date_fmt": generate_one(ctx, "record_data_issue", site_id="SITE-009", variant="milestone_date_malformed"),
        "date_tbd": generate_one(ctx, "record_data_issue", site_id="SITE-010", variant="milestone_date_garbage"),
        "name": generate_one(ctx, "record_data_issue", site_id="SITE-011", variant="staff_name_blank"),
        "training": generate_one(ctx, "staff_training_expired", site_id="SITE-012"),
    }
    ids = {k: v["event_id"] for k, v in e.items()}
    with connect() as conn:
        site_name = conn.execute("SELECT name FROM sites WHERE site_id = 'SITE-008'").fetchone()[0]

    await run_supervisor(rule_specialists)

    d, n = decisions(), notifications()
    assert d[ids["country"]] == ("data_quality_agent", "auto_resolve")
    assert d[ids["date_fmt"]] == ("data_quality_agent", "auto_resolve")
    assert d[ids["date_tbd"]] == ("data_quality_agent", "notify")
    assert n[ids["date_tbd"]] == ("pending", "medium", "data_manager")
    assert d[ids["name"]] == ("data_quality_agent", "notify")
    assert n[ids["name"]] == ("pending", "medium", "data_manager")
    assert d[ids["training"]] == ("data_quality_agent", "flag")
    assert n[ids["training"]] == ("flagged", "medium", "site_coordinator")
    assert ids["country"] not in n and ids["date_fmt"] not in n  # auto-fixed, nobody bothered

    with connect() as conn:
        country = conn.execute("SELECT country FROM sites WHERE site_id = 'SITE-008'").fetchone()[0]
        fixed_date = conn.execute("SELECT planned_date FROM site_milestones WHERE milestone_id = ?",
                                  (e["date_fmt"]["payload"]["entity_id"],)).fetchone()[0]
    city_country = load_config("reference")["city_country"]
    [city] = [c for c in city_country if c in site_name]
    assert country == city_country[city]
    assert date.fromisoformat(fixed_date)  # now a real YYYY-MM-DD date


@pytest.mark.anyio
async def test_current_state_wins_over_the_event():
    """If someone fixed the problem after the event was raised, the agent just logs it."""
    ctx = make_context()
    country = generate_one(ctx, "record_data_issue", site_id="SITE-013", variant="site_country_blank")
    training = generate_one(ctx, "staff_training_expired", site_id="SITE-014")
    with connect() as conn:
        conn.execute("UPDATE sites SET country = 'Fixed By Hand' WHERE site_id = 'SITE-013'")
        conn.execute("UPDATE site_staff SET gcp_training_expiry = '2099-01-01' WHERE staff_id = ?",
                     (training["payload"]["staff_id"],))

    await run_supervisor(rule_specialists)

    d = decisions()
    assert d[country["event_id"]] == ("data_quality_agent", "log_only")
    assert d[training["event_id"]] == ("data_quality_agent", "log_only")
    assert notifications() == {}


@pytest.mark.anyio
async def test_every_event_type_has_a_specialist_and_all_outcomes_appear():
    ctx = make_context()
    from generator.events import ACTIONS
    for i, event_type in enumerate(ACTIONS):
        generate_one(ctx, event_type, site_id=f"SITE-{i + 1:03d}")
    generate_one(ctx, "site_ready_check", site_id="SITE-019", complete=True)

    await run_supervisor(rule_specialists)

    d = decisions()
    assert {agent for agent, _ in d.values()} == {"document_agent", "milestone_site_agent", "data_quality_agent"}
    assert {"auto_resolve", "notify", "flag"} <= {outcome for _, outcome in d.values()}
    assert "error" not in {outcome for _, outcome in d.values()}
    assert unprocessed() == 0
