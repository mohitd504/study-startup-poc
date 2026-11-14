"""Command line for the data generator.

    python -m generator seed [--reset]           create the initial data set
    python -m generator run [--minutes M]        generate events until Ctrl+C (or M minutes)
    python -m generator inject EVENT [options]   create one specific event now
    python -m generator status                   row counts and site statuses
    python -m generator recent [-n 10]           latest events

Inject examples:
    python -m generator inject document_expired --site SITE-003
    python -m generator inject document_expiring --site SITE-001 --days 12
    python -m generator inject document_rejected --site SITE-002 --rejections 3
    python -m generator inject milestone_delayed --site SITE-004 --days 9
    python -m generator inject record_data_issue --site SITE-005 --variant site_country_blank
    python -m generator inject site_ready_check --site SITE-006 --complete
"""

import argparse
import json

from common.db import connect, init_schema
from generator.events import ACTIONS, DATA_ISSUE_VARIANTS
from generator.live import inject, run
from generator.seed import run_seed

TABLES = [
    "studies",
    "sites",
    "site_milestones",
    "regulatory_documents",
    "site_staff",
    "events",
    "agent_actions",
    "notifications",
]


def show_status() -> None:
    init_schema()
    with connect() as conn:
        for table in TABLES:
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"{table:<22}{count}")
        unprocessed = conn.execute("SELECT COUNT(*) FROM events WHERE processed = 0").fetchone()[0]
        print(f"{'  unprocessed events':<22}{unprocessed}")
        print("\nSites by status:")
        for row in conn.execute(
            "SELECT status, COUNT(*) AS n FROM sites GROUP BY status ORDER BY n DESC"
        ):
            print(f"  {row['status']:<20}{row['n']}")
        print("\nEvents by type:")
        for row in conn.execute(
            "SELECT event_type, COUNT(*) AS n FROM events GROUP BY event_type ORDER BY n DESC"
        ):
            print(f"  {row['event_type']:<24}{row['n']}")


def show_recent(limit: int) -> None:
    init_schema()
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM events ORDER BY event_id DESC LIMIT ?", (limit,)
        ).fetchall()
    for r in reversed(rows):
        mark = "done" if r["processed"] else "new "
        entity = f"{r['entity_type']}:{r['entity_id']}"
        print(f"#{r['event_id']:<5} {mark} {r['created_at']}  {r['event_type']:<23} "
              f"{entity:<19} {json.dumps(json.loads(r['payload']))}")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m generator",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_seed = sub.add_parser("seed", help="create the initial data set")
    p_seed.add_argument("--reset", action="store_true", help="delete existing data first")

    p_run = sub.add_parser("run", help="generate events until Ctrl+C")
    p_run.add_argument("--count", type=int, help="stop after N events")
    p_run.add_argument("--interval", type=float, help="fixed seconds between events")
    p_run.add_argument("--minutes", type=float, help="stop after this many minutes")

    p_inject = sub.add_parser("inject", help="create one specific event now")
    p_inject.add_argument("event_type", choices=sorted(ACTIONS))
    p_inject.add_argument("--site", help="site id, e.g. SITE-003")
    p_inject.add_argument("--days", type=int, help="days left / overdue / late")
    p_inject.add_argument("--rejections", type=int, help="rejection count to set (document_rejected)")
    p_inject.add_argument("--variant", choices=DATA_ISSUE_VARIANTS, help="kind of record_data_issue")
    complete = p_inject.add_mutually_exclusive_group()
    complete.add_argument("--complete", dest="complete", action="store_true", default=None,
                          help="site_ready_check: make the site fully ready first")
    complete.add_argument("--incomplete", dest="complete", action="store_false",
                          help="site_ready_check: leave the site as it is")

    sub.add_parser("status", help="row counts and site statuses")

    p_recent = sub.add_parser("recent", help="latest events")
    p_recent.add_argument("-n", type=int, default=10)

    args = parser.parse_args()
    if args.command == "seed":
        run_seed(reset=args.reset)
    elif args.command == "run":
        run(count=args.count, interval=args.interval, minutes=args.minutes)
    elif args.command == "inject":
        inject(args.event_type, site_id=args.site, days=args.days, rejections=args.rejections,
               variant=args.variant, complete=args.complete)
    elif args.command == "status":
        show_status()
    elif args.command == "recent":
        show_recent(args.n)


if __name__ == "__main__":
    main()
