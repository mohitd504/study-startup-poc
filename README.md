# Agentic orchestration for study start-up (POC)

AI agents watch a live stream of clinical-trial study start-up data (site
selection through site activation) and act on each event through an MCP
server: they **auto-resolve**, **notify** or **flag** issues, and log their
reasoning. All data is synthetic, generated continuously with Faker.

```
 ┌────────────────┐  writes data   ┌──────────────────────────────┐
 │ Data generator │ ─────────────▶ │ SQLite: study data + events  │
 │  (Faker)       │  + events      └──────────────┬───────────────┘
 └────────────────┘                               │  only access path
                                   ┌──────────────┴───────────────┐
        MCP Inspector ───────────▶ │ MCP server (FastMCP)         │
        (manual testing)           │ 12 tools · 3 resources       │
                                   │ validation · audit log       │
                                   └──────────────┬───────────────┘
                                                  │  MCP over HTTP
                                   ┌──────────────┴───────────────┐
                                   │ LangGraph agents             │
                                   │ supervisor → route →         │
                                   │   document / milestone+site /│
                                   │   data quality → finalize    │
                                   └──────────────────────────────┘
 Streamlit viewer ── read-only ──▶ SQLite
```

Four independent processes: the **generator** writes events, the **MCP server**
is the only way agents touch data, the **agents** poll for events and act, and
the optional **viewer** shows the results.

## Requirements

- Python 3.11 or newer (developed on 3.13, Windows 11)
- Node.js, only for the MCP Inspector (optional)
- An OpenAI API key, only for the LLM agent mode (optional; rules mode needs no key)

## Setup

```powershell
git clone https://github.com/mohitd504/study-startup-poc.git
cd study-startup-poc
python -m venv .venv
.\.venv\Scripts\Activate.ps1          # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python -m generator seed --reset      # creates data/study.db with 2 studies and 20 sites
```

For the LLM mode, copy `.env.example` to `.env` and add your key. `.env` is
git-ignored; never commit a key.

## Run it

Open three terminals in the project folder, activate the venv in each, then:

| Terminal | Command | What you see |
|---|---|---|
| 1 | `python -m mcp_server.server` | MCP server on `http://127.0.0.1:8000/mcp` |
| 2 | `python -m generator run` | A new event every 3 to 5 seconds |
| 3 | `python -m agents` | Each event routed, decided and logged within seconds |

Optional fourth terminal: `streamlit run viewer/app.py` opens a live view of
notifications, agent decisions, events, sites and the tool audit trail.

Start the MCP server before the agents. Stop any process with Ctrl+C. The
agents reconnect by themselves if the server restarts.

### Agent modes

| Command | Decisions made by | Cost |
|---|---|---|
| `python -m agents` | Rules engine applying `config/rules.yaml` (no LLM) | Free |
| `python -m agents --mode openai` | LLM agents (LangGraph ReAct) whose instructions are built from the same rules | About $0.004 per event on `gpt-6-luna` |

Both modes use exactly the same graph, MCP tools and rules. Choose the OpenAI
model with `AGENT_MODEL` in `.env` or `--model`. Each event's token use and
estimated cost are printed. `--once` processes the current backlog and exits.

## Trigger a specific problem (for demos)

`inject` creates one event now, for a chosen site:

```powershell
python -m generator inject document_expired --site SITE-003                  # flag, high
python -m generator inject document_expiring --site SITE-001 --days 12       # notify (30 days or fewer)
python -m generator inject document_rejected --site SITE-002 --rejections 3  # flag on 3rd rejection
python -m generator inject milestone_delayed --site SITE-004 --days 9        # flag (7+ days late)
python -m generator inject record_data_issue --site SITE-005 --variant site_country_blank   # auto-fix
python -m generator inject record_data_issue --site SITE-005 --variant milestone_date_garbage  # notify
python -m generator inject staff_training_expired --site SITE-008            # flag, medium
python -m generator inject site_ready_check --site SITE-006 --complete       # auto-resolve to Ready To Activate
```

Run `python -m generator inject -h` for all options. Other generator commands:
`status` (counts), `recent -n 10` (latest events), `seed --reset` (start over;
stop the MCP server, agents and viewer first).

## Test the MCP server without agents

**MCP Inspector:** with the server running, run `npx @modelcontextprotocol/inspector`.
In the page that opens, choose transport **Streamable HTTP**, URL
`http://127.0.0.1:8000/mcp`, then **Connect**. Under **Tools**, try
`get_unprocessed_events`, `get_site` (`SITE-001`) or `create_notification` with a
bad severity to see validation. Under **Resources**, read `study://rules`.

