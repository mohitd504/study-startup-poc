# 15-minute soak run

Run on 2026-11-14 in rules mode, generator at its default 3 to 5 second interval
(`python -m generator run --minutes 15`), all three processes running.
Checked with `python scripts/check_run.py`:

```
Events: 224 over 15.0 minutes

  [PASS] events were generated (224)
  [PASS] every event processed (unprocessed: 0)
  [PASS] no event processed twice (duplicates: 0)
  [PASS] every processed event has a logged decision (missing: 0)
  [PASS] processed within 10s of creation: max 3s, average 1.2s, slower than target: 0
  [PASS] at least one auto_resolve (21)
  [PASS] at least one notify (68)
  [PASS] at least one flag (40)

Decisions by outcome: {'auto_resolve': 21, 'flag': 40, 'log_only': 95, 'notify': 68}
Decisions by agent:   {'data_quality_agent': 33, 'document_agent': 114, 'milestone_site_agent': 77}
Notifications:        {'flagged': 40, 'pending': 68}
```

The agents' log showed no errors. A copy of this run's database is kept locally
as `data/soak-run-2026-11-14.db` (not in git) as a demo backup.
