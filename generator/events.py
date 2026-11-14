"""Event actions for the real-time generator.

Each action changes study data and writes one row to the events table, in a
single transaction. Every action accepts an optional site_id so the inject
command can target a specific site, plus action-specific options (days,
rejections, variant, complete). An action returns the event as a dict, or
None if there was nothing suitable to change.
"""

import json
import random
from dataclasses import dataclass
from datetime import date, timedelta
from sqlite3 import Connection, Row

from faker import Faker

from common.db import now
from common.domain import EXPIRING_DOC_TYPES, MILESTONE_TYPES


@dataclass
class Context:
    rng: random.Random
    fake: Faker
    cfg: dict

    @property
    def problems(self) -> dict:
        return self.cfg["problems"]

    def days_in(self, key: str) -> int:
        low, high = self.problems[key]
        return self.rng.randint(low, high)


def emit(conn: Connection, event_type: str, entity_type: str, entity_id, payload: dict) -> dict:
    created_at = now()
    cur = conn.execute(
        "INSERT INTO events (event_type, entity_type, entity_id, payload, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (event_type, entity_type, str(entity_id), json.dumps(payload), created_at),
    )
    return {
        "event_id": cur.lastrowid,
        "event_type": event_type,
        "entity_type": entity_type,
        "entity_id": str(entity_id),
        "payload": payload,
        "created_at": created_at,
    }


def _pick(conn: Connection, ctx: Context, table: str, site_id: str | None, wheres: list[str]) -> Row | None:
    """Return a random row matching the first WHERE clause that finds anything.

    With a site_id (inject), a final fallback picks any row for that site, so
    an on-demand inject always produces its event.
    """
    site_filter = " AND site_id = ?" if site_id else ""
    params = (site_id,) if site_id else ()
    for where in wheres + (["1=1"] if site_id else []):
        rows = conn.execute(f"SELECT * FROM {table} WHERE ({where}){site_filter}", params).fetchall()
        if rows:
            return ctx.rng.choice(rows)
    return None


def _expiring_types_sql() -> str:
    return ", ".join(f"'{t}'" for t in EXPIRING_DOC_TYPES)


def _today() -> date:
    return date.today()


# --- Document events ---------------------------------------------------------

def document_submitted(conn, ctx, site_id=None, **_):
    doc = _pick(conn, ctx, "regulatory_documents", site_id, ["status IN ('Not Started', 'Rejected')"])
    if not doc:
        return None
    conn.execute(
        "UPDATE regulatory_documents SET status = 'Submitted', submitted_at = ? WHERE doc_id = ?",
        (now(), doc["doc_id"]),
    )
    return emit(conn, "document_submitted", "document", doc["doc_id"], {
        "doc_id": doc["doc_id"], "site_id": doc["site_id"], "doc_type": doc["doc_type"],
        "previous_status": doc["status"],
    })


def document_rejected(conn, ctx, site_id=None, rejections=None, **_):
    wheres = ["status = 'Submitted'", "status != 'Approved'"]
    if rejections is None and ctx.rng.random() < ctx.problems["repeat_rejection_probability"]:
        wheres.insert(0, "status = 'Submitted' AND rejection_count >= 2")
    doc = _pick(conn, ctx, "regulatory_documents", site_id, wheres)
    if not doc:
        return None
    count = rejections if rejections is not None else doc["rejection_count"] + 1
    reason = ctx.rng.choice(ctx.cfg["rejection_reasons"])
    conn.execute(
        "UPDATE regulatory_documents SET status = 'Rejected', reviewer_comment = ?, "
        "rejection_count = ?, submitted_at = COALESCE(submitted_at, ?) WHERE doc_id = ?",
        (reason, count, now(), doc["doc_id"]),
    )
    return emit(conn, "document_rejected", "document", doc["doc_id"], {
        "doc_id": doc["doc_id"], "site_id": doc["site_id"], "doc_type": doc["doc_type"],
        "reason": reason, "rejection_count": count,
    })


