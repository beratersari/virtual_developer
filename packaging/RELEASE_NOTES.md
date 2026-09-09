# Yaver 0.6.0

Azure DevOps Server 2022.2 (TFS) project webhooks work like GitLab MR
comments. Mention the bot on a pull-request comment; Yaver clones with
the host Azure PAT (no username/password prompt), runs the job, and
replies on the PR. Completed or abandoned PRs delete the matching temp
clone.

Settings has an Azure tab: host PATs, bot username, webhook enable, and
webhook secret. URL: `POST /webhooks/azure` with `X-Azure-Token`.

GitLab leftover PAT is never sent to a TFS `/_git/` remote. Azure
leftover `AZURE_PAT` + `AZURE_ALLOWED_HOSTS` follows the same rule as
GitLab when `AZURE_HOST_PATS` is empty.

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

- Azure DevOps Server 2022.2 webhook intake (`POST /webhooks/azure`)
- Settings tabs: Jira, GitLab, Azure, Projects, Agent, Runtime
- TFS clone/push use Azure PAT only (empty username, no GCM prompt)
- Settings keeps a newer `.env` key (including `TRIGGER_ASSIGNEE_NAMES`) unless you later save that same field
- Dashboard login is the in-page form; Edge does not get a native HTTP/Windows popup on first load
- Jira webhook intake removed; board poller only
- One Jira bot name; one GitLab trigger username
- Exe zip ships one `opencoderman/` tree (`derman-build`, `derman-plan`, `skills/` only)
- `plan_refactor` reads every Jira comment page

## Config (secrets stay out of the binary)

Copy `.env.example` next to the executable. Minimum:

```env
JIRA_HOST=https://your-jira.example.com
JIRA_API_TOKEN=your-api-token-here
JIRA_BOARD_ID=1
TRIGGER_ASSIGNEE_NAMES=your-jira-display-name
```

Durable data: `YAVER_DATA_DIR` (`C:\vd\yaver` / `/vd/yaver`). Temp clones: `TEMP_DIR_BASE` (`C:\vd\t` / `/vd/t`).
