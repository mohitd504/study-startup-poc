# Demo guide

A 10-minute live run following section 14 of the brief, then the design
explanation. Commands assume the venv is active in each terminal.

## Before the demo (10 minutes beforehand)

1. `python -m generator seed --reset` for clean data.
2. Open four terminals (server, generator, agents, spare for inject) and a browser.
3. Start the MCP server: `python -m mcp_server.server`.
4. Start the viewer: `streamlit run viewer/app.py` and keep its tab open.
5. Start the Inspector once (`npx @modelcontextprotocol/inspector`) so the
   package is already downloaded, and connect it to `http://127.0.0.1:8000/mcp`.
6. If using OpenAI mode, check `.env` and run one event:
   `python -m generator inject document_submitted --site SITE-001` then
   `python -m agents --mode openai --once`.
7. Have the backup ready (see the end of this page).

## Run sheet

| Time | Show | Do / say |
|---|---|---|
| 0:00 | Generator | `python -m generator run`. "A Faker-based generator writes realistic start-up events every 3 to 5 seconds: document rejections, expiring certificates, late milestones, bad data." Show `python -m generator recent` in the spare terminal: events are arriving, all marked `new`. |
| 1:30 | MCP Inspector | "Agents never touch the database. Everything goes through this MCP server, and it works without any agent." Call `get_unprocessed_events`, then `get_site` with `SITE-003` (point at the readiness section), then `create_notification` with `severity: urgent` to show the validation error. Open **Resources → study://rules**: "the decision rules are served from config, not hardcoded in prompts." |
| 4:00 | Agents | `python -m agents` (or `--mode openai`). Events are picked up within a couple of seconds; each line shows the route, the outcome and the time taken. |
| 5:00 | Inject | `python -m generator inject document_expired --site SITE-003`. Point at the agent line: `document_agent → flag`. In the viewer, open **Agent decisions** and read the reasoning aloud; open **Notifications** for the flagged high-severity row; open **Tool audit** to show the status change to Expired recorded by the server. |
| 6:30 | Auto-resolve | `python -m generator inject site_ready_check --site SITE-006 --complete`. The site moves to Ready To Activate. Then try to cheat: in the Inspector, call `update_site_status` for a site that is not ready (e.g. `SITE-001`, `Ready To Activate`). The server refuses and lists what is missing. "The rule is enforced at the data boundary, whatever an agent decides." |
| 7:30 | Outcomes | Viewer metrics: auto-resolved, notified, flagged, logged. Show one of each in **Agent decisions**. |
| 8:30 | Proof | Stop the generator. `python scripts/check_run.py`: every event processed, none twice, all within 10 seconds, all three outcomes. |
| 9:00 | Design | Move to the explanation below. |

## The 10-minute explanation

**Why MCP sits between the agents and the data**
- One controlled interface: agents can only do what a tool allows. There is no
  delete tool, and `fix_record_field` only touches an allow-listed set of fields.
- Validation and business invariants live in one place, whoever the caller is:
  an LLM agent, the rules engine, the Inspector or a future integration. Example:
  Ready To Activate is refused unless the readiness check passes.
- Audit for free: every write records itself in `agent_actions`, in the same
  transaction as the change, so there is no write without a trail.
- Swappable on both sides: the data store could move to Postgres or a real CTMS
  behind the same tools, and any MCP client (another agent framework, the
  Inspector) can use them unchanged.

**How the graph routes events** ([agents/graph.py](../agents/graph.py))
- State: the event, the chosen route, the specialist's result.
- Nodes: `route` (supervisor) → one specialist → `finalize` (supervisor).
- A conditional edge picks the specialist from the routing table in `study://rules`.
- `finalize` checks that a decision was logged (an LLM that forgets is recorded
  as an error) and calls `mark_event_processed`, which only succeeds once.
- The supervisor loop polls every 2 seconds and runs up to 4 events at once.
  A failure or timeout is logged and skipped; it never crashes the loop.

**Rules mode and LLM mode**
- Same graph, same tools, same rules. The LLM's instructions are generated from
  `rules.yaml`; the rules engine applies it directly.
- The rules engine is free, instant and deterministic, which makes it the
  baseline to measure the LLM against, and a fallback if the LLM is unavailable.
- The LLM adds flexible reasoning and readable explanations, at about $0.004 per
  event on a small model, with a few seconds of latency.

**Trade-offs made for the POC**
- SQLite with WAL mode instead of Postgres: zero setup, good enough for one machine.
- Polling the events table instead of a message queue: simple, and the
  `processed` flag plus a guarded update prevents double processing.
- One MCP session shared by all agents: per-call sessions made each event take
  10 to 17 seconds; a shared session takes under one.
- MCP SDK pinned to 1.x because langchain-mcp-adapters does not support 2.x yet.

## What I would change before production

1. **Data and queue:** Postgres instead of SQLite; a real queue (or leased
   claims on events) so several supervisor workers can share the load, with
   retries and a dead-letter queue instead of "log and skip".
2. **Security:** authentication on the MCP server (OAuth), a scoped identity
   per agent so each can only call its own tools, TLS, and secrets in a vault.
3. **Human in the loop:** auto-resolve actions (status changes, data fixes)
   start in shadow mode, where agents recommend and a person approves, then are
   switched on rule by rule with feature flags.
4. **Prompt-injection defence:** event payloads and free text such as reviewer
   comments are untrusted input to the LLM. Keep tools narrow (already done) and
   keep invariants in the server (already done), and add input screening.
5. **Compliance:** GxP validation, 21 CFR Part 11 audit trail requirements
   (who, what, when, why; tamper-evident), and data protection for investigator
   personal data.
6. **Integrations:** a worker that reads `notifications` with status `pending`
   and sends email or Teams messages, then marks them sent; real source systems
   (CTMS, eTMF) behind the same MCP tools.
7. **Operations:** metrics for event lag, backlog, error rate and token cost,
   alerts on a growing backlog, structured logs, and versioned rules with a
   change review.
8. **Evaluation:** compare LLM decisions with the rules engine on the same
   events, and gate model or prompt changes on that agreement rate.

## Likely questions

- *What happens if the agent crashes halfway?* The event is not marked
  processed, so it is picked up again; writes already made are in the audit log.
  For production, idempotent tools or leased claims would make the retry clean.
- *Could an agent mark a site ready when it is not?* No. The server checks
  readiness itself and refuses.
- *Why not let the LLM query the database directly?* No validation, no audit,
  no invariants, and a much larger blast radius for a wrong or injected instruction.
- *How do you change a threshold?* Edit `config/rules.yaml`. The server serves
  the new rules on the next read; the rules engine uses them on the next event,
  and restarting the agents rebuilds the LLM prompts.

## Backup plan

- Record a short screen capture of a full run (generator, agents, inject,
  viewer, `check_run.py`) and keep it on the desktop.
- Take screenshots of: the Inspector calling a tool, the agents terminal, the
  viewer's Agent decisions and Notifications tabs, and the `check_run.py` output.
- Keep a copy of `data/study.db` from a good run: copy it back (with the server
  stopped) to show real results even if live generation fails.
- If OpenAI is slow or unavailable, switch to `python -m agents` (rules mode):
  same graph, same tools, same outcomes.
