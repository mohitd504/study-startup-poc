"""Command line for the data generator.

    python -m generator seed [--reset]   create the initial data set
    python -m generator status           show row counts and site statuses
"""

import argparse

from common.db import connect, init_schema
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


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m generator")
    sub = parser.add_subparsers(dest="command", required=True)

    p_seed = sub.add_parser("seed", help="create the initial data set")
    p_seed.add_argument("--reset", action="store_true", help="delete existing data first")

    sub.add_parser("status", help="show row counts and site statuses")

    args = parser.parse_args()
    if args.command == "seed":
        run_seed(reset=args.reset)
    elif args.command == "status":
        show_status()


if __name__ == "__main__":
    main()
