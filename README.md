# Yaver

<p align="center">
  <img src="web/public/yaver-logo.png" alt="Yaver — Sanal Geliştirici" width="220" />
</p>

**English** · [Türkçe](README.tr.md)

**Version:** see root [`VERSION`](VERSION)

**Yaver** (*the aide*) is a Python daemon that runs **OpenCode** agents (OpenCoderman **derman-plan** / **derman-build** / **derman-test**) for work that arrives from **Jira**, **GitLab**, or **Azure DevOps Server**. It clones the repo into an isolated temp folder, posts progress back on the ticket or review thread, and can push a work branch and open a merge/pull request.

Four ways work starts:

| Intake | How it starts | After accept | After the job finishes |
|--------|---------------|--------------|------------------------|
| **Jira board** | Poller: To Do-like + bot assignee | Board → **In Progress** | Stays In Progress. Move it back to **To Do** to run again. |
| **GitLab MR comment** | `@bot /yaver …` on a merge request | No board move | Reply on the same thread. Merged/closed MR deletes the temp clone, the durable plan named in the MR title, and that key's local state (even if the Jira ticket is still `plan_ready`). Job history stays. |
| **Azure PR comment** | `@bot /yaver …` on a pull request | No board move | Reply on the same thread. Completed/abandoned PR deletes the temp clone, the durable plan named in the PR title, and that key's local state. Job history stays. |
| **Azure Boards work item** | Assign the bot on **To Do** / **In Progress** (or New / Active / Doing, …) | State → **In Progress** category (**Active** / **Doing** / **Committed** / **In Progress**) | Stays there. Yaver does **not** move it to Resolved or Done. |

Same `Repository` + `Source branch` + `Target branch` + kind (`plan` vs `build`) resume the existing OpenCode session. Concurrency follows `MAX_CONCURRENT_JOBS`.

---

## What it does

1. **Discovers** work (Jira poller, GitLab webhook, Azure webhook).
2. **Routes** from a per-issue `{params}` block (`Mode: plan`, `Mode: build`, or `Mode: test`; default is build).
3. **Runs** OpenCode in a temp clone.
4. **Reports** plans, progress, errors, and completion on the ticket or review thread.
5. **Pushes** the work branch and opens an MR/PR when a build succeeds (orchestrator owns push + MR; the model should not push).
6. **Serves** an ops dashboard (tasks, poll monitor, storage, safe settings) in the same process.

---

## Architecture

```text
 Jira board poller          GitLab project hook           Azure service hook
 To Do + bot assignee       @bot /yaver on an MR          @bot /yaver on a PR
                                                          assign work item
              \                    |                    /
               \                   |                   /
                ▼                  ▼                  ▼
        ┌──────────────── Yaver (one process) ─────────────────┐
        │  Intake  →  Job processor  →  OpenCode serve (--dir) │
        │     temp clone from {params}                         │
        │     comments on Jira / MR / PR / work item           │
        │     git push + GitLab MR or Azure PR (build)         │
        │  Ops dashboard  ·  stuck-job monitor  ·  JSON state  │
        └──────────────────────────────────────────────────────┘
```

---

## How work starts (all intakes)

