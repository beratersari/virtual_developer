# Yaver 0.9.1

Schedule can follow up on an existing Azure DevOps PR the same
way as a GitLab MR.

Replies (usage notes, OpenCode results, errors) stay in the
triggering MR/PR thread.

Azure PR create no longer double-encodes project names with
spaces. Job-detail commit links use
`/commit/{sha}?refName=refs/heads/{branch}`. Storage shows
Azure PR status and does not spam `Folder not found` for
stale clone names.

# Yaver 0.9.0

GitLab and Azure comment jobs start only on `@mention /execute`.

A mention without `/execute` gets a usage note in that same
MR discussion or PR thread. Yaver does not open a new post.
`@mention /ask` is still a silent handoff to the other agent.
Comments from the bot user are ignored (no usage note).

Example: `@yaver /execute fix the login bug`

Webhook URLs are `POST /yaver/webhook/gitlab` and
`POST /yaver/webhook/azure`. The previous `/webhooks/gitlab` and
`/webhooks/azure` paths still work.

# Yaver 0.8.1

Azure webhooks have no secret and no password.
`POST /webhooks/azure` does not check `X-Azure-Token`.
Leftover `AZURE_WEBHOOK_SECRET` is ignored.

Settings Test is success when
`https://<server>/tfs/_apis/connectionData` is 200. A 404 on the
project list or an auth/login page is not a failed PAT.

# Yaver 0.8.0

One trigger list per provider: `JIRA_TRIGGER_USER`,
`GITLAB_TRIGGER_USER`, `AZURE_TRIGGER_USER` (comma-separated names,
no `@`). Leftover `TRIGGER_ASSIGNEE_NAMES` / `GITLAB_BOT_MENTIONS` /
`AZURE_BOT_MENTIONS` still load when the new key is empty.

Azure Settings Test matches Creasy 0.9.1: authenticate at
`https://<server>/tfs/_apis/connectionData`. A collection-scoped
`connectionData` call is 400 on TFS. Host can be `tfs.example.com`
or `tfs.example.com/tfs`. Clone still uses the collection on the
git URL from the webhook.

`@bot /ask …` on a GitLab MR or Azure PR comment is not a Yaver job.
That command is routed to another agent. The webhook returns
`ignored /ask handoff` and does not enqueue. `/asking` and
`/ask-review` still start a job. Merge and PR lifecycle hooks are
unchanged.

Schedules can still follow up on an existing GitLab MR: look up a
saved project or paste a URL, store a prompt, post it on the MR at
fire time, then run the usual note job.

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

- Azure webhook has no secret or password
- Settings Test stays OK if TFS identity is 200 and a later page is 404
- `JIRA_TRIGGER_USER` / `GITLAB_TRIGGER_USER` / `AZURE_TRIGGER_USER`
- Azure Settings Test uses Creasy 0.9.1 `/tfs` identity (not `/tfs/<Collection>`)
- `@bot /ask` on GitLab or Azure comments is ignored (another agent)
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
JIRA_TRIGGER_USER=your-jira-display-name
```

Durable data: `YAVER_DATA_DIR` (`C:\vd\yaver` / `/vd/yaver`). Temp clones: `TEMP_DIR_BASE` (`C:\vd\t` / `/vd/t`).
