# Yaver 0.7.0

Schedules can follow up on an existing GitLab MR: look up a saved
project or paste a URL, store a prompt, post it on the MR at fire
time, then run the usual note job. Notes written from the dashboard
are marked so the webhook does not start a second job.

Azure webhook, REST, Settings Test, git, and PR workflow steps now
write `[azure]` lines (never a PAT or Authorization header). A
rejected comment says why — token header, configured vs extracted
mentions, empty body, bot reply. Lines that arrive before `job_id`
exists are copied onto the job, so Job → Logs shows intake.

Dashboard first paint stays on the ops card. Slow or failed
`/api/meta` shows Retry instead of a blank Loading, and that
endpoint no longer waits on blocking Jira or GitLab work.

`plan_execute` with no plan file comments on Jira and stays at
`plan_ready`. Pasted git hosts keep a non-default port so on-prem
PATs match Settings. A last-turn pending `question` tool leaves
compact-wait for the unattended nudge.

Azure auth is still HTTP Basic `pat:<PAT>` (Settings Test, clone,
push, PR). Mention the bot on a PR comment; Yaver replies on the
PR. Completed or abandoned PRs delete the matching temp clone.

Settings has an Azure tab: host PATs, bot username, webhook enable,
and webhook secret. URL: `POST /webhooks/azure` with `X-Azure-Token`.

Standalone exe zips still ship **one** OpenCode kit: `opencoderman/agents`
(**derman-build** and **derman-plan** only — not gitlab-reviewer) and
`opencoderman/skills`. There is no second `opencode_configs/` tree.
Windows zips include `install-opencode-agents.bat`; Linux zips include
`install-opencode-agents.sh`. That script copies only those two agents
plus skills into the OpenCode home.

Plan-refactor reads every Jira comment page, so a late `@bot`
mention is not missed on busy tickets.

The exact OpenCoderman submodule commit is in `opencoderman.pin` and
`opencoderman-<sha>.zip` on this release.

Changelog: see `CHANGELOG.md` in the source tree.

## What to download

### Standalone executables (no host Python)

| File | Platform |
|------|----------|
| `yaver-windows-x64-*.zip` | Windows x64 |
| `yaver-linux-x64-*.zip` or `.tar.gz` | Linux x64 |

Each archive is an **onedir** folder:

- `yaver.exe` / `yaver` — CLI + daemon
- `_internal/` — bundled Python runtime, SPA, prompts
- `opencoderman/agents` + `opencoderman/skills` — derman-build, derman-plan, skills
- `install-opencode-agents.bat` (Windows) or `install-opencode-agents.sh` (Linux)
- `.env.example`, `START_HERE.txt`, `VERSION`

```text
copy .env.example .env     # Windows
cp .env.example .env       # Linux
# edit .env  (JIRA_HOST, JIRA_API_TOKEN, JIRA_BOARD_ID, …)
yaver.exe start            # Windows
./yaver start              # Linux
# open http://127.0.0.1:8080
```

Then, if OpenCode is already installed:

```text
install-opencode-agents.bat    # Windows
./install-opencode-agents.sh   # Linux
```

OpenCode and Codex are **not** inside these binaries.

### Full offline installers (Python + OpenCode + Codex vendor)

| File | Platform |
|------|----------|
| `virtual_developer-windows-x64-*.zip` | Windows |
| `virtual_developer-linux-x64-*.zip` / `.tar.gz` | Linux |

Extract, run `install-dashboard` then `install-backends` (and `install-codex` if needed), edit `.env`, start with `start-backend` / `start.bat`.

### OpenCoderman snapshot

Each release also attaches `opencoderman-<sha>.zip`.

## Highlights

- Scheduled follow-up on an existing GitLab MR
- Azure `[azure]` job trail (webhook reject reasons, REST, git, workflow)
- Webhook intake lines appear on Job → Logs
- Ops UI opens on the console card; `/api/meta` does not block on Jira/GitLab
- `plan_execute` with no plan file comments on Jira
- Pasted git hosts keep `:8080` / `:8929` for PAT maps
- Last-turn `question` tool leaves compact-wait for the unattended nudge
- Azure DevOps Server 2022.2 webhook intake (`POST /webhooks/azure`)
- TFS clone, push, and PR use Basic `pat:<PAT>` (same as Settings Test)
- Exe zip ships one `opencoderman/` tree (`derman-build`, `derman-plan`, `skills/` only)

## Config (secrets stay out of the binary)

Copy `.env.example` next to the executable. Minimum:

```env
JIRA_HOST=https://your-jira.example.com
JIRA_API_TOKEN=your-api-token-here
JIRA_BOARD_ID=1
TRIGGER_ASSIGNEE_NAMES=your-jira-display-name
```

Durable data: `YAVER_DATA_DIR` (`C:\vd\yaver` / `/vd/yaver`). Temp clones: `TEMP_DIR_BASE` (`C:\vd\t` / `/vd/t`).