Every job still needs a **`{params}`** block so Yaver knows the git remote and branches (see [Issue template](#issue-template-params)). Review-thread jobs (`/yaver` on an MR/PR) can reuse the repo and branches from that MR/PR when `{params}` is missing on the bound ticket.

### After accept vs after complete

| | Jira | Azure work item | GitLab MR / Azure PR |
|--|------|-----------------|----------------------|
| **Accept** | Transition toward **In Progress** (best-effort) and assign the PAT user when configured | Set `System.State` to the type’s **InProgress** name: Agile/CMMI **Active**, Basic **Doing**, Scrum **Committed**, or **In Progress** | No board change |
| **Job done** (plan or build) | Stays **In Progress**. Comment only. | Stays **Active / Doing / In Progress**. **Not** Resolved or Done. | Reply on the thread |
| **Run again** | Move the ticket back to **To Do** (still assigned to the bot) | Assign again while it is To Do / In Progress (or New / Active / Doing). Moving Active → New while still assigned does **not** re-queue. After an error, edit the title/description. | New `@bot /yaver …` comment |
| **In-flight** | Never restarted from poll or webhook noise | Same | Same |

---

## 1. Jira board (poller)

This is the only Jira intake. There is no Jira comment webhook for starting work.

### Accept

All of these:

- Issue is on the configured **board** (`JIRA_BOARD_ID`). Scrum: **first active sprint only**.
- Status looks like **To Do** (name or `statusCategory` new/backlog-like: To Do, New, Open, Backlog, Yapılacaklar, …).
- Assignee matches `JIRA_TRIGGER_USER`.
- If `JIRA_TRIGGER_LABEL` is set, the issue also needs one of those labels.
- Not already `planning` / `executing`.

**To Do + bot assignee = rework (intentional).** After `completed` / `error` / `cancelled`, leaving the ticket on To Do (or moving it back to To Do) starts another run. After accept, Yaver moves the board to **In Progress** so the next poll does not start a second job until you put it on To Do again.

`plan_ready` is the exception: **`Mode: plan` never implements by itself.** See [After a plan](#after-a-plan).

### Usage example — plan then implement on the same Jira ticket

1. Create `KAN-12` on the board.

```text
Summary: Plan login rate limit

{params}
Repository: https://gitlab.example.com/group/app.git
Source branch: feature/KAN-12
Target branch: develop
Mode: plan
{params}
```

2. Assign it to the bot (`JIRA_TRIGGER_USER`, e.g. `yaver`) and leave it on **To Do**.
3. Within one poll interval Yaver:
   - accepts the issue
   - moves it to **In Progress**
   - writes `{YAVER_DATA_DIR}/plans/KAN-12.md`
   - posts the plan as a **comment** (not the description)
   - sets label `plan_ready` and local status `plan_ready`
   - **stops**
4. To implement on the **same** ticket: while it is **In Progress**, rename label `plan_ready` → `plan_execute`.
5. Yaver starts a **build** session, implements that plan file, pushes `feature/KAN-12`, opens an MR, comments completion. The label becomes `plan_executed`. The Jira status stays **In Progress**.
6. You move the ticket to Done when you are satisfied.

### Usage example — rework a finished Jira ticket

1. `KAN-12` is `completed` and still **In Progress**.
2. Fix the description or leave it as-is.
3. Move the ticket back to **To Do** (still assigned to the bot).
4. Next poll re-queues a new run.

### Usage example — fail, fix, retry

1. Accept fails (bad `{params}`). Yaver still moves the ticket toward **In Progress** and posts an error comment.
2. Fix the description.
3. Move it back to **To Do**. Next poll retries.  
   Editing the text while it stays on To Do after `error` also retries (`text_changed_retry`).

### What not to do on Jira

- Do not start implement by changing `Mode: plan` to `Mode: build` on the **same** plan ticket. Same-ticket implement is **`plan_execute` + In Progress** only.
- Do not use Azure `/planExecute` / `/planRefactor` on Jira comments.
- Do not expect a second job while the ticket stays In Progress (except `plan_execute` / `plan_refactor`).

---

## 2. GitLab merge request comments

Register a **project** webhook (Comments + Merge request events) to:

`http://<yaver-host>:8080/yaver/webhook/gitlab`

Set `GITLAB_WEBHOOK_SECRET` to the same secret GitLab sends. Set `GITLAB_TRIGGER_USER` (comma-separated usernames, no `@`).

### Commands

| Comment | What happens |
|---------|----------------|
| `@yaver /yaver add tests for login` | Starts a job. Prompt is the rest of the comment. |
| `@yaver` (no `/yaver`) | Usage note in **that thread**. No job. |
| `@yaver /ask …` or `@yaver /review …` | Starts a code review (or a follow-up on `/ask`). Same rules as Creasy: `/review`, `/ask`, assign the bot as reviewer, or open an MR that already lists the bot. New commits do not re-review. No push or new MR. |

### Which ticket the job binds to

In order:

1. Jira key in the MR title that matches `JIRA_PROJECTS` (e.g. `feat(KAN-12): …`)
2. `WIT-…` in the title
3. `#42` (Azure work item) when a collection-scoped item exists
4. `Closes KAN-12` / similar
5. `WIT-…` or `#id` in the description
6. Existing local job with the same repo + source + target
7. Fallback key `GL-{PROJECT}-{iid}`

### Usage example

MR title: `feat(KAN-12): rate limit login`

```text
@yaver /yaver cover the new limiter with unit tests
```

Yaver clones with the host PAT, resumes the **build** session for that repo + branches when one exists, replies on the same discussion, and opens/updates the MR if the agent committed.

When the MR is **merged** or **closed**, Yaver deletes the matching temp clone, unlinks `{YAVER_DATA_DIR}/plans/{KEY}.md` for the key parsed from the title (`feat(KAN-12): …` → `KAN-12`), and drops that key's local issue state. Job JSON stays for Analytics. This is intentional even if the Jira ticket is still `plan_ready` or executing.

---

## 3. Azure DevOps pull request comments

Same idea as GitLab. Enable `AZURE_WEBHOOK_ENABLED=true`. On the project: Service hooks → Web Hooks:

- Events: **Pull request commented**, **updated**, **merged**, **abandoned**
- URL: `http://<yaver-host>:8080/yaver/webhook/azure`
- No webhook secret

Set `AZURE_TRIGGER_USER` and `AZURE_COLLECTION_PATS` (collection URL → PAT). Clone/push/PR use HTTP Basic `pat:<PAT>`.

### Commands (PR thread only)

| Comment | What happens |
|---------|----------------|
| `@yaver /yaver explain this diff` | Starts a job on the PR |
| `@yaver` (no `/yaver`) | Usage note in that thread |
| `@yaver /ask …` or `@yaver /review` | Yaver review / follow-up (no push). Assign the bot as reviewer, or open a PR that already lists it, to start a review. |

Do **not** use `/planExecute` / `/planRefactor` on a PR. Those are work-item comments only.

### Which ticket the job binds to

Same order as GitLab: Jira title, `WIT-…`, `#42` in this collection, Closes, description, repo/source/target, then `AZ-{PROJECT}-{id}`.

Dashboard local key for a work item is `WIT-BETA-42`. The agent **Ticket** / `{ISSUE_KEY}` line is the numeric id `42`.

### Usage example

PR title: `feat(KAN-12): login limiter` **or** `Fix #42`

```text
@yaver /yaver tighten the null check on line 40
```

Completed or abandoned PRs delete the matching temp clone. Storage warns when a clone has no linked MR/PR (it will not auto-delete).

---

## 4. Azure Boards work items (webhook)

Same Azure URL as PR comments. Add service hooks for **Work item created**, **updated**, and **commented**. Updated is only used for **Assigned To**. Comments use the commented hook. State, description, and tag edits do not start a job.

### Accept

- Assigned To matches `AZURE_TRIGGER_USER`
- State is **To Do** or **In Progress**, or the same process-template column:

| Column kind | Names that start a job |
|-------------|------------------------|
| To Do | **To Do**, **New**, **Proposed**, **Approved**, Open, Backlog, Yapılacak / Yapılacaklar |
| In Progress | **In Progress**, **Active**, **Doing**, **Committed**, WIP, Devam Ediyor |
| Not intake | **Resolved**, **Done**, Closed, Completed, Removed |

After accept Yaver:

1. Sets state to that type’s **In Progress** name (**Active** on Agile, **Doing** on Basic, **Committed** on Scrum, or **In Progress**)
2. Assigns the collection PAT user
3. Starts the job from `{params}`

**Unlike Jira**, moving Active → New (or In Progress → To Do) while still assigned does **not** re-queue. First sighting only. After `error`, assign the item again. Board moves, description, and tag edits do not retry. After a plan, use comments (below).

Yaver **never** moves a work item to Resolved or Done.

### After a plan (Azure work item only)

Do **not** use Jira labels `plan_ready` / `plan_execute` on Azure.

| Comment on the work item | What happens |
|--------------------------|----------------|
| `@yaver /planExecute` | Implement the waiting plan (item must already be `plan_ready`) |
| `@yaver /planRefactor tighten the API` | Revise the plan on the **plan** session, then `plan_ready` again |
| `@yaver` without those commands | Work-item usage note (not posted on PRs) |

### Usage example — assign a New bug, then implement

1. Work item **42** in project Beta, state **New** (or To Do / Active / Doing).

```text
{params}
Repository: https://tfs.example.com/tfs/DefaultCollection/Beta/_git/app
Source branch: feature/42
Target branch: develop
Mode: plan
{params}
```

2. Assign it to `yaver`.
3. Yaver accepts, moves it to **Active** (Agile) / **Doing** (Basic) / **In Progress**, writes the plan, comments, sets local `plan_ready`, **stops**. Board stays In Progress-like.
4. Comment:

```text
@yaver /planExecute
```

5. Yaver implements, pushes, opens a PR. The work item **stays Active / In Progress**. You close it when the PR is done.

### Usage example — direct build (no plan)

Set `Mode: build` in `{params}`, assign on New/To Do/Active. One build session; no `/planExecute` needed.

---

## After a plan

```text
Jira
  To Do + bot  →  Mode: plan  →  plan_ready + label plan_ready  →  In Progress, stop
       ├─ rename plan_ready → plan_execute (still In Progress)  →  build
       ├─ remove plan_ready, add plan_refactor, comment @bot   →  revise plan
       └─ new ticket, Mode: build, same repo/branches          →  own build session

Azure work item
  assign on To Do / In Progress (or New / Active / Doing)  →  plan_ready, stop
       ├─ comment @bot /planExecute                         →  build
       ├─ comment @bot /planRefactor <prompt>               →  revise plan
       └─ new work item, Mode: build, same repo/branches    →  own build session
```

Plan and build keep **separate** OpenCode sessions for the same repo + source + target.

Dashboard **Start** is disabled. Do not use `/planExecute` on Jira, GitLab, or Azure PR comments.

---

## Issue template (`{params}`)

Put this block in the Jira description or Azure work-item description:

```text
{params}
Repository: https://gitlab.example.com/group/your-repo
Source branch: feature/PROJ-123
Target branch: develop
Mode: plan
{params}
```

| Field | Meaning |
|-------|---------|
| **Repository** | Clone URL (aliases: Repo, GitLab, Project URL). Do not add a trailing `.git` on Azure `…/_git/…` URLs. |
| **Source branch** | Work / MR source branch. If missing or equal to a base name (`main`/`develop`), work branch becomes `feature/{ISSUE_KEY}` |
| **Target branch** | Must exist on remote; work is based on it; MR/PR merges **into** it |
| **Mode** | **`plan`** — plan only, no push. **`build`** — implement, push, open MR/PR. **`test`** — unit tests only |

Mode aliases: `planning`/`prometheus` → plan; `execute`/`execution`/`atlas`/`implement` → build.

Incomplete templates get a user-visible comment with the format help.

---

## Task statuses

```text
pending → planning | executing → (plan_ready) → completed | error | cancelled
```

| Status | Meaning |
|--------|---------|
| `planning` / `executing` | Agent running — never restarted from intake noise |
| `plan_ready` | Plan finished; **not** an error. Jira: `plan_execute`. Azure work item: `/planExecute` |
| `completed` | Delivered (or soft no-op). Jira: To Do to rework. Azure WIT: no auto re-queue |
| `error` | Failed; comment explains why. Jira: To Do or edit text. Azure WIT: edit text |
| `cancelled` | Operator cancel. Jira To Do + bot is still rework |

---

## Quick start

### Linux

See [packaging/linux/README.md](packaging/linux/README.md).

```bash
git submodule update --init --recursive
./install-dashboard.sh
./install-backends.sh
./install-codex.sh
# Edit .env — JIRA_HOST, JIRA_API_TOKEN, JIRA_BOARD_ID at minimum
./start-backend.sh        # API + SPA on :8080
```

Ops dashboard: **http://127.0.0.1:8080**  
OpenCode TUI: `./start-opencode.sh` from the project folder (never from `$HOME`).

### Windows (offline zip)

See [packaging/windows/README.md](packaging/windows/README.md).

```cmd
install-dashboard.bat
install-backends.bat
install-codex.bat
start.bat
```

Open the TUI only via **`start-opencode.bat`** from the project folder.

### Standalone executables

CI **Standalone Executables** freezes `yaver` / `yaver.exe` (onedir). Linux: download the Ubuntu 18.04 / 20.04 / 22.04 / 24.04 build that matches the host. OpenCode / Codex are **not** inside the binary. See [packaging/pyinstaller/](packaging/pyinstaller/README.md).

---

## Ops dashboard

Enabled with the daemon (`DASHBOARD_ENABLED=true`). Default bind in the offline zip is `0.0.0.0` (LAN). Loopback-only: `DASHBOARD_HOST=127.0.0.1`.

| | |
|--|--|
| URL | `http://127.0.0.1:8080` |
| Auth | Optional `DASHBOARD_USERNAME` + `DASHBOARD_PASSWORD`. Empty = no login. Does **not** apply to the Jira poller or the GitLab/Azure webhooks. |

**Frontend is display-only.** Filtering and poll math live on the backend.

Useful pages:

- **Tasks / Jobs** — live and past runs (prompts and logs are per selected job)
- **Poll** — last Jira board snapshot
- **Storage** — temp clones. Delete is refused while a job owns the clone. Merged GitLab MRs and completed/abandoned Azure PRs delete the matching folder. Clones with no linked MR/PR are warned (will not auto-delete).
- **Scheduled** — create a Jira issue or Azure work item later, or look up an existing one. **Cancel** is only for `scheduled` / `error` (`dispatching` cannot be cancelled).
- **Settings** — board id, poll interval, trigger names, Azure collection PATs (no token values shown)

Dashboard **Start** is disabled. **Cancel** kills agent children immediately.

```bash
cd web && npm install && npm run build
```

---

## Configuration

Copy [`.env.example`](.env.example) → `.env`. Never commit secrets.

### Jira

| Variable | Description |
|----------|-------------|
| `JIRA_HOST` | Base URL |
| `JIRA_API_TOKEN` | On-prem PAT or Cloud API token |
| `JIRA_EMAIL` | Cloud/dev only → HTTP Basic. Empty = Bearer PAT (prod) |
| `JIRA_PROJECTS` | Project keys: default create + parse keys from GitLab/Azure titles (`feat(KAN-12):`) |
| `JIRA_BOARD_ID` | Agile board to poll (**required** for Jira discovery) |
| `JIRA_TRIGGER_USER` | Assignee / mention names (comma-separated, no `@`) |
| `JIRA_TRIGGER_LABEL` | Optional. When set, To Do intake also needs one of these labels |

TLS verify is off for typical on-prem certs.

### GitLab

| Variable | Description |
|----------|-------------|
| `GITLAB_HOST_PATS` | JSON hostname → PAT |
| `GITLAB_TRIGGER_USER` | Usernames that start a job on `@name /yaver` |
| `GITLAB_WEBHOOK_SECRET` | Required for `POST /yaver/webhook/gitlab` |

### Azure DevOps

| Variable | Description |
|----------|-------------|
| `AZURE_COLLECTION_PATS` | JSON `https://host/tfs/Collection` or `https://host/Collection` → PAT |
| `AZURE_WEBHOOK_ENABLED` | Accept PR + work-item hooks on `/yaver/webhook/azure` (no secret) |
| `AZURE_TRIGGER_USER` | PR `@name /yaver` and work-item Assigned To |

### Agent / paths

| Variable | Default | Description |
|----------|---------|-------------|
| `POLL_INTERVAL_SECONDS` | `30` | Jira board poll |
| `MAX_CONCURRENT_JOBS` | `6` | Parallel agent jobs |
| `DEFAULT_MODEL` | (see `.env.example`) | Shared by OpenCode and Codex |
| `DEFAULT_AGENT` | `derman-build` | Build jobs |
| `DEFAULT_PLAN_AGENT` | `derman-plan` | Plan jobs |
| `DEFAULT_TEST_AGENT` | `derman-test` | Test jobs |
| `AGENT_TASK_TIMEOUT_SECONDS` | `1800` | Per-attempt wall clock |
| `YAVER_BASE_DIR` | Windows `%LOCALAPPDATA%\Yaver`; Linux `~/.local/share/yaver` | One folder the user can write. Data is `{base}/yaver`. Clones are `{base}/t`. |
| `TEMP_CLONE_MAX_AGE_DAYS` | `7` | Hourly delete of unused clones older than this. Live jobs are never removed. `0` = off. Session bind is kept so a later mention reclones and resumes. |

```bash
python cli.py models
python cli.py models --set provider/model-id
```

---

## CLI

```bash
python cli.py --help
python cli.py --version
python cli.py init
python cli.py start
python cli.py process PROJ-123
python cli.py process PROJ-123 --dry-run
python cli.py status
python cli.py show PROJ-123
python cli.py cancel PROJ-123
python cli.py costs
```

---

## Project layout

```text
virtual_developer/
├── cli.py
├── VERSION
├── README.md              # this file (English)
├── README.tr.md           # Turkish operator guide
├── agent/PLAN_PROMPT.md
├── agent/BUILD_PROMPT.md
├── opencoderman/          # derman-build / derman-plan / derman-test
├── src/                   # daemon, processor, jira, gitlab, azure, dashboard
├── web/                   # ops SPA
├── packaging/             # Windows / Linux / PyInstaller
└── AGENTS.md              # contributor rules
```

---

## Testing

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt pytest pytest-asyncio pytest-cov
.venv/bin/python -m pytest tests/ --ignore=tests/test_logical_issues.py -q
```

---

## Git flow (this repository)

Default branch is **`develop`**. Feature MRs go into `develop`. Release: tag `vMAJOR.MINOR.PATCH` and promote `develop` → `main`.

---

## Troubleshooting

| Symptom | What to check |
|---------|----------------|
| Jira poller idle | `JIRA_BOARD_ID`, To Do, bot assignee, `python cli.py process KEY` |
| Jira To Do + bot but nothing happens | `plan_ready` → use `plan_execute`. `completed`/`error`/`cancelled` on To Do **is** rework — check logs |
| Azure assign does nothing | State must be To Do / In Progress / New / Active / Doing (not Resolved/Done). Webhook enabled? Assigned To matches `AZURE_TRIGGER_USER`? |
| Azure plan never implements | Comment `@bot /planExecute` on the **work item**, not the PR |
| MR/PR mention does nothing | Need `@bot /yaver …`. Bare mention only posts a usage note |
| 401 / 403 Jira | Token; Cloud needs `JIRA_EMAIL` |
| Git / MR fails | `{params}` complete; host PAT in `GITLAB_HOST_PATS` or `AZURE_COLLECTION_PATS` |
| Dashboard down | Daemon up? `http://127.0.0.1:8080` |
| Windows TUI black screen | `start-opencode.bat` from the project folder |

```bash
python cli.py config
python cli.py show PROJ-123
```

---

## Security notes

1. Keep **`.env`** out of git.
2. Dashboard login is optional; lock it down if the host is not on a trusted network.
3. A GitLab/Azure PAT is only sent to the host/collection it is stored for.
4. Prefer a dedicated bot account with least privilege.
5. Never log raw tokens.

---

## Related documentation

| Doc | Purpose |
|-----|---------|
| [README.tr.md](README.tr.md) | Turkish operator guide |
| [AGENTS.md](AGENTS.md) | Contributor / AI rules |
| [opencoderman/agents/derman-plan.md](opencoderman/agents/derman-plan.md) | Unattended planner |
| [opencoderman/agents/derman-build.md](opencoderman/agents/derman-build.md) | Unattended implementer |
| [packaging/windows/README.md](packaging/windows/README.md) | Offline Windows zip |
| [packaging/linux/README.md](packaging/linux/README.md) | Linux install |
| [`.env.example`](.env.example) | Full environment template |

---

## License

MIT
