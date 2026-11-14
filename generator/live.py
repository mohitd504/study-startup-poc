"""Continuous event generation and on-demand injection."""

import json
import random
import time

from faker import Faker

from common.config import load_config
from common.db import connect
from generator.events import ACTIONS, Context
from generator.seed import has_data


def make_context() -> Context:
    # Unseeded, so restarting the generator does not replay the same events.
    return Context(rng=random.Random(), fake=Faker(), cfg=load_config("generator"))


def describe(event: dict) -> str:
    payload = json.dumps(event["payload"])
    if len(payload) > 110:
        payload = payload[:107] + "..."
    entity = f"{event['entity_type']}:{event['entity_id']}"
    return (
        f"[{event['created_at']}] #{event['event_id']:<5} {event['event_type']:<23} "
        f"{entity:<19} {payload}"
    )


def generate_one(ctx: Context, event_type: str | None = None, **opts) -> dict | None:
    """Create one event. Without event_type, pick one by weight; if that type has
    nothing to act on, try a few others before giving up for this tick."""
    with connect() as conn:
        if event_type:
            return ACTIONS[event_type](conn, ctx, **opts)
        weights = ctx.cfg["event_weights"]
        for _ in range(5):
            chosen = ctx.rng.choices(list(weights), list(weights.values()))[0]
            event = ACTIONS[chosen](conn, ctx)
            if event:
                return event
    return None


def _require_seeded() -> None:
    if not has_data():
        raise SystemExit("No data yet. Run `python -m generator seed --reset` first.")


def run(count: int | None = None, interval: float | None = None, minutes: float | None = None) -> None:
    """Generate events until stopped (Ctrl+C), after `count` events, or after `minutes`."""
    _require_seeded()
    ctx = make_context()
    low, high = ctx.cfg["interval_seconds"]["min"], ctx.cfg["interval_seconds"]["max"]
    deadline = time.monotonic() + minutes * 60 if minutes else None
    made = 0
    print(f"Generating events every {interval or f'{low}-{high}'}s. Press Ctrl+C to stop.")

    def keep_going() -> bool:
        return (count is None or made < count) and (deadline is None or time.monotonic() < deadline)

    try:
        while keep_going():
            event = generate_one(ctx)
            if event:
                made += 1
                print(describe(event), flush=True)
            if keep_going():
                time.sleep(interval if interval is not None else ctx.rng.uniform(low, high))
    except KeyboardInterrupt:
        pass
    print(f"Stopped after {made} events.")


def inject(event_type: str, site_id: str | None = None, **opts) -> dict:
    """Create one specific event now, optionally for a chosen site."""
    _require_seeded()
    if site_id:
        with connect() as conn:
            if not conn.execute("SELECT 1 FROM sites WHERE site_id = ?", (site_id,)).fetchone():
                raise SystemExit(f"Unknown site: {site_id}")
    opts = {k: v for k, v in opts.items() if v is not None}
    event = generate_one(make_context(), event_type, site_id=site_id, **opts)
    if not event:
        raise SystemExit(f"Nothing suitable to create a {event_type} event for. Try another --site.")
    print("Injected:", describe(event))
    return event
