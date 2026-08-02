# AGENTS.md — Virtual Developer

Instructions for humans and AI agents working on **this** repository (`virtual_developer`).

---

## 1. Product snapshot

JIRA Virtual Developer is a Python daemon that:

1. Discovers issues (board poller only)
2. Routes work (plan / direct execution / oracle)
3. Runs Oh My OpenAgent / OpenCode in isolated temp git clones
4. Posts progress, plans, errors, reviews, and completion back to Jira
5. Pushes feature branches and opens merge requests
6. Serves an **ops dashboard** (same process) for tasks, poll monitor, and safe settings

**Default development branch:** `develop` (not `main`).  
**Integration branch for releases / stable:** `main`.

---

## 2. Coding standards

### Language & layout

- Python 3.12+, type hints on public APIs where practical.
- Package root: `src/`. Entry points: `cli.py`, `src/daemon.py`.
- Prefer small, focused modules; avoid shared mutable singletons across concurrent issues (`JobProcessor.git_manager` / `agent_runner` must not be overwritten mid-flight for parallel jobs when you touch concurrency).
- Do not log secrets (Jira tokens, GitLab PATs). Never commit `.env`.

### HTTP / TLS (mandatory)

- **All outbound HTTP(S) clients must use `verify=False`** (httpx, requests, etc.).
  Applies to Jira, GitLab, connection probes, MR API fallbacks, and any new
  external HTTPS call — including Cloud (`*.atlassian.net`) and on-prem.
- Rationale: on-prem / enterprise TLS interception and self-signed certs are
  common; fail-open TLS verification is a product requirement until a secure
  custom-CA path is designed.
- When constructing `httpx.Client(...)` / `httpx.AsyncClient(...)`, always pass
  **`verify=False` explicitly** (do not rely on defaults).
- Do **not** “fix” TLS verification back on without an explicit product decision
  and a supported CA-bundle configuration path.
- Prefer silencing urllib3 `InsecureRequestWarning` at the client boundary (as
  `JiraClient` already does) rather than leaving noisy logs.

### State & workflows

- Task statuses: `pending` → `planning` | `executing` → (`plan_ready`) → `completed` | `error` | `cancelled`.
- **Never** restart work that is in-flight (`planning` / `executing`) from poll noise.
- Terminal reprocess only when the user moves the issue back to **To Do** (or an explicit rework signal).
- **Plans never auto-start** (intentional). After `plan_ready`, do **not** promote the same ticket via `Mode: build` alone. To implement: open a **new** issue with `Mode: build` (same `{params}` repo/branches), **or** add label `ai-start-work` / `ai-execute` while the plan ticket is **To Do**. Dashboard Start stays disabled.
- Failures must set `ERROR` **and** notify Jira (`_fail_issue` / `post_error`). Stuck in-flight jobs are watchdogged in the daemon. Fail/cancel/watchdog use **CAS** so late ERROR cannot overwrite `COMPLETED` / `CANCELLED`.
- Dashboard **Cancel** kills agent children immediately and must **not** wait on the long-held workflow issue lock.
- `update_state(metadata={...})` **merges** metadata; never wipe unrelated keys.
- Temp clones: default policy `age` with `TEMP_CLEANUP_MAX_AGE_DAYS=1` (24h); purge on daemon start and hourly.

### Error handling

- Outer `try/except` on workflow entrypoints → `_fail_issue` + log with the project logger (`logger.exception(msg, exc)` — two args).
- Soft failures that users care about (push/MR failure) should still leave a Jira comment.
- Do not leave issues stuck in intermediate statuses after exceptions.

### Style

- Match existing naming and logging style in the file you edit.
- No drive-by refactors or unrelated file churn.
- Comments only for non-obvious intent (not narration of the code).

---

## 3. Jira (on-prem)

### Auth (only this)

```env
JIRA_HOST=https://your-on-prem-jira.example.com
JIRA_API_TOKEN=your-api-token-here
```

- REST **API v2** (`/rest/api/2/...`), Agile at `/rest/agile/1.0/...`.
- Auth header: **`Authorization: Bearer {JIRA_API_TOKEN}`** only for on-prem PAT.
  Cloud may use Basic (email + API token) when `JIRA_EMAIL` is set.