def _approved_expiring_doc(conn, ctx, site_id):
    types = _expiring_types_sql()
    doc = _pick(conn, ctx, "regulatory_documents", site_id, [
        f"status = 'Approved' AND doc_type IN ({types})",
        f"doc_type IN ({types})",
    ])
    if doc and doc["status"] != "Approved":  # inject fallback: approve it first
        conn.execute(
            "UPDATE regulatory_documents SET status = 'Approved', "
            "submitted_at = COALESCE(submitted_at, ?) WHERE doc_id = ?",
            (now(), doc["doc_id"]),
        )
    return doc


def document_expiring(conn, ctx, site_id=None, days=None, **_):
    doc = _approved_expiring_doc(conn, ctx, site_id)
    if not doc:
        return None
    days_left = days if days is not None else ctx.days_in("expiring_days_range")
    expiry = (_today() + timedelta(days=days_left)).isoformat()
    conn.execute("UPDATE regulatory_documents SET expiry_date = ? WHERE doc_id = ?", (expiry, doc["doc_id"]))
    return emit(conn, "document_expiring", "document", doc["doc_id"], {
        "doc_id": doc["doc_id"], "site_id": doc["site_id"], "doc_type": doc["doc_type"],
        "expiry_date": expiry, "days_left": days_left,
    })


def document_expired(conn, ctx, site_id=None, days=None, **_):
    doc = _approved_expiring_doc(conn, ctx, site_id)
    if not doc:
        return None
    days_ago = days if days is not None else ctx.days_in("expired_days_ago_range")
    expiry = (_today() - timedelta(days=days_ago)).isoformat()
    # Status stays Approved: deciding what to do about it is the agent's job.
    conn.execute("UPDATE regulatory_documents SET expiry_date = ? WHERE doc_id = ?", (expiry, doc["doc_id"]))
    return emit(conn, "document_expired", "document", doc["doc_id"], {
        "doc_id": doc["doc_id"], "site_id": doc["site_id"], "doc_type": doc["doc_type"],
        "expiry_date": expiry, "days_overdue": days_ago,
    })


# --- Milestone and site events -----------------------------------------------

def _complete_milestone(conn, site_id: str, m_type: str) -> None:
    conn.execute(
        "UPDATE site_milestones SET status = 'Completed', actual_date = ? "
        "WHERE site_id = ? AND type = ? AND status != 'Completed'",
        (_today().isoformat(), site_id, m_type),
    )


def approval_received(conn, ctx, site_id=None, **_):
    wheres = [
        "doc_type = 'Ethics/IRB approval' AND status = 'Submitted'",
        "doc_type = 'Ethics/IRB approval' AND status != 'Approved'",
    ]
    if site_id:  # inject: an already-approved site gets a renewed approval
        wheres.append("doc_type = 'Ethics/IRB approval'")
    doc = _pick(conn, ctx, "regulatory_documents", site_id, wheres)
    if not doc or doc["doc_type"] != "Ethics/IRB approval":
        return None
    expiry = (_today() + timedelta(days=ctx.rng.randint(365, 730))).isoformat()
    conn.execute(
        "UPDATE regulatory_documents SET status = 'Approved', expiry_date = ?, "
        "submitted_at = COALESCE(submitted_at, ?), reviewer_comment = NULL WHERE doc_id = ?",
        (expiry, now(), doc["doc_id"]),
    )
    _complete_milestone(conn, doc["site_id"], "Ethics Submission")
    _complete_milestone(conn, doc["site_id"], "Ethics Approval")
    return emit(conn, "approval_received", "site", doc["site_id"], {
        "site_id": doc["site_id"], "doc_id": doc["doc_id"], "doc_type": doc["doc_type"],
        "milestone": "Ethics Approval", "expiry_date": expiry,
    })


