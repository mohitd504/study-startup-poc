"""Seed the database with studies, sites, documents, staff and milestones.

The seeded data is internally consistent: a site's status decides how many
milestones are complete, and documents line up with those milestones (for
example, an approved ethics document implies a completed Ethics Approval).
Pending milestones are always in the future, so problems only appear when
the generator creates them as events.
"""

import random
from datetime import date, datetime, time, timedelta

from faker import Faker

from common.config import load_config
from common.db import DB_PATH, connect, init_schema, now
from common.domain import (
    DOC_TYPES,
    EXPIRING_DOC_TYPES,
    MILESTONE_TYPES,
    STAFF_ROLES,
    THERAPEUTIC_AREAS,
)

# How many milestones (in MILESTONE_TYPES order) are complete for each starting status.
COMPLETED_MILESTONES = {
    "Identified": 0,
    "Selected": 2,          # Feasibility Complete, CDA Signed
    "Docs In Progress": 3,  # + Ethics Submission
    "Contract In Review": 4,  # + Ethics Approval
}


def reset_database() -> None:
    """Delete the database file (and its WAL files) and recreate the schema."""
    for suffix in ("", "-wal", "-shm"):
        path = DB_PATH.with_name(DB_PATH.name + suffix)
        try:
            path.unlink(missing_ok=True)
        except PermissionError:
            raise SystemExit(
                f"Cannot delete {path.name}: it is in use. Stop the MCP server, "
                "agents and viewer, then try again."
            )
    init_schema()


def _iso(d: date) -> str:
    return d.isoformat()


def _iso_dt(d: date, rng: random.Random) -> str:
    t = time(rng.randint(8, 17), rng.randint(0, 59))
    return datetime.combine(d, t).isoformat(timespec="seconds")


def _doc_status(doc_type: str, site_status: str, done: int, rng: random.Random) -> str:
    if doc_type == "Ethics/IRB approval":
        return "Approved" if done >= 4 else "Submitted" if done >= 3 else "Not Started"
    if doc_type == "Clinical trial agreement":
        return "Submitted" if site_status == "Contract In Review" else "Not Started"
    if doc_type == "Site budget":
        if site_status == "Contract In Review":
            return rng.choices(["Approved", "Submitted"], [7, 3])[0]
        if site_status == "Docs In Progress":
            return rng.choice(["Submitted", "Not Started"])
        return "Not Started"
    # PI CV, GCP certificate, Financial disclosure, Lab certification
    if done >= 3:
        return rng.choices(["Approved", "Submitted", "Rejected"], [60, 25, 15])[0]
    if done >= 2:
        return rng.choice(["Submitted", "Not Started"])
    return "Not Started"