- No username/password and no Cloud-only `accountId` assumptions for core bot auth.
- TLS: **`verify=False` on all Jira HTTP** (see §2 HTTP / TLS).

### Behaviour expectations

- Comments use plain string bodies (Server/DC style); ADF is fallback only on 400.
- Report **errors**, **stuck states**, **retries**, and **completion** via Jira comments.
- Poller focuses on board/sprint + To Do + trigger labels / bot assignee.
- **No HTTP webhook intake** — comments are not ingested unless a separate comment-polling path is added later.

### Config checklist (common)

| Variable | Role |
|----------|------|
| `JIRA_HOST` | Base URL |
| `JIRA_API_TOKEN` | Bearer token |
| `JIRA_PROJECTS` | Project keys (config/reference; board scopes poller) |
| `JIRA_BOARD_ID` | Sprint/board poller board |
| `TRIGGER_LABELS` | Labels that make an issue eligible (e.g. `ai-assist,bot`) |
| `TRIGGER_ASSIGNEE_NAMES` | Assignee name fragments for bot-assignment trigger (e.g. `devbot,jira ai bot`) |
| `TEMP_CLEANUP_POLICY` | `age` (default) / `always` / `on_success` / `never` |
| `TEMP_CLEANUP_MAX_AGE_DAYS` | Age cutoff for temp clones (default `1` = 24 hours) |
| `POLL_INTERVAL_SECONDS` | Board poller interval (poller always runs) |
| `DASHBOARD_ENABLED` | Serve ops dashboard with the daemon (default true) |
| `DASHBOARD_HOST` | Dashboard bind host (default `127.0.0.1`) |
| `DASHBOARD_PORT` | Dashboard HTTP port (default `8080`) |

---

## 3b. Ops dashboard

### Stack

| Layer | Technology |
|-------|------------|
| API | FastAPI in the **daemon process** (with poller + jobs) |
| Live updates | WebSocket `/ws` |
| Frontend | Vite + React + TypeScript + Tailwind (`web/`) — **display only** |
| Palette | Neutral ops console (`web/src/index.css` tokens): flat zinc surfaces, single blue accent for actions; status via small tone dots (not rainbow chips). Job detail is run-scoped (prompts/logs for selected job only; other runs collapsed). |

### Rules

- **All business logic is backend-only.** Frontend only renders DTOs from REST/WS (no filter rules, no poll scheduling math except displaying server-provided countdown).
- Poller writes a thread-safe **poll snapshot** (`src/dashboard/snapshot.py`) each cycle: every board issue, label/assignee match flags, `will_process`, next poll time.
- Tasks come from state store + live `_contexts` keys (`live: true` when process cache holds the issue).
- Settings API exposes **safe projection only** (no token values). Writable runtime fields: board id, poll interval, trigger labels, trigger_on_assignment, max_concurrent_jobs. Plans never auto-start (see §2).
- **No dashboard auth in v1** — keep bind host localhost unless operators knowingly open it.
- Version is read from repo root `VERSION`.

### Layout

```text
src/dashboard/     # FastAPI app, schemas, service, poll snapshot
web/               # React SPA (build → web/dist, served by FastAPI)
```

Build UI: `cd web && npm install && npm run build`.  
Open: `http://127.0.0.1:8080` after daemon start.

### Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/meta` | Version + server time |
| GET | `/api/tasks` | Agent job list |
| GET | `/api/poll` | Last poll snapshot + countdown |
| GET/PATCH | `/api/settings` | Safe settings |
| GET | `/api/dashboard` | Full envelope |
| WS | `/ws` | Live dashboard pushes |

---

## 4. Testing

### Commands

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt pytest pytest-asyncio pytest-cov
.venv/bin/python -m pytest tests/ --ignore=tests/test_logical_issues.py -q
.venv/bin/python -m pytest tests/ --ignore=tests/test_logical_issues.py --cov=src --cov-branch
```

### Rules

- Put tests under `tests/`; use fixtures in `tests/conftest.py`.
- Prefer unit tests with mocks for Jira/git/agent subprocesses (no live Jira in CI).
- Green CI / default suite: **ignore** `tests/test_logical_issues.py` (those assert desired behaviour that is still broken by design until fixed).
- When you fix a bug listed there, flip the corresponding test to the normal suite and make it pass.
- New behaviour → add or update tests in the same change.
- Do not commit `.coverage`, `coverage.json`, `.pytest_cache/`, or `htmlcov/`.

### Coverage expectations

- Aim for high statement coverage on `src/` when touching critical paths (state, processor, reporter, poller).
- Branch 100% is aspirational; do not add worthless tests only to paint branches green.

---

## 5. Git flow & branch architecture

```
main          ← stable / release line (protected when possible)
  ▲
  │  merge via MR only (squash or merge commit with conventional title)
  │