def contract_signed(conn, ctx, site_id=None, **_):
    wheres = [
        "doc_type = 'Clinical trial agreement' AND status = 'Submitted'",
        "doc_type = 'Clinical trial agreement' AND status != 'Approved'",
    ]
    if site_id:  # inject: an already-signed contract is re-confirmed
        wheres.append("doc_type = 'Clinical trial agreement'")
    doc = _pick(conn, ctx, "regulatory_documents", site_id, wheres)
    if not doc or doc["doc_type"] != "Clinical trial agreement":
        return None
    conn.execute(
        "UPDATE regulatory_documents SET status = 'Approved', "
        "submitted_at = COALESCE(submitted_at, ?), reviewer_comment = NULL WHERE doc_id = ?",
        (now(), doc["doc_id"]),
    )
    _complete_milestone(conn, doc["site_id"], "Contract Signed")
    return emit(conn, "contract_signed", "site", doc["site_id"], {
        "site_id": doc["site_id"], "doc_id": doc["doc_id"], "milestone": "Contract Signed",
    })


def milestone_delayed(conn, ctx, site_id=None, days=None, **_):
    # The next pending milestone of a site is the one that slips.
    sites = conn.execute(
        "SELECT DISTINCT site_id FROM site_milestones WHERE status = 'Pending'"
        + (" AND site_id = ?" if site_id else ""),
        (site_id,) if site_id else (),
    ).fetchall()
    if not sites:
        return None
    chosen = ctx.rng.choice(sites)["site_id"]
    ms = conn.execute(
        "SELECT * FROM site_milestones WHERE site_id = ? AND status = 'Pending' "
        "ORDER BY milestone_id LIMIT 1",
        (chosen,),
    ).fetchone()
    days_late = days if days is not None else ctx.days_in("delay_days_range")
    planned = (_today() - timedelta(days=days_late)).isoformat()
    conn.execute(
        "UPDATE site_milestones SET planned_date = ?, status = 'Delayed' WHERE milestone_id = ?",
        (planned, ms["milestone_id"]),
    )
    return emit(conn, "milestone_delayed", "milestone", ms["milestone_id"], {
        "milestone_id": ms["milestone_id"], "site_id": chosen, "type": ms["type"],
        "planned_date": planned, "days_late": days_late,
    })


def site_ready_check(conn, ctx, site_id=None, complete=None, **_):
    site = _pick(conn, ctx, "sites", site_id, ["status NOT IN ('Ready To Activate', 'Activated', 'On Hold')"])
    if not site:
        return None
    if complete is None:
        complete = ctx.rng.random() < ctx.problems["ready_check_complete_probability"]
    if complete:
        _make_site_complete(conn, ctx, site["site_id"])
    # The payload does not say whether the site is complete: the agent must check.
    return emit(conn, "site_ready_check", "site", site["site_id"], {
        "site_id": site["site_id"], "site_status": site["status"],
    })


def _make_site_complete(conn, ctx, site_id: str) -> None:
    """Approve every document, complete milestones up to Contract Signed, renew training."""
    today = _today()
    for doc in conn.execute("SELECT * FROM regulatory_documents WHERE site_id = ?", (site_id,)):
        expiry = doc["expiry_date"]
        if doc["doc_type"] in EXPIRING_DOC_TYPES and (not expiry or expiry < today.isoformat()):
            expiry = (today + timedelta(days=ctx.rng.randint(180, 720))).isoformat()
        conn.execute(
            "UPDATE regulatory_documents SET status = 'Approved', expiry_date = ?, "
            "submitted_at = COALESCE(submitted_at, ?), reviewer_comment = NULL WHERE doc_id = ?",
            (expiry, now(), doc["doc_id"]),
        )
    for m_type in MILESTONE_TYPES[: MILESTONE_TYPES.index("Contract Signed") + 1]:
        _complete_milestone(conn, site_id, m_type)
    conn.execute(
        "UPDATE site_staff SET gcp_training_expiry = ? WHERE site_id = ? AND gcp_training_expiry < ?",
        ((today + timedelta(days=365)).isoformat(), site_id, today.isoformat()),
    )


