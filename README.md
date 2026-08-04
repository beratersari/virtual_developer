# JIRA Virtual Developer

**Version:** see root [`VERSION`](VERSION) (currently `0.2.0`)

A Python daemon that connects **Jira** (Server/DC or Cloud) to **OpenCode / Oh My OpenAgent**. It discovers work from a board poll, runs AI agents in isolated temporary Git clones, posts progress back to Jira, and can push feature branches and open GitLab merge requests.

---

## What it does

1. **Polls** a Jira board for To Do issues that match trigger labels and/or bot assignee  
2. **Routes** work from a per-issue `{params}` block (`Mode: plan` or `Mode: build`)  
3. **Runs** OpenCode agents (Prometheus planning, Atlas build, Oracle consult) in temp clones  
4. **Reports** plans, progress, errors, and completion as Jira comments  
5. **Pushes** work branches and opens merge requests when build mode finishes successfully  
6. **Serves** a localhost ops dashboard (tasks, poll monitor, safe settings) in the same process  

There is **no HTTP webhook intake**. Discovery is board polling only. Comment-driven bot commands are not a primary path (legacy plan-start labels still exist; see [Workflows](#workflows)).

---

## Architecture

```text
┌─────────────────────────── Jira (REST v2 + Agile) ───────────────────────────┐
│  Board / sprint  →  To Do + label or bot assignee  →  poll every N seconds     │
└───────────────────────────────────────┬───────────────────────────────────────┘
                                        │
                                        ▼
┌──────────────────────── JIRA Virtual Developer (one process) ────────────────┐
│  Board Poller  →  Job Processor  →  Agent Runner (opencode run --dir …)       │
│        │                  │                                                   │
│        │                  ├─ temp clone: feature/{ISSUE} from issue {params}  │
│        │                  ├─ Jira comments (progress / plan / error / done)   │
│        │                  └─ GitLab push + MR (build mode)                    │
│        │                                                                      │
│  Ops dashboard (FastAPI + React SPA)  ·  stuck-job monitor  ·  JSON state     │
└───────────────────────────────────────────────────────────────────────────────┘
```

| Component | Role |
|-----------|------|
| **Board poller** | Sole intake. Reads board/sprint issues; writes a poll snapshot for the UI |
| **Job processor** | State machine, concurrency limits, plan vs build routing, fail + Jira notify |
| **Agent runner** | Spawns OpenCode with plan/build mode prompts; streams session logs |
| **Jira client** | REST API v2 + Agile; Bearer (on-prem PAT) or Basic (Cloud email+token) |
| **Git manager** | Clone, branch, commit identity, push, MR via `glab` / GitLab API |
| **State store** | Per-issue JSON under `.jira-agent/state/`; job records for the dashboard |
| **Ops dashboard** | REST + WebSocket + static SPA from `web/dist` |

### Agents (Oh My OpenAgent)

| Setting | Role |
|---------|------|
| **`DEFAULT_AGENT`** (e.g. `atlas`) | OpenCode persona for both `Mode: plan` and `Mode: build` |
| **Plan vs build text** | `agent/PLAN_PROMPT.md` vs `agent/BUILD_PROMPT.md` (mode only) |
| **Oracle** | Architecture Q&A when routing detects consultative wording |

---

## Requirements

- **Python 3.12+** recommended (3.10–3.13 also used on Windows offline wheels)  
- **OpenCode** CLI on `PATH` (`OPENCODE_CLI`, default `opencode`) with **oh-my-openagent** plugin  
- **Git**  
- **glab** (GitLab CLI) when push/MR is enabled  
- Jira access (board browse, comment, optional transitions)  
- GitLab PAT with clone/push/MR rights when using remote workspaces  

---

## Quick start

### Linux / macOS

```bash
# From repo root
cp .env.example .env
# Edit .env — at least JIRA_HOST, JIRA_API_TOKEN, JIRA_BOARD_ID, GITLAB_* as needed

python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Optional installer (deps + OpenCode + glab heuristics)
./install.sh

python cli.py init
python cli.py start
```

Ops dashboard (default): **http://127.0.0.1:8080**  
Stop the daemon with **Ctrl+C**.

### Windows (offline zip)

CI builds `virtual_developer-windows-x64-*.zip` (see [packaging/windows/README.md](packaging/windows/README.md)).

```cmd
:: Extract zip so install.bat sits next to vendor\ and src\
install.bat
:: Or, if OpenCode is already installed on this PC (skip OpenCode install):
install-dashboard.bat
:: Edit .env, then:
start.bat
::   or: start-backend.bat / start-frontend.bat
```

Open the OpenCode TUI only via **`start-opencode.bat`** from the project folder (after full `install.bat`) — not bare `opencode` from your user profile (home-as-project causes long black-screen indexing).

### Docker

See [Dockerfile](Dockerfile) and [`.github/workflows/docker.yml`](.github/workflows/docker.yml) if you run containerized builds.

---

## Jira issue template (`{params}`)

Every issue the bot should work on needs a **`{params}` … `{params}`** block in the description (or summary fields scanned by the parser). Repository URL is **per issue**, not a global env var.

```text
{params}
Repository: https://gitlab.example.com/group/your-repo.git
Source branch: feature/PROJ-123
Target branch: develop
Mode: plan
{params}
```

| Field | Meaning |
|-------|---------|
| **Repository** | GitLab clone URL (aliases: Repo, GitLab, Project URL) |
| **Source branch** | Work / MR source branch. If missing or equal to a base name (`main`/`develop`/…), work branch becomes `feature/{ISSUE_KEY}` |
| **Target branch** | Must exist on remote; work is based on it; MR merges **into** it |
| **Mode** | **`plan`** — plan only, append plan to Jira, no push. **`build`** — implement, push, open MR |

Mode aliases: `planning`/`prometheus` → plan; `execute`/`execution`/`atlas`/`implement` → build.

Incomplete templates cause a **user-visible Jira comment** with the format help (see `src/issue_git_spec.py`).

### When the poller picks up an issue

All of the following roughly apply **for first intake**:

- Issue is on the configured **board**  
- Status looks like **To Do** (name or `statusCategory` new/backlog-like)  
- Has a **trigger label** (`TRIGGER_LABELS`, default `ai-assist,bot`) **and/or** assignee name looks like the bot (when `TRIGGER_ON_ASSIGNMENT=true`)  
- Not already **in-flight** (`planning` / `executing`) — poll noise never restarts live work  

**Trigger labels only mean “allowed to pick up work.”** They do **not** mean the bot
re-runs the same ticket on every poll after work has already finished (see
[Plans never auto-start](#plans-never-auto-start-intentional) below).

Terminal issues (`error` / `cancelled` / `completed`) reprocess only when moved
back to **To Do** (or another rework signal such as editing the description after
an ERROR). A successful **plan** ends in `plan_ready`, which is different — see below.

---

## Workflows

### Plan (`Mode: plan`)

1. Poller accepts issue → state `planning`  
2. Prometheus runs in a temp clone  
3. Plan posted to Jira (comment + description) → local state **`plan_ready`**, label **`ai-plan-ready`**  
4. Bot **stops**. The ticket may still show **To Do** on the board with `bot` — that is normal.  

### Plans never auto-start (intentional)

After planning finishes, the issue is **waiting for an explicit implement signal**.
Sitting on **To Do** with only `bot` / `ai-assist` will **not** start coding.

```text
To Do + bot  →  Mode: plan runs  →  plan_ready + ai-plan-ready
                                         │
                    still To Do + bot alone │  no further work
                                         ▼
                         waiting (not stuck)
                                         │
          ┌──────────────────────────────┼──────────────────────────────┐
          ▼                              ▼                              ▼
  Add label                    Open a NEW issue              (Do not rely on
  ai-start-work                with Mode: build              Mode: build alone
  or ai-execute                (same {params})               on the plan ticket)
  while still To Do
          │                              │
          └──────────►  build / implement  ◄──────────────────┘
```

| What you see | What it means |
|--------------|----------------|
| To Do + `bot` + local `plan_ready` | Plan done; waiting for start signal |
| Label `ai-plan-ready` | Bot finished planning (not a start label) |
| Labels `ai-start-work` or `ai-execute` on To Do | **Start implementation** on that same ticket |
| New ticket with `Mode: build` + trigger label | Independent build run (recommended for clean history) |
| Daemon log `Skip cold-start requeue … plan_ready` | Correct — daemon restart will not re-plan or auto-build |

**How to implement after a plan**

1. **Same ticket:** while status is **To Do**, add label `ai-start-work` or `ai-execute`  
   (next poll starts the build path), **or**  
2. **New ticket:** create an issue with the same `{params}` repo/branches and
   `Mode: build`, plus a trigger label (`bot` / `ai-assist`).

Changing the plan ticket to `Mode: build` **alone** does **not** auto-start
(product rule so plans are reviewed before code). Dashboard **Start** is also
disabled for the same reason.

### Build (`Mode: build`)

1. Poller accepts issue → prepare git workspace from `{params}`  
2. Atlas (orchestrator) implements against the plan / description  
3. On success: push branch, open MR, comment completion → `completed`  
4. On failure: state `error` **and** Jira error comment (`_fail_issue` / `post_error`)  

### Oracle

Consultative questions without implementation keywords may route to Oracle (read-only style advice). Implementation language forces plan/build paths instead.

### Task statuses

```text
pending → planning | executing → (plan_ready) → completed | error | cancelled
```

| Status | Meaning for operators |
|--------|------------------------|
| `planning` / `executing` | Agent running — poller will not restart from board noise |
| `plan_ready` | Plan finished; **not** an error. Needs start label or new `Mode: build` issue |
| `completed` | Done (build delivered or soft no-op completion) |
| `error` | Failed; fix description / params, then return to To Do (or edit text) to requeue |
| `cancelled` | Operator cancel; not auto-retried while still To Do |

Stuck in-flight jobs are watchdogged by the daemon. Startup recovers orphaned disk `planning`/`executing` states to `error`.

---

## Ops dashboard

Enabled by default with the daemon (`DASHBOARD_ENABLED=true`).

| | |
|--|--|
| URL | `http://127.0.0.1:8080` |
| Stack | FastAPI in-daemon + WebSocket `/ws` + React SPA (`web/`) |
| Auth | **None in v1** — keep bind host localhost unless you put a proxy/auth in front |

**Frontend is display-only.** Filtering, poll math, and settings rules live on the backend.

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/health` | Liveness + version |
| GET | `/api/meta` | Version + server time |
| GET | `/api/tasks` | Agent task list |
| GET | `/api/jobs` | Paginated job history |
| GET | `/api/jobs/{id}` | Job detail |
| DELETE | `/api/jobs/{id}` | Delete job record |
| GET | `/api/tasks/{key}` | Task detail for issue |
| POST | `/api/tasks/{key}/cancel` | Cancel live work (preferred over CLI when daemon runs) |
| GET | `/api/poll` | Last poll snapshot + countdown |
| GET/PATCH | `/api/settings` | Safe settings (no token values) |
| GET | `/api/models` | Available OpenCode models |
| GET | `/api/dashboard` | Full envelope |
| WS | `/ws` | Live pushes |

Writable runtime settings (examples): board id, poll interval, trigger labels, `trigger_on_assignment`, `max_concurrent_jobs`, default model.  
`DASHBOARD_ALLOW_REMOTE=false` forces non-loopback hosts back to `127.0.0.1`.

### Building the UI

```bash
cd web && npm install && npm run build
# dist/ is served by the daemon; Vite dev: npm run dev (proxies to :8080)
```

---

## Configuration

Copy [`.env.example`](.env.example) → `.env`. Secrets must never be committed.

### Jira connection

| Variable | Description |
|----------|-------------|
| `JIRA_HOST` | Base URL (no trailing slash preferred) |
| `JIRA_API_TOKEN` | Cloud API token **or** on-prem personal access token |
| `JIRA_EMAIL` | **Cloud/dev only** — with token uses HTTP Basic. Leave empty for Bearer PAT (prod) |
| `JIRA_PROJECTS` | Comma-separated project keys (reference / allow-list style) |
| `JIRA_BOARD_ID` | Agile board id to poll (**required** for discovery) |

Auth summary:

- **Prod / on-prem:** `JIRA_HOST` + `JIRA_API_TOKEN` → `Authorization: Bearer …`  
- **Cloud (dev):** `JIRA_HOST` + `JIRA_EMAIL` + `JIRA_API_TOKEN` → Basic email:token  

TLS verify is currently off for typical on-prem certs; do not “fix” that without a deliberate secure path.

### Intake & dashboard

| Variable | Default | Description |
|----------|---------|-------------|
| `POLL_INTERVAL_SECONDS` | `30` | Board poll interval |
| `MAX_CONCURRENT_JOBS` | `6` | Parallel agent jobs |
| `POLL_DISPATCH_WORKERS` | `8` | Parallel dispatch/transitions per poll cycle |
| `DASHBOARD_ENABLED` | `true` | Serve ops UI with daemon |
| `DASHBOARD_HOST` | `127.0.0.1` | Bind host |
| `DASHBOARD_PORT` | `8080` | HTTP port |
| `DASHBOARD_ALLOW_REMOTE` | `false` | Allow non-loopback bind |

### Triggers

| Variable | Default |
|----------|---------|
| `TRIGGER_LABELS` | `ai-assist,bot` |
| `TRIGGER_ON_ASSIGNMENT` | `true` |
| `TRIGGER_MENTIONS` | `@DevBot,@AI` |

### GitLab

| Variable | Description |
|----------|-------------|
| `GITLAB_PAT` | Clone / push / MR token |
| `GITLAB_ALLOWED_HOSTS` | **Required when PAT is set** — comma-separated hosts that may receive the PAT (fail-closed) |
| `GIT_USER_NAME` / `GIT_USER_EMAIL` | Commit identity in temp clones |

Repo URL and branches always come from the issue `{params}` block.

### Agent / OpenCode

| Variable | Default | Description |
|----------|---------|-------------|
| `OPENCODE_CLI` | `opencode` | CLI binary/command |
| `DEFAULT_MODEL` | (see `.env.example`) | Passed to `opencode run --model` |
| `DEFAULT_AGENT` | `atlas` | OpenCode agent for plan and build jobs |
| `AGENT_PROMPTS_DIR` | `agent` | Dir with `PLAN_PROMPT.md` + `BUILD_PROMPT.md` only |
| `SISYPHUS_PLANS_DIR` | `.sisyphus/plans` | Plan markdown location |
| `AGENT_TASK_TIMEOUT_SECONDS` | `1800` | Per-attempt timeout |
| `AGENT_TASK_MAX_RETRIES` | `3` | Retries with exponential backoff |
| `TEMP_DIR_BASE` | `.temp` | Temp clone root |
| `TEMP_CLEANUP_POLICY` | `age` / `never` | Cleanup policy (see `.env.example`) |

List or set models:

```bash
python cli.py models
python cli.py models --set provider/model-id
```

---

## CLI

```bash
python cli.py --help
python cli.py --version

# Lifecycle
python cli.py init              # dirs + .env from example
python cli.py start             # daemon: poller + dashboard + monitor
python cli.py config            # safe config dump
python cli.py models            # list OpenCode models

# Issue ops (need Jira config)
python cli.py process PROJ-123  # force-process one issue
python cli.py process PROJ-123 --dry-run
python cli.py status            # active issues table
python cli.py show PROJ-123
python cli.py cancel PROJ-123   # state cancel; kill live agent via dashboard if daemon is up
python cli.py costs             # token/cost rollup from state files

# Local agent smoke (no Jira)
python cli.py test-issue -t "Fix bugs" -d "Fix calculator divide by zero" -p sample_project
python cli.py test-issue -t "Plan feature" -d "..." --plan-only --model provider/id

# Simulated Jira (in-memory REST only; does not push to the daemon)
python cli.py simulate start-server --port 7001
python cli.py simulate create-issue -s "Title" -d "..." -a DevBot -l ai-assist
python cli.py simulate list-issues
python cli.py simulate show-issue SIM-1001
```

---

## Project layout

```text
virtual_developer/
├── cli.py                 # Click CLI entry
├── VERSION                # SemVer product version
├── .env.example           # Config template
├── requirements.txt
├── agent/PLAN_PROMPT.md   # Plan mode prompt
├── agent/BUILD_PROMPT.md  # Build mode prompt
├── src/
│   ├── daemon.py          # Process entry: poller + dashboard + monitor
│   ├── config.py
│   ├── processor.py       # Job lifecycle
│   ├── git_manager.py
│   ├── issue_git_spec.py  # {params} parser
│   ├── jira/              # client, poller, simulated client
│   ├── orchestrator/      # agent_runner, prompts, workflow_router
│   ├── reporter/          # Jira comments
│   ├── state/             # models, manager, job_store
│   └── dashboard/         # FastAPI API, schemas, poll snapshot
├── web/                   # React + Vite + Tailwind SPA → web/dist
├── packaging/windows/     # Offline Windows dist
├── sample_project/        # Calculator with intentional bugs for test-issue
├── tests/                 # Pytest suite
└── AGENTS.md              # Contributor / AI agent rules for this repo
```

---

## Testing

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt pytest pytest-asyncio pytest-cov
.venv/bin/python -m pytest tests/ --ignore=tests/test_logical_issues.py -q
```

- Prefer unit tests with mocks (no live Jira in CI).  
- **`tests/test_logical_issues.py`** is excluded from the default green suite: it documents desired behaviour that is still incorrect until fixed.  
- Do not commit `.coverage`, `htmlcov/`, or `.pytest_cache/`.

---

## Git flow (this repository)

Default development branch is **`develop`** (not `main`).

```text
feature/*  →  MR into develop  →  (release) develop → main
```

Commits and MR titles use [Conventional Commits](https://www.conventionalcommits.org/):

```text
feat(dashboard): show poll countdown
fix(poller): do not requeue in-flight issues
```

Full rules: [AGENTS.md](AGENTS.md). For **target** product repos that agents work in, branch `feature/{JIRA_ISSUE_ID}` and conventional commit policy live in `agent/BUILD_PROMPT.md` / `commitMsgFormat.md`.

---

## State on disk

```text
.jira-agent/
  state/          # per-issue JSON (status, tokens, plan path, metadata)
  sessions/       # agent stdout/stderr session logs (not auto-deleted by temp cleanup)
.temp/            # per-issue git clones (cleanup policy from env)
.sisyphus/plans/  # plan markdown when using local plans dir
logs/             # optional log file (LOG_FILE)
```

---

## Troubleshooting

| Symptom | What to check |
|---------|----------------|
| Poller idle / no jobs | `JIRA_BOARD_ID`, issue in To Do, trigger label or bot assignee, `python cli.py process KEY` |
| Ticket on To Do with `bot` but bot does nothing | Local status may be **`plan_ready`** (plan finished). `bot` alone does not re-run or auto-build — add `ai-start-work` / `ai-execute`, or open a new `Mode: build` issue. See [Plans never auto-start](#plans-never-auto-start-intentional). |
| 401 / 403 from Jira | Token, Cloud needs `JIRA_EMAIL` for API tokens, host URL, project permissions |
| Agent never starts | `opencode` / plugin install, `DEFAULT_MODEL`, session logs under `.jira-agent/sessions/` |
| Git / MR fails | Issue `{params}` complete, `GITLAB_PAT`, `GITLAB_ALLOWED_HOSTS` includes that host, `glab` available |
| Dashboard unreachable | Daemon running? `DASHBOARD_*` bind, open `http://127.0.0.1:8080` |
| Windows TUI black screen | Use `start-opencode.bat` from project dir; re-run `install.bat`; see `packaging/windows/` diag notes |
| Stuck `planning`/`executing` | Restart daemon (orphan recovery) or cancel from dashboard; check watchdog logs |

```bash
python cli.py config
python cli.py show PROJ-123
# DEBUG=true python cli.py start
```

---

## Security notes

1. Keep **`.env`** out of git (tokens, PATs).  
2. Dashboard has **no auth** — localhost only unless you knowingly expose it.  
3. `GITLAB_ALLOWED_HOSTS` prevents sending the PAT to arbitrary hosts from issue text.  
4. Prefer a dedicated Jira bot account with least privilege.  
5. Never log raw API tokens or PATs.

---

## Related documentation

| Doc | Purpose |
|-----|---------|
| [AGENTS.md](AGENTS.md) | Coding standards, Jira rules, dashboard rules, Windows packaging hard-won fixes |
| [agent/PLAN_PROMPT.md](agent/PLAN_PROMPT.md) | Plan-mode prompt |
| [agent/BUILD_PROMPT.md](agent/BUILD_PROMPT.md) | Build-mode prompt |
| [packaging/windows/README.md](packaging/windows/README.md) | Offline zip design and versioning |
| [`.env.example`](.env.example) | Full environment template with comments |
| [web/README.md](web/README.md) | Frontend notes (if present) |

---

## License

MIT