**Automated tests:** `python -m pytest -q` runs 28 tests: every MCP tool on its
own, and the agents end to end against a real server process. Tests use their
own database (`data/test.db`) and never touch your demo data.

## Check a run

```powershell
python scripts/check_run.py
```

Checks the acceptance criteria: every event processed, none twice, every event
has a logged decision, processing within 10 seconds, and at least one
auto-resolve, notify and flag. Exits with status 1 if a requirement fails.

## How decisions work

The rules live in [config/rules.yaml](config/rules.yaml), not in code or
prompts. The MCP server serves them as the `study://rules` resource, and both
agent modes read them from there.

| Agent | Event | Outcome |
|---|---|---|
| Document | `document_expiring` | 30 days or fewer: notify (low). More: log only |
| | `document_expired` | Always flag (high) and set the document to Expired |
| | `document_rejected` | Under 3 rejections: notify the site coordinator. 3rd or more: flag (high) |
| | `document_submitted` | Log only |
| Milestone and site | `milestone_delayed` | Under 7 days late: notify (low). 7 or more: flag (high) |
| | `approval_received` | Site at Identified or Selected: auto-resolve to Docs In Progress |
| | `site_ready_check`, `contract_signed` | All checks pass: auto-resolve to Ready To Activate. Otherwise notify what is missing (`contract_signed`: log) |
| Data quality | `record_data_issue` | Inferable (country from the city in the site name, unambiguous date format): auto-fix. Otherwise notify the data manager |
| | `staff_training_expired` | Always flag (medium) |

Agents decide on the **current** data, not only the event payload: if a
problem was fixed after the event was raised, they log that and do nothing.

## What the MCP server guarantees

- Every write tool validates its inputs and returns a clear error.
- No tool deletes anything.
- Every write is recorded in `agent_actions`, in the same transaction as the change.
- A site cannot be set to Ready To Activate unless the readiness check passes.
- `mark_event_processed` succeeds once per event, so no event is handled twice,
  even with two supervisors running.

| Read tools | Write tools |
|---|---|
| `get_unprocessed_events`, `get_site`, `list_site_documents`, `list_site_milestones`, `list_site_staff`, `get_study_summary` | `update_document_status`, `update_site_status`, `fix_record_field`, `create_notification`, `log_agent_action`, `mark_event_processed` |

Resources: `study://rules`, `study://reference`, `study://schema`.

## Configuration

| File | Controls |
|---|---|
| `config/rules.yaml` | Decision thresholds, severities, roles, routing, readiness definition |
| `config/generator.yaml` | Event interval, event weights, problem-case ranges, seed size |
| `config/agents.yaml` | MCP URL, polling, concurrency, timeouts, OpenAI model and prices |
| `config/reference.yaml` | City to country map (site names and country inference) |
| `.env` | `OPENAI_API_KEY`, `AGENT_MODEL`, optional `MCP_URL`, `STUDY_DB_PATH` |

## Project layout

```
common/       database schema, domain values, readiness check, config loader
generator/    seed data, the 10 event types, run loop, inject command
mcp_server/   server.py (the full server), hello.py (Stage 1 hello world)
agents/       graph.py (LangGraph), supervisor.py (polling loop),
              rules_engine.py (rules mode), llm_agents.py (OpenAI mode),
              mcp_tools.py (MCP client)
viewer/       Streamlit viewer
scripts/      check_run.py
tests/        MCP tool tests and end-to-end agent tests
docs/         demo guide and progress notes
```

## Troubleshooting

| Problem | Fix |
|---|---|
| `Cannot reach the MCP server` | Start `python -m mcp_server.server` first, or check `MCP_URL` |
| `Cannot delete study.db: it is in use` | Stop the MCP server, agents and viewer, then `seed --reset` |
| `No data yet` | Run `python -m generator seed --reset` |
| `OPENAI_API_KEY is not set` | Add it to `.env` in the project folder, or use rules mode |
| Port 8000 in use | `python -m mcp_server.server --port 8001` and set `MCP_URL=http://127.0.0.1:8001/mcp` |
| PowerShell will not run `Activate.ps1` | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` |

## Notes on versions

- The MCP SDK is pinned to 1.x (`mcp==1.30.0`, FastMCP) because
  `langchain-mcp-adapters` does not support MCP SDK 2.x yet.
- The agents keep one MCP session open; opening a session per tool call made
  each event take 10 to 17 seconds instead of under one.

See [docs/demo.md](docs/demo.md) for the demo run sheet, design talking
points and what would change for production.
