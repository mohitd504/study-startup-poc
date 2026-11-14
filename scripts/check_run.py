"""Check a run against the acceptance criteria (section 12 of the brief).

    python scripts/check_run.py

Reports: every event processed, none processed twice, every event has a
logged decision, processing lag (target: within 10 seconds), and which
outcomes appeared. Exits with status 1 if a hard requirement fails.
"""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.db import connect  # noqa: E402

LAG_TARGET_SECONDS = 10


def main() -> int:
    with connect() as conn:
        def one(sql: str):
            return conn.execute(sql).fetchone()[0]

        total = one("SELECT COUNT(*) FROM events")
        unprocessed = one("SELECT COUNT(*) FROM events WHERE processed = 0")
        twice = one("""SELECT COUNT(*) FROM (SELECT event_id FROM agent_actions
                       WHERE action_type = 'tool:mark_event_processed'
                       GROUP BY event_id HAVING COUNT(*) > 1)""")
        no_decision = one("""SELECT COUNT(*) FROM events e WHERE processed = 1 AND NOT EXISTS (
                             SELECT 1 FROM agent_actions a WHERE a.event_id = e.event_id
                             AND a.action_type NOT LIKE 'tool:%')""")
        outcomes = dict(conn.execute("""SELECT action_type, COUNT(*) FROM agent_actions
                                        WHERE action_type NOT LIKE 'tool:%' GROUP BY action_type"""))
        by_agent = dict(conn.execute("""SELECT agent_name, COUNT(*) FROM agent_actions
                                        WHERE action_type NOT LIKE 'tool:%' GROUP BY agent_name"""))
        notifications = dict(conn.execute("SELECT status, COUNT(*) FROM notifications GROUP BY status"))
        lags = [
            (datetime.fromisoformat(p) - datetime.fromisoformat(c)).total_seconds()
            for c, p in conn.execute("SELECT created_at, processed_at FROM events WHERE processed = 1")
        ]
        span = conn.execute("SELECT MIN(created_at), MAX(created_at) FROM events").fetchone()

    slow = sum(lag > LAG_TARGET_SECONDS for lag in lags)
    minutes = ((datetime.fromisoformat(span[1]) - datetime.fromisoformat(span[0])).total_seconds() / 60
               if span[0] else 0)

    def check(ok: bool, text: str) -> bool:
        print(f"  [{'PASS' if ok else 'FAIL'}] {text}")
        return ok

    print(f"Events: {total} over {minutes:.1f} minutes\n")
    results = [
        check(total > 0, f"events were generated ({total})"),
        check(unprocessed == 0, f"every event processed (unprocessed: {unprocessed})"),
        check(twice == 0, f"no event processed twice (duplicates: {twice})"),
        check(no_decision == 0, f"every processed event has a logged decision (missing: {no_decision})"),
    ]
    if lags:
        check(slow == 0, f"processed within {LAG_TARGET_SECONDS}s of creation: max {max(lags):.0f}s, "
                         f"average {sum(lags) / len(lags):.1f}s, slower than target: {slow}")
    for kind in ("auto_resolve", "notify", "flag"):
        check(outcomes.get(kind, 0) > 0, f"at least one {kind} ({outcomes.get(kind, 0)})")

    print(f"\nDecisions by outcome: {outcomes}")
    print(f"Decisions by agent:   {by_agent}")
    print(f"Notifications:        {notifications}")
    if outcomes.get("error"):
        print(f"Note: {outcomes['error']} event(s) ended in a logged error; see agent_actions.")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
