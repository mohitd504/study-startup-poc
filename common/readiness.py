"""Site readiness check, driven by the `readiness` section of rules.yaml.

Used by the MCP server (to report readiness and to guard the move to
Ready To Activate) so the rule lives in one place.
"""

from datetime import date


def missing_for_activation(
    docs: list[dict], milestones: list[dict], staff: list[dict], readiness: dict, today: date | None = None
) -> list[str]:
    """Return what is still missing for a site to be Ready To Activate (empty list = ready)."""
    today_iso = (today or date.today()).isoformat()
    missing = []

    by_type = {d["doc_type"]: d for d in docs}
    for doc_type in readiness["required_documents"]:
        doc = by_type.get(doc_type)
        if not doc:
            missing.append(f"{doc_type}: no document on file")
        elif doc["status"] != "Approved":
            missing.append(f"{doc_type}: {doc['status']}")
        elif readiness.get("no_expired_documents") and doc["expiry_date"] and doc["expiry_date"] < today_iso:
            missing.append(f"{doc_type}: expired on {doc['expiry_date']}")

    ms_status = {m["type"]: m["status"] for m in milestones}
    for m_type in readiness["required_milestones_completed"]:
        if ms_status.get(m_type) != "Completed":
            missing.append(f"Milestone {m_type}: {ms_status.get(m_type, 'missing')}")

    if readiness.get("no_expired_staff_training"):
        for s in staff:
            if s["gcp_training_expiry"] and s["gcp_training_expiry"] < today_iso:
                who = s["name"] or f"staff #{s['staff_id']}"
                missing.append(f"GCP training expired for {who} ({s['role']}) on {s['gcp_training_expiry']}")

    return missing
