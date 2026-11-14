"""Read-only viewer for the output tables (stretch goal).

    streamlit run viewer/app.py

Opens the database read-only and refreshes every few seconds.
"""

import sqlite3
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.db import DB_PATH  # noqa: E402

REFRESH_SECONDS = 3
OUTCOME_LABELS = {"auto_resolve": "Auto-resolved", "notify": "Notified", "flag": "Flagged",
                  "log_only": "Logged only", "error": "Errors"}

st.set_page_config(page_title="Study Start-up Agents", layout="wide")


def query(sql: str, params: tuple = ()) -> pd.DataFrame:
    conn = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True)
    try:
        return pd.read_sql_query(sql, conn, params=params)
    finally:
        conn.close()


def show(df: pd.DataFrame, empty: str) -> None:
    if df.empty:
        st.info(empty)
    else:
        st.dataframe(df, hide_index=True, width="stretch")


st.title("Study start-up: agent activity")
st.caption(f"Database: {DB_PATH} (read-only). Refreshes every {REFRESH_SECONDS}s.")

if not DB_PATH.exists():
    st.error("No database yet. Run `python -m generator seed --reset` first.")
    st.stop()


@st.fragment(run_every=f"{REFRESH_SECONDS}s")
def dashboard() -> None:
    totals = query("SELECT COUNT(*) AS total, COALESCE(SUM(processed = 0), 0) AS waiting FROM events").iloc[0]
    outcomes = dict(query("""SELECT action_type, COUNT(*) AS n FROM agent_actions
                             WHERE action_type NOT LIKE 'tool:%' GROUP BY action_type""").values)

    cols = st.columns(6)
    cols[0].metric("Events", int(totals["total"]), help="Total events generated")
    cols[1].metric("Waiting", int(totals["waiting"]), help="Events not processed yet")
    for col, kind in zip(cols[2:], ["auto_resolve", "notify", "flag", "log_only"]):
        col.metric(OUTCOME_LABELS[kind], outcomes.get(kind, 0))
    if outcomes.get("error"):
        st.warning(f"{outcomes['error']} event(s) ended in an error. See Agent decisions.")

    tabs = st.tabs(["Notifications", "Agent decisions", "Events", "Sites", "Tool audit"])

    with tabs[0]:
        status = st.segmented_control("Status", ["all", "flagged", "pending"], default="all", key="n_status")
        where = "" if status in (None, "all") else "WHERE n.status = ?"
        show(query(f"""SELECT n.notification_id AS id, n.created_at, n.status, n.severity, n.recipient_role,
                              n.message, n.event_id, e.event_type
                       FROM notifications n JOIN events e USING (event_id) {where}
                       ORDER BY n.notification_id DESC LIMIT 300""",
                   () if not where else (status,)),
             "No notifications yet.")

    with tabs[1]:
        kinds = ["all", *OUTCOME_LABELS]
        kind = st.segmented_control("Outcome", kinds, default="all", key="d_kind",
                                    format_func=lambda k: OUTCOME_LABELS.get(k, "All"))
        where = "AND a.action_type = ?" if kind not in (None, "all") else ""
        show(query(f"""SELECT a.created_at, a.event_id, e.event_type, a.agent_name, a.action_type AS outcome,
                              a.reasoning, a.outcome AS result
                       FROM agent_actions a JOIN events e USING (event_id)
                       WHERE a.action_type NOT LIKE 'tool:%' {where}
                       ORDER BY a.action_id DESC LIMIT 300""",
                   () if not where else (kind,)),
             "No decisions yet. Are the agents running?")

    with tabs[2]:
        show(query("""SELECT event_id, created_at, event_type, entity_type, entity_id,
                             CASE processed WHEN 1 THEN 'done' ELSE 'waiting' END AS state,
                             processed_at, payload
                      FROM events ORDER BY event_id DESC LIMIT 300"""),
             "No events yet. Is the generator running?")

    with tabs[3]:
        show(query("""SELECT s.site_id, s.name, s.country, s.pi_name, s.status, st.title AS study
                      FROM sites s JOIN studies st USING (study_id) ORDER BY s.site_id"""),
             "No sites.")

    with tabs[4]:
        st.caption("Every write made through the MCP server, recorded automatically by the server.")
        show(query("""SELECT created_at, event_id, agent_name, action_type AS tool, outcome, reasoning
                      FROM agent_actions WHERE action_type LIKE 'tool:%'
                      ORDER BY action_id DESC LIMIT 300"""),
             "No tool calls yet.")


dashboard()