# --- Data quality events -----------------------------------------------------

def staff_training_expired(conn, ctx, site_id=None, days=None, **_):
    staff = _pick(conn, ctx, "site_staff", site_id, [f"gcp_training_expiry >= '{_today().isoformat()}'"])
    if not staff:
        return None
    days_ago = days if days is not None else ctx.days_in("training_expired_days_ago_range")
    expired_on = (_today() - timedelta(days=days_ago)).isoformat()
    conn.execute(
        "UPDATE site_staff SET gcp_training_expiry = ? WHERE staff_id = ?",
        (expired_on, staff["staff_id"]),
    )
    return emit(conn, "staff_training_expired", "staff", staff["staff_id"], {
        "staff_id": staff["staff_id"], "site_id": staff["site_id"], "name": staff["name"],
        "role": staff["role"], "gcp_training_expiry": expired_on,
    })


DATA_ISSUE_VARIANTS = [
    "site_country_blank",
    "milestone_date_malformed",
    "milestone_date_garbage",
    "staff_name_blank",
]


def record_data_issue(conn, ctx, site_id=None, variant=None, **_):
    if variant is None:
        weights = ctx.problems["data_issue_variants"]
        variant = ctx.rng.choices(list(weights), list(weights.values()))[0]

    if variant == "site_country_blank":
        row = _pick(conn, ctx, "sites", site_id, ["country IS NOT NULL AND country != ''"])
        if not row:
            return None
        conn.execute("UPDATE sites SET country = '' WHERE site_id = ?", (row["site_id"],))
        entity_type, entity_id, field, bad, issue = "site", row["site_id"], "country", "", "blank value"
        site = row["site_id"]

    elif variant in ("milestone_date_malformed", "milestone_date_garbage"):
        row = _pick(conn, ctx, "site_milestones", site_id, ["status != 'Completed' AND planned_date LIKE '____-__-__'"])
        if not row:
            return None
        if variant == "milestone_date_malformed":
            d = date.fromisoformat(row["planned_date"]) if "-" in (row["planned_date"] or "") else _today()
            bad = d.strftime(ctx.rng.choice(["%Y/%m/%d", "%d %b %Y", "%Y.%m.%d"]))
            issue = "malformed date format"
        else:
            bad = ctx.rng.choice(["TBD", "??", "pending"])
            issue = "not a date"
        conn.execute("UPDATE site_milestones SET planned_date = ? WHERE milestone_id = ?", (bad, row["milestone_id"]))
        entity_type, entity_id, field, site = "milestone", row["milestone_id"], "planned_date", row["site_id"]

    elif variant == "staff_name_blank":
        row = _pick(conn, ctx, "site_staff", site_id, ["role != 'Principal Investigator' AND name != ''"])
        if not row:
            return None
        conn.execute("UPDATE site_staff SET name = '' WHERE staff_id = ?", (row["staff_id"],))
        entity_type, entity_id, field, bad, issue = "staff", row["staff_id"], "name", "", "blank value"
        site = row["site_id"]

    else:
        raise ValueError(f"Unknown data issue variant: {variant}. Choose from {DATA_ISSUE_VARIANTS}")

    return emit(conn, "record_data_issue", entity_type, entity_id, {
        "entity_type": entity_type, "entity_id": entity_id, "site_id": site,
        "field": field, "bad_value": bad, "issue": issue,
    })


ACTIONS = {
    "document_submitted": document_submitted,
    "document_rejected": document_rejected,
    "document_expiring": document_expiring,
    "document_expired": document_expired,
    "approval_received": approval_received,
    "milestone_delayed": milestone_delayed,
    "staff_training_expired": staff_training_expired,
    "contract_signed": contract_signed,
    "record_data_issue": record_data_issue,
    "site_ready_check": site_ready_check,
}