develop       ← default integration branch for day-to-day work
  ▲
  │  MRs from feature branches
  │
feature/...   ← short-lived work branches
fix/...
chore/...
```

### Rules

1. **All new development starts from `develop`**, not `main`.
2. Create a short-lived branch for each change:
   - `feature/<short-topic>` or `feature/<JIRA-KEY>-short-topic`
   - `fix/<short-topic>`
   - `chore/<short-topic>`, `test/<short-topic>`, `docs/<short-topic>`, `ci/<short-topic>`
3. Open an MR **into `develop`** (not into `main` unless releasing).
4. Release / promote: MR **`develop` → `main`** when ready; title still uses the conventional pattern below (e.g. `chore(release): promote develop to main`).
5. Never force-push `main` or `develop` unless the team explicitly agrees.
6. Do not leave long-lived “personal” branches on the remote without an MR.

### Agent work on *target* product repos

When this daemon’s agents commit inside a **customer/project** temp clone:

- Branch: `feature/{JIRA_ISSUE_ID}` (see `commitMsgFormat.md` / `GitManager`).
- The **system** pushes and creates the MR; agents should not push.
- MR title and commit messages for those product MRs must still follow the conventional pattern in §6 (include the issue key in the scope or body, e.g. `feat(PROJ-123): add retry guard`).

---

## 6. Commit & merge-request naming (mandatory)

Use [Conventional Commits](https://www.conventionalcommits.org/)-style messages for **both commits and MR titles**.

### Format

```text
<type>(<optional-scope>): <short explanation>
```

### Allowed types

| Type | Use for |
|------|---------|
| `feat` | New user-facing capability |
| `fix` | Bug fix |
| `test` | Tests only |
| `docs` | Documentation only |
| `ci` | CI/CD, workflows, pipelines |
| `chore` | Tooling, deps, chores, non-user-facing maintenance |
| `refactor` | Internal restructure without behaviour change |
| `perf` | Performance |

Scopes are optional but encouraged: `jira`, `state`, `processor`, `git`, `daemon`, `auth`, `release`, etc.

### Examples (good)

```text
feat(jira): report stuck jobs to issue comments
fix(state): merge metadata instead of replacing
test(processor): cover terminal reprocess guards
docs(agents): document develop-branch workflow
chore(deps): pin pytest-cov
ci(docker): cache pip layers
```

### Examples (bad — do not use)

```text
Merged from feature/foo into develop
Merge branch 'feature/x' into develop
Update stuff
bug fix
WIP
final final 2
```

**MR titles** must use the same pattern as commits.  
Do **not** accept default GitLab/GitHub titles like “Merged from …” / “Merge branch …”. Rename the MR before merge if the host fills a bad default.

### Commit body (optional but preferred for non-trivial work)

```text
feat(processor): fail workflows with Jira notification

Unhandled exceptions left issues stuck in EXECUTING.
Route crashes through _fail_issue and post_error.

