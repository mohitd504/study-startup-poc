"""SQLite connection and schema for the study start-up POC.

Run `python -m common.db` to create data/study.db with all tables.
"""

import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "study.db"


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # rows behave like dicts
    conn.execute("PRAGMA journal_mode=WAL")  # generator and MCP server can share the DB
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS studies (
    study_id                TEXT PRIMARY KEY,
    title                   TEXT NOT NULL,
    therapeutic_area        TEXT,
    target_sites            INTEGER,
    planned_activation_date TEXT
);

CREATE TABLE IF NOT EXISTS sites (
    site_id    TEXT PRIMARY KEY,
    study_id   TEXT NOT NULL REFERENCES studies(study_id),
    name       TEXT,
    country    TEXT,
    pi_name    TEXT,
    status     TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS site_milestones (
    milestone_id INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id      TEXT NOT NULL REFERENCES sites(site_id),
    type         TEXT NOT NULL,
    planned_date TEXT,
    actual_date  TEXT,
    status       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS regulatory_documents (
    doc_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id          TEXT NOT NULL REFERENCES sites(site_id),
    doc_type         TEXT NOT NULL,
    status           TEXT NOT NULL,
    submitted_at     TEXT,
    expiry_date      TEXT,
    reviewer_comment TEXT,
    rejection_count  INTEGER NOT NULL DEFAULT 0  -- added: drives the "3rd rejection" rule
);

CREATE TABLE IF NOT EXISTS site_staff (
    staff_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id             TEXT NOT NULL REFERENCES sites(site_id),
    name                TEXT,
    role                TEXT,
    gcp_training_expiry TEXT,
    cv_on_file          INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS events (
    event_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type   TEXT NOT NULL,
    entity_type  TEXT NOT NULL,
    entity_id    TEXT NOT NULL,
    payload      TEXT NOT NULL DEFAULT '{}',  -- JSON
    created_at   TEXT NOT NULL,
    processed    INTEGER NOT NULL DEFAULT 0,
    processed_at TEXT                         -- added: measures the 10-second target
);

CREATE TABLE IF NOT EXISTS agent_actions (
    action_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    INTEGER REFERENCES events(event_id),
    agent_name  TEXT NOT NULL,
    action_type TEXT NOT NULL,
    reasoning   TEXT,
    outcome     TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notifications (
    notification_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id        INTEGER REFERENCES events(event_id),
    severity        TEXT NOT NULL,
    recipient_role  TEXT NOT NULL,
    message         TEXT NOT NULL,
    status          TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_unprocessed ON events(processed, event_id);
"""


def init_schema() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)


if __name__ == "__main__":
    init_schema()
    print(f"Database ready at {DB_PATH}")