def seed(rng: random.Random, fake: Faker) -> dict:
    """Insert the initial data set. Returns row counts per table."""
    cfg = load_config("generator")
    ref = load_config("reference")
    seed_cfg = cfg["seed"]
    today = date.today()

    n_studies = seed_cfg["studies"]
    n_sites = seed_cfg["sites_total"]
    statuses = list(seed_cfg["site_status_weights"])
    status_weights = list(seed_cfg["site_status_weights"].values())
    staff_min, staff_max = seed_cfg["staff_per_site"]
    cities = list(ref["city_country"])
    city_picks = (
        rng.sample(cities, n_sites) if n_sites <= len(cities) else rng.choices(cities, k=n_sites)
    )

    with connect() as conn:
        study_ids = []
        for i in range(1, n_studies + 1):
            study_id = f"STU-{i:03d}"
            area = rng.choice(THERAPEUTIC_AREAS)
            compound = f"{rng.choice(['ABX', 'NVT', 'KRX', 'ZLM', 'QTR'])}-{rng.randint(100, 999)}"
            conn.execute(
                "INSERT INTO studies VALUES (?, ?, ?, ?, ?)",
                (
                    study_id,
                    f"Phase {rng.choice(['II', 'III'])} trial of {compound} in {area}",
                    area,
                    len(range(i - 1, n_sites, n_studies)),  # sites assigned round-robin below
                    _iso(today + timedelta(days=rng.randint(60, 150))),
                ),
            )
            study_ids.append(study_id)

        for n in range(n_sites):
            site_id = f"SITE-{n + 1:03d}"
            city = city_picks[n]
            status = rng.choices(statuses, status_weights)[0]
            done = COMPLETED_MILESTONES[status]
            created = today - timedelta(days=rng.randint(30, 120))
            pi_name = fake.name()

            conn.execute(
                "INSERT INTO sites VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    site_id,
                    study_ids[n % n_studies],
                    f"{city} {rng.choice(ref['site_name_suffixes'])}",
                    ref["city_country"][city],
                    pi_name,
                    status,
                    _iso_dt(created, rng),
                ),
            )

            # Milestones: completed ones have an actual date in the past,
            # pending ones are planned in the future.
            cursor = created + timedelta(days=7)
            for idx, m_type in enumerate(MILESTONE_TYPES):
                if idx < done:
                    actual = min(cursor + timedelta(days=rng.randint(-3, 5)), today)
                    row = (_iso(cursor), _iso(actual), "Completed")
                else:
                    cursor = max(cursor, today + timedelta(days=5))
                    row = (_iso(cursor), None, "Pending")
                conn.execute(
                    "INSERT INTO site_milestones (site_id, type, planned_date, actual_date, status) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (site_id, m_type, *row),
                )
                cursor += timedelta(days=rng.randint(10, 25))

            # Regulatory documents: one of each type per site.
            for doc_type in DOC_TYPES:
                doc_status = _doc_status(doc_type, status, done, rng)
                submitted_at = expiry = comment = None
                rejections = 0
                if doc_status != "Not Started":
                    submitted_at = _iso_dt(
                        min(created + timedelta(days=rng.randint(5, 40)), today), rng
                    )
                if doc_status == "Approved" and doc_type in EXPIRING_DOC_TYPES:
                    expiry = _iso(today + timedelta(days=rng.randint(90, 720)))
                if doc_status == "Rejected":
                    rejections = rng.choice([1, 1, 2])
                    comment = rng.choice(cfg["rejection_reasons"])
                conn.execute(
                    "INSERT INTO regulatory_documents (site_id, doc_type, status, submitted_at, "
                    "expiry_date, reviewer_comment, rejection_count) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (site_id, doc_type, doc_status, submitted_at, expiry, comment, rejections),
                )

            # Staff: the PI (same name as on the site record) plus others.
            others = rng.sample(STAFF_ROLES[1:], rng.randint(staff_min, staff_max) - 1)
            for role in ["Principal Investigator", *others]:
                conn.execute(
                    "INSERT INTO site_staff (site_id, name, role, gcp_training_expiry, cv_on_file) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        site_id,
                        pi_name if role == "Principal Investigator" else fake.name(),
                        role,
                        _iso(today + timedelta(days=rng.randint(60, 900))),
                        1 if rng.random() < 0.9 else 0,
                    ),
                )

        counts = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("studies", "sites", "site_milestones", "regulatory_documents", "site_staff")
        }
    return counts


def make_random(seed_value: int | None) -> tuple[random.Random, Faker]:
    rng = random.Random(seed_value)
    fake = Faker()
    if seed_value is not None:
        Faker.seed(seed_value)
    return rng, fake


def has_data() -> bool:
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM studies").fetchone()[0] > 0


def run_seed(reset: bool) -> None:
    if reset:
        reset_database()
    else:
        init_schema()
        if has_data():
            raise SystemExit("Database already has data. Use `seed --reset` to start fresh.")
    rng, fake = make_random(load_config("generator").get("random_seed"))
    counts = seed(rng, fake)
    print(f"Seeded {DB_PATH} at {now()}")
    for table, count in counts.items():
        print(f"  {table:<22}{count}")