Closes: PROJ-123
```

- Subject ≤ ~72 characters, imperative mood, no trailing period required.
- Reference Jira keys in the body (`Closes: KEY-123`) and/or scope when the change maps to a ticket.

### Who creates what

| Actor | Commits | Push | MR |
|-------|---------|------|-----|
| Human / AI on **this** repo | You | After review / when asked | You — into `develop` |
| OpenCode agent in **target** repo | Agent (conventional message) | Orchestrator (`GitManager`) | Orchestrator — title must be conventional, not “Merged from…” |

When creating MRs via `glab` / API, set **title** explicitly:

```bash
glab mr create --title "feat(auth): bearer-only jira token" --description "..." --target-branch develop
```

---

## 7. Local setup (quick)

```bash
cp .env.example .env   # set JIRA_HOST, JIRA_API_TOKEN, PROJECT_GITLAB_URL, GITLAB_PAT as needed
./install.sh           # or install.bat
.venv/bin/python cli.py init
.venv/bin/python -m src.daemon   # or project’s documented start command
```

**Windows (offline zip from CI):** extract artifact → `install.bat` → open TUI only via **`start-opencode.bat`** from the project folder (never bare `opencode` from `%USERPROFILE%`).

---

## 8. Do / Don’t checklist for agents

**Do**

- Branch from latest `develop`.
- Keep changes scoped; add tests for behaviour you change.
- Use conventional `type(scope): summary` for every commit and MR.
- Keep Jira user-visible on failure/stuck states.
- Preserve on-prem Bearer auth only (Cloud may use email+token Basic).
- Use **`verify=False`** on every outbound HTTP(S) client (Jira, GitLab, probes).
- For Windows dist changes: run/extend `packaging/windows/e2e-smoke.ps1` expectations (plugin tree size, `rg.exe`, pinned `oh-my-openagent@`, launcher).

**Don’t**

- Commit secrets or coverage artifacts.
- Merge to `main` for routine work.
- Leave jobs stuck without Jira notification.
- Use merge titles like “Merged from feature/…”.
- Reintroduce Jira username/basic auth without an explicit product decision.
- Enable TLS certificate verification on outbound HTTP without an explicit secure-path decision.
- Ship Windows packaging that prunes plugin `*.md`, uses junctions for Bun cache, or registers legacy `oh-my-opencode` without the openagent id (see §9).

---

## 9. Windows offline dist & OpenCode — hard-won rules

This section exists so agents **do not reintroduce** bugs we already paid for in production installers. Packaging lives under `packaging/windows/` and `.github/workflows/windows-dist.yml`.

### 9.1 Product layout (must stay true)

| Item | Rule |
|------|------|
| OpenCode home | **`%USERPROFILE%\.opencode` only** (bin, plugin `node_modules`, configs). No second install at `C:\vd\opencode` unless `VD_OPENCODE_ROOT` is set on purpose. |
| Global config | Mirror valid JSON to **`%USERPROFILE%\.config\opencode\`** (OpenCode’s real discovery path). |
| Plugin cache | OpenCode/Bun loads npm plugins from **`%USERPROFILE%\.cache\opencode\`** (`node_modules` and/or `packages/<name>`). Installer must **full-copy** the plugin tree there — **not** a junction. |
| TUI launcher | Ship **`start-opencode.bat`** that `cd`s to the **project** directory. Document: never run `opencode` from `C:\Users\<name>` (home as project = multi-minute black screen indexing the profile). |
| Product version | Repo root **`VERSION`** (`MAJOR.MINOR.PATCH`). CI names zips via `packaging/windows/resolve-version.ps1` (develop prerelease / main build metadata / `v*` releases). |

### 9.2 cmd.exe / install.bat landmines

1. **Never `echo ... -> path` in `.bat` files.** In cmd, `>` is redirect.  
   `echo [OK] config -> %OPENCODE_HOME%\opencode.json` **overwrites** `opencode.json` with the text `[OK] config -` (exactly the “invalid JSON” failure users hit).  
   Same pattern can clobber `opencode.exe`. Always use `^>` or rephrase without `>`.
2. **`install.bat` must be idempotent:** wipe previous `.opencode`, legacy short paths, stale PATH entries, and broken managed configs before extract — users should only re-run the installer.
3. Prefer **PowerShell `-File` scripts** for non-trivial logic; never use PowerShell parameter name **`$args`** (automatic variable — breaks `Start-Process -ArgumentList`).

### 9.3 oh-my-openagent / oh-my-opencode plugin

| Symptom | Real cause | Correct approach |
|---------|------------|------------------|
| TUI black screen 2–3+ min, then default **build/plan** agents | Bun cannot resolve plugin offline; falls back to stock OpenCode | Full offline `node_modules` + seed cache; pin plugin with **version** |
| Config `plugin: ["oh-my-opencode"]` or unversioned name | OpenCode **auto-migrates** to `oh-my-openagent@…` and may re-fetch via Bun | Register **`oh-my-openagent@X.Y.Z`** in `opencode.json` from the start |
| “Install finished too fast” but agents wrong | Junction-only cache or incomplete tree | **robocopy** full tree; assert **≥ ~500 files** and many `*.md` skills |
| Plugin “there” but no Sisyphus/Prometheus | **Pruned `*.md`** / deleted `docs`/`test` dirs that held skills | Do **not** strip skill markdown. Light prune (e.g. `*.map`) only |
| Dual package names | Rename transition: npm still has both | Offline install **both** `oh-my-openagent` and `oh-my-opencode` at the same pin (or full duplicate folders) |

**Pin format (required):**

```json
{
  "$schema": "https://opencode.ai/config.json",
  "autoupdate": false,
  "plugin": ["oh-my-openagent@4.19.3"]
}
```

`autoupdate: false` reduces surprise network work on air-gapped / flaky networks.

### 9.4 Offline / first-run network hangs (black screen)

User diagnostics (`packaging/windows/collect-opencode-diag.bat`) showed:

1. **`directory="C:\\Users\\anon"`** — entire home treated as the project → long hang at “booting location services”.
2. **`downloading ripgrep`** into `~/.cache/opencode/bin/rg.exe` — stalls offline; tools hang.
3. **`Failed to fetch models.dev`** — log spam / slow start on restricted networks.

**Mitigations already in the dist (keep them):**

- Seed **`%USERPROFILE%\.cache\opencode\bin\rg.exe`** from `vendor\bin\rg.exe` during `install.bat`.
- Set user env **`OPENCODE_DISABLE_MODELS_FETCH=1`** (and set it in `start-opencode.bat`).
- Launcher always starts in the **product/project folder**, not the user profile.

### 9.5 CI / packaging process (do not weaken)

1. **Build** on `windows-latest`: `build-dist.ps1` → `vendor/opencode-home.zip` (never expand `node_modules` into the outer artifact).
2. **E2E smoke must prove real user install:** extract to a deep path, run `install.bat` non-interactively, assert:
   - AMD64 `opencode.exe` + `--version`
   - Valid JSON configs (not echo garbage)
   - Full plugin tree under home **and** cache (file count + `dist/agents` + markdown)
   - Plugin id pinned to **`oh-my-openagent@…`**
   - `rg.exe` present under cache `bin`
   - `start-opencode.bat` present at payload root
3. **Artifact naming:** SemVer from `VERSION` + channel (`resolve-version.ps1`). Do not go back to opaque `dev-<sha>` only.
4. After shipping: delete merged feature branches; do not leave long-lived `feature/*` on origin without an open MR.

### 9.6 Debugging a black screen (shareable evidence)

When TUI shows nothing, **logs still exist**:

| Path | What |
|------|------|
| `%USERPROFILE%\.local\share\opencode\log\` | Runtime logs (often the smoking gun) |
| `%USERPROFILE%\.config\opencode\opencode.json` | Plugin registration |
| File counts under `.opencode` / `.cache\opencode` | Incomplete offline seed |
| `packaging\windows\collect-opencode-diag.bat` | Zip of configs + counts + non-TUI commands |

**Safe-mode test:** temporarily `"plugin": []` — if TUI works, hang is plugin/Bun/cache; if still black, check cwd (home vs project) and network.

### 9.7 Agent_runner note

Daemon/agent runs use `opencode run` with `--dir` on the **issue temp clone**. That path already avoids “home as project.” Packaging mistakes still break agents if the plugin never loads (defaults to non–oh-my agents / wrong names). Keep offline plugin seed correct even if you never open the TUI.

---

## 10. Related docs

| File | Purpose |
|------|---------|
| `README.md` | User-facing setup and architecture |
| `VERSION` | SemVer product version (`MAJOR.MINOR.PATCH`) |
| `web/` | Ops dashboard frontend (React) |
| `src/dashboard/` | Dashboard API and poll snapshot |
| `packaging/windows/README.md` | Offline zip design, versioning table, Windows pain points |
| `packaging/windows/versions.env` | Pinned OpenCode / oh-my-openagent / glab / Python / Node |
| `packaging/windows/collect-opencode-diag.bat` | User black-screen diagnostics bundle |
| `agent/AGENT_PROMPT.md` | Unified agent prompt kit (`§policy.commit`, `§role.*`) for target clones |
| `commitMsgFormat.md` | Pointer to kit commit policy for target product repos |
| `.env.example` | Environment template |
| `tests/test_logical_issues.py` | Known incorrect behaviours (expected fail until fixed) |
