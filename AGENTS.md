# AGENTS.md — Virtual Developer

Instructions for humans and AI agents working on **this** repository (`virtual_developer`).

---

## 1. Product snapshot

JIRA Virtual Developer is a Python daemon that:

1. Discovers issues (board poller; webhooks optional)
2. Routes work (plan / direct execution / oracle)
3. Runs Oh My OpenAgent / OpenCode in isolated temp git clones
4. Posts progress, plans, errors, reviews, and completion back to Jira
5. Pushes feature branches and opens merge requests

**Default development branch:** `develop` (not `main`).  
**Integration branch for releases / stable:** `main`.

---

## 2. Coding standards

### Language & layout

- Python 3.12+, type hints on public APIs where practical.
- Package root: `src/`. Entry points: `cli.py`, `src/daemon.py`.
- Prefer small, focused modules; avoid shared mutable singletons across concurrent issues (`JobProcessor.git_manager` / `agent_runner` must not be overwritten mid-flight for parallel jobs when you touch concurrency).
- Do not log secrets (Jira tokens, GitLab PATs). Never commit `.env`.

### State & workflows

- Task statuses: `pending` → `planning` | `executing` → (`plan_ready`) → `code_review` → `completed` | `error` | `cancelled`.
- **Never** restart work that is in-flight (`planning` / `executing` / `code_review`) from poll noise.
- Terminal reprocess only when the user moves the issue back to **To Do** (or an explicit rework signal).
- Failures must set `ERROR` **and** notify Jira (`_fail_issue` / `post_error`). Stuck in-flight jobs are watchdogged in the daemon.
- `update_state(metadata={...})` **merges** metadata; never wipe unrelated keys.

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
- Auth header: **`Authorization: Bearer {JIRA_API_TOKEN}`** only.
- No username/password, no Basic auth, no Cloud-only `accountId` assumptions for core bot auth.
- TLS verify is currently off for on-prem certs; do not “fix” that without an explicit secure path.

### Behaviour expectations

- Comments use plain string bodies (Server/DC style); ADF is fallback only on 400.
- Report **errors**, **stuck states**, **retries**, and **completion** via Jira comments.
- Poller focuses on board/sprint + To Do + trigger labels / bot assignee.
- Webhook is optional; do not assume comments are ingested unless webhooks (or comment polling) are enabled.

### Config checklist (common)

| Variable | Role |
|----------|------|
| `JIRA_HOST` | Base URL |
| `JIRA_API_TOKEN` | Bearer token |
| `JIRA_PROJECTS` | Project filter (webhook path) |
| `JIRA_BOARD_ID` | Sprint poller board |
| `ENABLE_POLLING` / `ENABLE_WEBHOOK` | Intake mode (daemon may hardcode poller today — keep flags honest when you touch daemon) |

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

---

## 8. Do / Don’t checklist for agents

**Do**

- Branch from latest `develop`.
- Keep changes scoped; add tests for behaviour you change.
- Use conventional `type(scope): summary` for every commit and MR.
- Keep Jira user-visible on failure/stuck states.
- Preserve on-prem Bearer auth only.

**Don’t**

- Commit secrets or coverage artifacts.
- Merge to `main` for routine work.
- Leave jobs stuck without Jira notification.
- Use merge titles like “Merged from feature/…”.
- Reintroduce Jira username/basic auth without an explicit product decision.

---

## 9. Related docs

| File | Purpose |
|------|---------|
| `README.md` | User-facing setup and architecture |
| `commitMsgFormat.md` | Agent commit/branch rules in **target** project clones |
| `.env.example` | Environment template |
| `tests/test_logical_issues.py` | Known incorrect behaviours (expected fail until fixed) |
