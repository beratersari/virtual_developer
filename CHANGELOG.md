# Changelog

All notable changes to Yaver are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [SemVer](https://semver.org/) from the repo root `VERSION` file.
GitHub Releases are cut from tags `vMAJOR.MINOR.PATCH`.

## [Unreleased]

## [0.9.0] — 2026-09-10

GitLab and Azure comment jobs start only on `@mention /execute`.

### Changed

- GitLab and Azure comment jobs start only on `@mention /execute`. A mention without `/execute` gets a usage note in the same thread (not a new post). `@mention /ask` is still a silent handoff to the other agent. Comments from the bot user are ignored (no usage note). Replies stay in the existing MR discussion / PR thread.

[0.9.0]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.0

## [0.8.1] — 2026-09-11

Azure webhooks have no secret. Settings Test stays green after TFS identity even if a later page is 404.

### Removed

- Azure webhook secret and password. `POST /webhooks/azure` has no token check. Leftover `AZURE_WEBHOOK_SECRET` is ignored.

### Fixed

- Azure Settings Test succeeds when identity (`/tfs/_apis/connectionData`) is 200 even if the project list or an auth page returns 404.

[0.8.1]: https://github.com/beratersari/virtual_developer/releases/tag/v0.8.1

## [0.8.0] — 2026-09-10

One trigger list per provider. Azure Settings Test uses Creasy 0.9.1 TFS identity (`/tfs`, not `/tfs/<Collection>`).

### Changed

- One trigger list per provider: `JIRA_TRIGGER_USER`, `GITLAB_TRIGGER_USER`, `AZURE_TRIGGER_USER` (comma-separated names, no `@`). Leftover `TRIGGER_ASSIGNEE_NAMES` / `GITLAB_BOT_MENTIONS` / `AZURE_BOT_MENTIONS` still load when the new key is empty.

### Fixed

- Azure Settings Test follows Creasy 0.9.1: identity is `https://<server>/tfs/_apis/connectionData`. Collection-scoped `connectionData` is 400 on TFS. Hostname-only Test tries the host then `/tfs`. Clone still uses the webhook collection.

[0.8.0]: https://github.com/beratersari/virtual_developer/releases/tag/v0.8.0

## [0.7.1] — 2026-09-10

`@bot /ask` on a GitLab or Azure comment is not a Yaver job.

### Changed

- GitLab and Azure webhooks ignore a comment that contains `@mention_name /ask` (any spacing, HTML mention chip included). That command is for another agent. `/asking` and `/ask-review` still start a job. Merge/PR lifecycle hooks are unchanged.

[0.7.1]: https://github.com/beratersari/virtual_developer/releases/tag/v0.7.1

## [0.7.0] — 2026-09-10

Scheduled follow-up on an existing GitLab MR. Azure jobs write a readable `[azure]` trail, including webhook rejects, and those lines show on Job → Logs.

### Added

- Schedules can look up a saved project or pasted GitLab URL, store a prompt, post it on the MR at fire time, and run the usual note job. Dashboard-written notes are marked so the webhook does not re-fire.
- Azure webhook, REST, Settings Test, git PAT/PR, and PR workflow steps write `[azure]` lines (no PAT or Authorization). Rejects include token header, configured vs extracted mentions, and a comment preview.
- Webhook / enqueue lines that land before `job_id` exists are copied onto the job so Job → Logs shows intake, not only the agent run.

### Fixed

- First paint stays on the ops card. Slow or failed `/api/meta` shows Retry instead of a blank Loading. `/api/meta` no longer waits on blocking Jira or GitLab work.
- `plan_execute` with no plan file comments on Jira, keeps `plan_ready`, and unlatches the poller.
- Pasted git hosts keep a non-default port (`:8080` / `:8929`) so on-prem GitLab and Azure PATs match Settings.
- A last-turn pending `question` tool leaves compact-wait for the unattended nudge instead of burning the budget.

[0.7.0]: https://github.com/beratersari/virtual_developer/releases/tag/v0.7.0

## [0.6.1] — 2026-09-09

Azure DevOps Server auth matches Creasy and GitLab: username + PAT as HTTP Basic.

### Fixed

- Settings Test, clone, push, and PR create all send HTTP Basic `pat:<PAT>`. IIS rejects an empty username (`:PAT`) and ignores URL userinfo when it advertises Windows Negotiate — that was the Settings Test 401 with a valid PAT. Askpass returns username `pat`. Test continues past a 401 on the host root and tries `/tfs/DefaultCollection`.

## [0.6.0] — 2026-09-09

Azure DevOps Server 2022.2 webhooks work like GitLab MR comments.

### Added

- Azure DevOps Server 2022.2 (TFS) project webhooks (`POST /webhooks/azure`) with the same usage as GitLab: `@mention` a PR comment to start a job, Yaver replies on the PR, completed/abandoned PRs delete the temp clone. Clone and push use a host Azure PAT only (empty username, no credential prompt).
- Settings → Azure: host PATs, bot username, webhook enable, and webhook secret. The ops SPA includes the Azure tab.

## [0.5.3] — 2026-09-09

Settings follows a newer `.env`, and Edge no longer pops a native login dialog.

### Fixed

- Jira → Bot name in Settings showed leftover `.env.example` names after you edited `TRIGGER_ASSIGNEE_NAMES`. Save now writes only changed fields. After restart, a `.env` key is used unless you later save that same field in Settings.
- Some Edge windows showed a Windows/HTTP username popup that rejected `DASHBOARD_USERNAME` / `DASHBOARD_PASSWORD`. The first SPA probe is always 200; sign-in stays on the in-page Ops console form.

[0.6.1]: https://github.com/beratersari/virtual_developer/releases/tag/v0.6.1
[0.6.0]: https://github.com/beratersari/virtual_developer/releases/tag/v0.6.0
[0.5.3]: https://github.com/beratersari/virtual_developer/releases/tag/v0.5.3

## [0.5.2] — 2026-09-09

Exe zip `opencoderman/agents` is only **derman-build** and **derman-plan**. The copy scripts do not install `gitlab-reviewer`.

[0.5.2]: https://github.com/beratersari/virtual_developer/releases/tag/v0.5.2

## [0.5.1] — 2026-09-09

Standalone exe zips ship one OpenCode kit. Plan-refactor reads every Jira comment page.

### Changed

- Exe zip `opencoderman/` contains only `agents/` and `skills/`. The duplicate `opencode_configs/` tree is gone. Windows ships `install-opencode-agents.bat`; Linux ships `install-opencode-agents.sh`.

### Added

- Optional dashboard login: `DASHBOARD_USERNAME` and `DASHBOARD_PASSWORD` at the top of `.env`. Empty = no login. The board poller and `POST /webhooks/gitlab` stay on their own paths.

### Fixed

- `plan_refactor` missed the newest `@bot` comment when Jira returned only the first comment page.
- Dashboard buttons (including Sign out) respond to a single click again. Storage no longer stacks `/api/storage` polls, Sign out leaves the session immediately, and logout no longer waits behind GitLab/storage work.

[0.5.1]: https://github.com/beratersari/virtual_developer/releases/tag/v0.5.1

## [0.5.0] — 2026-09-08

Settings no longer have a second GitLab allowlist or a dead assignment switch. Storage deletes clones when GitLab says the MR is merged.

### Changed

- GitLab auth is one setting: `GITLAB_HOST_PATS` (host + PAT). A host with a PAT is allowed. `GITLAB_ALLOWED_HOSTS` is leftover only (expands a lone `GITLAB_PAT` when the map is empty).
- Jobs use the model's advertised context window. `OPENCODE_CONTEXT_LIMIT` defaults to `0` (no workspace override).
- `JIRA_EMAIL` is optional Cloud Basic only (bottom of `.env.example`). Daily auth is host + token (Bearer).

### Fixed

- Merged or closed GitLab MRs delete the temp clone again. Session binds no longer count as in-use, and the issue key is enough to find the folder.
- Storage folders that already show an MR (`!N`) are deleted when that MR is merged or closed, even if GitLab.com cannot reach the webhook. The poller asks GitLab for the same MR Storage displays.
- Storage shows live GitLab MR status next to each `!N`.

### Removed

- `TRIGGER_ON_ASSIGNMENT`. Poller intake is always To Do + bot assignee.
- Unused `.env.example` keys `ASELIXAI_API_KEY` and `PROJECT_ROOT`.
- `GIT_USER_NAME` / `GIT_USER_EMAIL`. Yaver does not commit; the agent uses the machine git identity.

[0.5.0]: https://github.com/beratersari/virtual_developer/releases/tag/v0.5.0

## [0.4.0] — 2026-09-08

Jira intake is poller-only. Settings group fields by tab, and each bot has one name.

### Added

- Standalone exe zips include the `opencoderman/` tree plus `install-opencode-agents.bat` / `.sh`. The script finds the OpenCode home and copies `derman-build`, `derman-plan`, and `skills/` only.

### Changed

- Settings tabs: Jira, GitLab, Projects, Agent, Runtime.
- GitLab trigger username is `GITLAB_BOT_MENTIONS` only.
- Jira assignee and @mention use `TRIGGER_ASSIGNEE_NAMES` only.

### Removed

- Jira webhook intake (`JIRA_INTAKE_MODE`, `JIRA_WEBHOOK_SECRET`, `POST /webhooks/jira`). The board poller is the only Jira intake. GitLab project webhooks are unchanged.

[0.4.0]: https://github.com/beratersari/virtual_developer/releases/tag/v0.4.0

## [0.3.0] — 2026-09-08

Plan → build is now label-driven, plan and build keep separate OpenCode sessions, and the ops dashboard is cheaper to live-update.

### Added

- Same-ticket implement: rename `plan_ready` → `plan_execute` while the ticket is **In Progress** (Mode in `{params}` can stay `plan`).
- Plan revise: remove `plan_ready`, add `plan_refactor`, comment tagging the bot. The **plan** session is resumed.
- Separate OpenCode session maps for plan vs build (same repo + source + target). Dashboard **Sessions** lists them in two sections.
- A new `Mode: build` ticket implements the durable plan when one exists (this key, or the plan-session ticket for the same repo/branches).
- Plans are posted as Jira **comments** (Cloud ADF when possible), never appended to the description.

### Changed

- Poller intake is To Do + bot assignee (`TRIGGER_ASSIGNEE_NAMES`) only. `TRIGGER_LABELS` and `ai-start-work` / `ai-execute` / `ai-plan-ready` are gone.
- Same-ticket `Mode: build` no longer starts implementation after a plan.
- Plan files live at `{YAVER_DATA_DIR}/plans/{ISSUE_KEY}.md` (not inside the git clone).
- Dashboard WebSocket ticks send poll/meta/queue only. Jobs/Sessions refetch when live issues or the queue change, not on every 5s countdown.
- OpenCoderman `derman-plan` is git-read-only and ends with `PLAN_DONE`. `derman-build` cannot push; the named plan is the spec.

### Fixed

- Operator comments that @mention the PAT user are accepted for `plan_refactor` even when the token is the same Cloud account.
- Numbered plan recaps are not treated as multiple-choice questions. Plan-job nudges do not say “implement”.

[0.3.0]: https://github.com/beratersari/virtual_developer/releases/tag/v0.3.0

## [0.2.4] — 2026-09-07

### Added

- Standalone exe zip now includes `opencode_configs/` next to `yaver.exe` / `yaver`: OpenCoderman `agents/` (`derman-build`, `derman-plan`) and the full `skills/` tree, so operators can copy them into `~/.opencode` without the submodule.

### Fixed

- Frozen `yaver.exe` clone askpass no longer execs the Click CLI (`Usage: yaver.exe --help` / `No such command …\\vd-git-askpass`). The helper is a self-contained `.cmd` / `.sh` that prints `VD_GIT_PASSWORD`.

[0.2.4]: https://github.com/beratersari/virtual_developer/releases/tag/v0.2.4

## [0.2.3] — 2026-09-06

### Added

- OpenCoderman **`028c79c`**: 30 skills outside C++ (TypeScript, Kotlin, Swift, PHP, Ruby, Dart, Scala, Elixir, PowerShell, Lua, R, React, Vue, Node, Next.js, Android, iOS, Django, Spring, Rails, PostgreSQL, MongoDB, Redis, AWS, HTML/CSS, ML, protobuf, WebSocket, OAuth/OIDC, Linux). C++ remains one group among many.

[0.2.3]: https://github.com/beratersari/virtual_developer/releases/tag/v0.2.3

## [0.2.2] — 2026-09-06

### Added

- Every release records the exact OpenCoderman submodule commit in `opencoderman.pin` (exe zip + offline installers) and attaches `opencoderman-<sha>.zip` so a later submodule bump cannot rewrite what that tag shipped.

[0.2.2]: https://github.com/beratersari/virtual_developer/releases/tag/v0.2.2

## [0.2.1] — 2026-09-06

### Fixed

- Frozen `yaver.exe` crashed on startup as soon as a `.env` was present: a debug log used `≈`, which Windows cp1252 cannot encode. The process died before the ops dashboard bound :8080. Logger now never raises on console encoding, and a no-argument / double-click launch starts the daemon.

[0.2.1]: https://github.com/beratersari/virtual_developer/releases/tag/v0.2.1

## [0.2.0] — 2026-09-06

First tagged product release. Builds from `develop` (`v0.2.0`).

### Added

- Standalone **Windows** (`yaver.exe`) and **Linux** (`yaver`) executables via PyInstaller onedir, built in CI (`.github/workflows/executables.yml`). Operator config is `.env` next to the binary (copy from `.env.example`).
- OpenCoderman submodule: unattended **derman-build** / **derman-plan** agents (not stock OpenCode `build` / `plan`).
- Codex worker (`AGENT_BACKEND=codex`) with the same job contract as OpenCode.
- Ops dashboard (FastAPI + React): jobs, poll monitor, settings, storage, live chat, schedules, issue report zip.
- Jira **webhook** intake (`JIRA_INTAKE_MODE=webhook`) in addition to board poll.
- GitLab MR comment mentions as queued builds; delete the temp clone when an MR is merged or closed.
- Job search (issue key, title, description), storage MR link/status, schedule param picker.
- Assign handled Jira issues to the PAT user.

### Fixed

- Do not open an empty MR after an agent ERROR; keep the job in ERROR.
- Flatten Jira Cloud ADF so `{params}` parse on Cloud issues.
- Do not retry unregistered OpenCode agents.
- Push existing commits after an agent-session error; recover skipped schedules and stale Continue todos.
- OpenCode serve: compact wait, one unattended nudge, last-turn-only clarifying questions, abort compact-loop then Continue the same session.
- Cancel kills agent children immediately; unattended PAT clone; GitLab PAT used for clone/push.

### Packaging

- Windows and Linux offline zips still ship `install-dashboard` + `install-backends` + `install-codex` (Python wheels, OpenCode, Codex). Standalone executables do **not** replace those zips.
- Offline zips include `.env.example`.
- Standalone zip includes `.env.example`, `versions.env`, `START_HERE.txt`, and `VERSION`.

### Downloads (this release)

| Asset | What it is |
|-------|------------|
| `yaver-windows-x64-0.2.0.zip` | Frozen `yaver.exe` + `_internal/` + config templates |
| `yaver-linux-x64-0.2.0.zip` / `.tar.gz` | Frozen `yaver` + `_internal/` + config templates |
| `virtual_developer-windows-x64-0.2.0.zip` | Full Windows offline installer (needs host Python) |
| `virtual_developer-linux-x64-0.2.0.zip` / `.tar.gz` | Full Linux offline installer (needs host Python) |
| Source code (zip / tar.gz) | Git tree at tag `v0.2.0` (added by GitHub) |

[0.2.0]: https://github.com/beratersari/virtual_developer/releases/tag/v0.2.0
