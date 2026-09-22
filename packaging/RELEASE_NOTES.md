# Yaver 0.9.56

Scheduled and Sessions now use a local SQLite index, the same way Jobs does.
schedules.sqlite and opencode-binds.sqlite are created on first start, including when the JSON files are already there.
The JSON files remain the full record, and the Sessions page lists every live bind.

Looking up an existing issue fills repository, source, target, and mode in the same fields as a new issue.
The prompt box shows the ticket text without the {params} block.
Schedule or Run now writes those fields back to Jira only when you change them.
The source choice is labeled custom branch.

Hovering a dot on the Analytics jobs chart shows the time bucket and the exact count for every series that is turned on.

When a merge request cannot be created, the job log and the Jira comment include the remote status and the server message.
Submodules are updated after the work branch is checked out, so the pins match the branch the job edits.

# Yaver 0.9.55

Analytics and the Jobs list read a local SQLite index next to the job files. The index is created on first start, including for a data directory that already has months of job JSON. The visible Jobs page still opens those files, so error text and the session id stay on the list.

Storage is one folder, YAVER_BASE_DIR. The default is %LOCALAPPDATA%\Yaver on Windows and ~/.local/share/yaver on Linux. Data is {base}/yaver and clones are {base}/t. An existing YAVER_DATA_DIR or TEMP_DIR_BASE still wins for that path.

The chart step follows the time range. There is no separate Hour, Day, Week, or Month control. The mark is phosphor green, and the in-app wink is larger.

# Yaver 0.9.54

A GitLab review keeps the overview comment on the merge request when an inline finding cannot be posted. That skip is logged, and it no longer fails the whole MR job. Linux install and start scripts are stored with LF line endings, so WSL bash can run them.

# Yaver 0.9.53

Analytics splits merge requests Yaver opened from ones it only commented on. Open, Merged, and Closed cards go to a list of the unique MR and PR links. Counts are split into Opened by us (Jira or Azure Boards) and Contributed (a comment on an existing GitLab MR or Azure PR). Review and build jobs on the same URL still count once. Search and Issue key filters are gone from Analytics.

# Yaver 0.9.52

Selected code on a GitLab or Azure review comment reaches the agent. An inline `/yaver`, `/review`, or `/ask` on a selected range puts the file, the lines, and a clone snippet in the prompt. A reply on that thread loads the original range and the earlier notes. Azure PR file-thread comments load the file, lines, and parent comments from the thread API, because the webhook does not send them. The Scheduled list pages the same way Jobs does, 25 rows at a time. Analytics shows Open, Merged, Closed, and Total cards for unique merge requests stored on the jobs.

# Yaver 0.9.51

A second GitLab `/yaver` on the same merge request no longer fails to push. The queued follow-up fetches the first job's push and rebases onto it before delivering, so the local branch does not stay behind origin (`tip of your current branch is behind`).

# Yaver 0.9.50

Analytics no longer hangs when the page refreshes or a chart request is slow. A live tick does not abort an Analytics GET that is already running. Changing the period or the filters still cancels the previous request. The daemon stops a cancelled walk and returns 504 if aggregation takes longer than 55 seconds. Issue key is an exact match, so `KAN-24` does not include `KAN-240`. Search still matches a substring, and several keys can be separated by commas. Azure `/yaver` on a `plan_ready` ticket posts the same wait note GitLab already posts on the MR. Implement still waits for `plan_execute`. Plan ready and in flight show up on the cards, the chart, and the tables. Repository URLs with or without `.git` count as one repo. Jobs with no model appear as `(unset)` so the shares add up to 100%. There is a By agent table and a repository filter. A custom range copies the current from and to dates instead of jumping to all time. `GET /api/analytics` runs off the event loop, so Jobs and Stop stay responsive.

# Yaver 0.9.49

Analytics no longer times out when you change the chart bucket. Switching the period or the bucket, for example from 24 hours to Month, cancels the previous Analytics GET instead of showing Request timed out. The request budget is 60 seconds. The bucket series is capped so a coarse bucket cannot hang the handler.

# Yaver 0.9.48

The dashboard has an Analytics page at `/analytics`. It charts job counts over time and can filter by status, model, category, source, and more. When a GitLab MR or an Azure PR is merged or closed, Yaver still deletes the clone, the session logs, and the OpenCode rows, and it keeps `job_*.json` so Analytics can count those runs. Deleting a row from Jobs still removes it. `/review` and `/ask` no longer reset a ticket that is waiting at `plan_ready`. They rebind onto a synthetic GL or AZ key. A failed `plan_execute` stays retryable. The label is not renamed to `plan_executed` before implement finishes.

# Yaver 0.9.47

On Windows, Storage Delete and merged-review clone cleanup no longer follow the `.yaver-plans` junction into `{YAVER_DATA_DIR}/plans`. Deleting one clone no longer deletes plan files that belong to other tickets.

# Yaver 0.9.46

When a GitLab MR or an Azure PR is merged or closed, Yaver also deletes that review's jobs, session logs, plan file, issue state, and OpenCode session rows. The shared daemon log is kept, and a job that is still running is skipped. After each OpenCode `GET /session/{id}/message`, the job session log records the last assistant finish, info.finish, and step-finish.

# Yaver 0.9.45

The dashboard uses the new Yaver mark. The sidebar, the login gate, and the boot splash show icon A as a short wink GIF. The README uses lockup C (YAVER / Sanal Geliştirici).

# Yaver 0.9.44

Opening a Sessions workspace no longer times out while the clone size is measured. The detail GET uses the Storage size cache instead of walking the temp clone on the request. The SPA aborts GETs after 15 seconds, and walking a real clone on Windows or WSL can take longer than that.

# Yaver 0.9.43

A Sessions workspace lists every distinct GitLab MR or Azure PR linked from jobs on that repo, source, and target. The Sessions list is paginated at 25 rows per page, and it can be searched by repository, branch, issue key, or kind.

# Yaver 0.9.42

Sessions lists one workspace for each repository, source branch, and target branch. The detail page shows the linked OpenCode sessions (plan, build, and test), the jobs, the temp clone with Storage delete when it is not in use, and plan files when a plan bind exists. GitLab and Azure jobs started from the webhook queue take the same per-issue lock as the handle path. Stop plus a leftover `/yaver` cannot start a second OpenCode session beside the cancelled worker.

# Yaver 0.9.41

Windows Distribution no longer fails the GitHub Release when Defender quarantines `opencode.exe`. The payload check lists every missing path. If `vendor/opencode-home.zip` is in the zip, a missing `opencode.exe` is a warning, not a failed release. The zip is still enough to install.

# Yaver 0.9.40

The Windows offline zip is attached to the GitHub Release again. CI turns off runner Defender scanning of `opencode.exe` and restores the binary from `vendor/bin` if antivirus removed the copy under `opencoderman/vendor/bin/windows`. That check had been failing on every Windows zip from 0.9.36 through 0.9.39, so those releases never received the offline zip.

# Yaver 0.9.39

A compact recap with `finish=None` is not a crash and it is not success. OpenCode 1.18 lists the compact recap (`agent=compaction`, `summary=true`) before `info.finish` is set, while the UI already shows the summary. The work turn in the log is `finish='stop'` with `summary=None`. That recap means the job is still incomplete, so Yaver waits for auto-resume. Success counts only after a later assistant turn that is not a recap.

# Yaver 0.9.38

The Windows offline zip builds again. CI requires `opencoderman/agents/derman-reviewer.md`, which is the agent actually in the tree, instead of the old `code-reviewer.md` alias. It also checks the current Settings copy, so the zip is attached to the GitHub Release.

# Yaver 0.9.37

Unused temp clones older than 7 days are deleted once an hour. The limit is `TEMP_CLONE_MAX_AGE_DAYS` in Settings. A job that is still running is never removed. Set the value to 0 to turn the policy off. The OpenCode session bind is kept, so a later mention on the same MR or PR reclones the repo and resumes that session.

# Yaver 0.9.36

The dashboard stays up while many clones are running. Git clone, checkout, push, and clone delete use a dedicated `yaver-git` thread pool sized to the maximum number of concurrent jobs. Those git calls no longer occupy FastAPI's default executor, so `/api/jobs` and `/ws` do not hang behind `git clone`.

# Yaver 0.9.35

Asking for review again after a failed review starts a new job. A GitLab or Azure assign that follows an error no longer reuses the failed queue row (`review-update-{iid}`). `install-opencode-agents.bat` and `install-opencode-agents.sh` copy `derman-reviewer.md`. They previously copied only the build, plan, and test agents.

# Yaver 0.9.34

Assign as reviewer matches the PAT user id, the same way aMIR-mini does, not only the trigger-user string. GitLab uses `GET /user`. Azure uses `connectionData` and accepts `git.pullrequest.reviewers.update`. Settings has a separate Review model (`DEFAULT_REVIEW_MODEL`). Plan, build, test, and `/yaver` keep using Default model. An empty review model still uses Default model, and a per-issue `Model:` line still wins. Settings, under Jira, can turn the board poller and Jira comments off with `JIRA_ENABLED=false`. GitLab and Azure jobs still run.

# Yaver 0.9.33

Azure DevOps Server 2020 Update 1.1 works alongside Server 2022. REST calls try API versions `7.1`, then `7.0`, then `6.1`, then `6.0`. Server 2022 still uses 7.x. Server 2020 Update 1.1 only speaks REST 6.0, and it no longer fails after the 7.1 and 7.0 attempts. Settings Test, PR comments, threads, and work items all use that same list.

# Yaver 0.9.32

GitLab and Azure code review is always on, using the same review rules as Creasy. `@bot /review`, `@bot /ask`, assigning the bot as reviewer, or opening an MR or PR while the bot is already a reviewer starts a review. New commits on that review do not start another one. An overview is posted when there is no thread, and a reply stays in the thread it was written on. Review jobs do not push. `/review` and `/ask` on a work item stay silent. A `/review` result that includes an `opencoderman-findings` fence opens one inline file and line thread per finding. `/ask` stays on the request thread only. The agent is derman-reviewer. Jobs are labeled `gitlab-review` or `azure-review`.

# Yaver 0.9.31

An Azure collection URL no longer has to include `/tfs`. Both `https://host/tfs/DefaultCollection` and `https://host/DefaultCollection` can be saved. A hostname with no collection name, or `/tfs` by itself, is still rejected. The path is stored the way it was entered. Yaver does not insert `/tfs` when it was left out.

# Yaver 0.9.30

Plan, build, and test keep three OpenCode sessions. Failed implement retries with plan_execute. Queue reap no longer starts a second job during accept. Settings Projects has no default source.

# Yaver 0.9.29

Operator comments no longer use “Yapay zekâ — …” headings. Ticket {params} stay multiline. Sidebar is one clock, Connected, then stacked Report issue and Sign out.

# Yaver 0.9.28

PAT assign failure stops the job instead of leaving the work item unassigned. Schedule shows saved collections and all team projects. Operator comments are Turkish.

# Yaver 0.9.27

Storage no longer sticks Azure PR status on Unknown after a restart. The no-linked review warning is clearer.

# Yaver 0.9.26

Work-item jobs start only on Assigned To and comments, not board moves. Settings saves only AZURE_COLLECTION_PATS.

# Yaver 0.9.25

Azure work-item webhooks no longer freeze the ops dashboard. TFS HTTP runs off the request thread. Settings Test probe is not used on assign.

# Yaver 0.9.24

Work-item assign uses the Settings Test identity path (TFS FedAuthRedirect). Updated hooks only listen for Assigned To, comments, and board position. Trigger label is gone from Settings.

# Yaver 0.9.23

Azure work items start on To Do / In Progress and the same columns (New, Active, Doing). After accept they move to Active / Doing / In Progress — not Done. English and Turkish READMEs document every Jira, GitLab, and Azure flow with examples.

# Yaver 0.9.22

Work-item keys are WIT-{project}-{id}. PR/MR comments bind Jira, WIT, #42 (this collection), then git match, then AZ-/GL-. Agent Ticket line is 42; dashboard stays WIT-BETA-42. Storage warns when a clone has no MR/PR.

# Yaver 0.9.21

Azure work items start on any open column (not only New). Create, webhook accept, and /planExecute assign the PAT user. One usage note per comment. Clone URLs drop a trailing .git.

# Yaver 0.9.20

Fix Scheduled Azure lookup PAT. New Azure work item on Scheduled. One collection URL → PAT map.

# Yaver 0.9.19

Azure work items: Scheduled lookup like Jira, ticket name is the id, HTML comments, PAT collection URLs, ignore our own webhook PATCHes.

# Yaver 0.9.18

Scheduled → Existing issue can look up an Azure work item (collection + id), same as Jira.

# Yaver 0.9.17

Azure Boards work items start from service hooks (assign a New item to the bot). After a plan, use @mention /planRefactor or /planExecute on the work item. Settings require a TFS collection URL (https://host/tfs/Collection), not a hostname.

# Yaver 0.9.16

Issue reports can include several jobs plus serve status and OpenCode logs. GitLab/Azure review comments now include the selected file range in the agent prompt.

# Yaver 0.9.15

Linux standalone yaver is one freeze per Ubuntu (18.04, 20.04, 22.04, 24.04). The 0.9.14 binary was built on Ubuntu 24.04 and failed on older glibc (GLIBC_2.38 not found). Download the archive that matches the host.

# Yaver 0.9.14

Stuck-job watchdog aborts the live OpenCode session before ERROR. A cancelled GitLab/Azure worker cannot COMPLETE a newer run. Builds do not resume the plan chat.

# Yaver 0.9.13

Stop on macOS kills leftover clone tools. Cancel does not take down shared OpenCode serve. Leftover GITLAB_PAT still works when Azure hosts are set. Stop during clone stays Cancelled. Stop is refused on plan_ready.

# Yaver 0.9.12

Jobs tab shows Queue (N) from the work queue. GitLab and Azure follow-ups still count while that ticket is in flight.

# Yaver 0.9.11

Oracle consult is gone. Tickets no longer route on "should we / architecture / how to". Only Mode: plan, Mode: build, and Mode: test remain. No Mode still goes to plan.

# Yaver 0.9.10

Settings can save JIRA_PROJECTS so feat(KAN-12) binds to the Jira ticket.

Storage Delete works after the job ends. Session binds no longer leave the clone marked In use.

KAN-1 no longer lists KAN-10 jobs. /yaver on a plan_ready MR posts a wait note. Dropped-accept does not overwrite COMPLETED. Hyphen and underscore issue keys stay separate. Old scheduled tickets still wait after 500 newer rows.

# Yaver 0.9.9

Storage Delete is refused while a job owns the clone. The dashboard button is disabled and labeled In use.

Schedule Cancel is refused for dispatching, so it cannot abort a live job on the same issue.

Assignee İrem matches JIRA_TRIGGER_USER=irem (Turkish dotted and dotless I).

Jira Cloud ADF/smart-link/mention chips are out of scope (on-prem Server/DC). GitLab REST MR create stays HTTPS. Re-adding plan_refactor reuses the latest @bot mention.

# Yaver 0.9.8

Reply headers always include job_id, matching Creasy: `**Yaver {version} — Kind** · model · job_id`.

Queue claim scans 1000 queued rows so a long blocked MR/PR backlog does not hide a free repo.

`@<GUID> /yaver` without a configured trigger name is still a usage note, not a job. Scrum poller still uses the first active sprint only.

# Yaver 0.9.7

Scheduled GitLab MR and Azure PR follow-ups post the prompt as a regular note, not a resolvable review thread. The model answer is a reply to that note.

# Yaver 0.9.6

Optional `JIRA_TRIGGER_LABEL`: when set, To Do intake needs the trigger user AND one of those labels. Empty still means assignee only.

`@mention /ask` and `/review` are silent (other agent). Other invalid mentions get a usage note with no `@` tokens.

Thread follow-ups send **Replied message** and **Prompt** as separate sections. Replies start with `**Yaver {version} — Kind** · model · job`.

# Yaver 0.9.5

HTTP Azure DevOps Server remotes clone again. `http://tfs:8080/tfs/…/_git/…` stays HTTP when the PAT is applied. HTTPS and SSH remotes still use HTTPS + PAT.

A leftover `GITLAB_PAT` (no host map) authenticates MR replies, same as clone and push. Settings Test connection does not send that leftover token to a newly typed host.

# Yaver 0.9.4

Comment jobs start on `@mention /yaver` (was `/execute`). A mention without `/yaver` still gets a usage note.

`Mode: test` runs derman-test: unit tests only, after reading the clone AGENTS.md. Push + MR like build.

TFS mentions match identity chips, `@<VSID>` GUIDs, and `CORP\user`. The PAT user's id is seeded from `/tfs` connectionData. Reviewer GUIDs on the PR count as the bot.

derman-build stays on the already checked-out work branch (no git checkout / switch). Plan and build write tests the way derman-test requires.

# Yaver 0.9.3

GitLab replies stay in the MR discussion (Discussions API, not Notes `in_reply_to_discussion_id`).

Azure comments that omit threadId are no longer collapsed when every new thread starts at comment id 1.

A second `@mention /execute` on the same ticket stays on the Queue tab until the live job finishes.

Storage deletes only the clone for that project + MR/PR id — not a Jira plan folder that shares the issue key, and not another repo with the same PR number.

# Yaver 0.9.2

Azure Schedule → Run now now queues the job. TFS comment ids restart at 1 on every new thread; dedup uses PR + thread + comment so a second Run now is not treated as a duplicate.

# Yaver 0.9.1

Schedule can follow up on an existing Azure DevOps PR the same way as a GitLab MR.

Replies (usage notes, OpenCode results, errors) stay in the triggering MR/PR thread.

Azure PR create no longer double-encodes project names with spaces. Job-detail commit links use `/commit/{sha}?refName=refs/heads/{branch}`. Storage shows Azure PR status and does not spam `Folder not found` for stale clone names.

# Yaver 0.9.0

GitLab and Azure comment jobs start only on `@mention /execute`.

A mention without `/execute` gets a usage note in that same MR discussion or PR thread. Yaver does not open a new post. `@mention /ask` is still a silent handoff to the other agent. Comments from the bot user are ignored (no usage note).

Example: `@yaver /execute fix the login bug`

Webhook URLs are `POST /yaver/webhook/gitlab` and `POST /yaver/webhook/azure`. The previous `/webhooks/gitlab` and `/webhooks/azure` paths still work.

# Yaver 0.8.1

Azure webhooks have no secret and no password. `POST /webhooks/azure` does not check `X-Azure-Token`. Leftover `AZURE_WEBHOOK_SECRET` is ignored.

Settings Test is success when `https://<server>/tfs/_apis/connectionData` is 200. A 404 on the project list or an auth/login page is not a failed PAT.

# Yaver 0.8.0

One trigger list per provider: `JIRA_TRIGGER_USER`, `GITLAB_TRIGGER_USER`, `AZURE_TRIGGER_USER` (comma-separated names, no `@`). Leftover `TRIGGER_ASSIGNEE_NAMES` / `GITLAB_BOT_MENTIONS` / `AZURE_BOT_MENTIONS` still load when the new key is empty.

Azure Settings Test matches Creasy 0.9.1: authenticate at `https://<server>/tfs/_apis/connectionData`. A collection-scoped `connectionData` call is 400 on TFS. Host can be `tfs.example.com` or `tfs.example.com/tfs`. Clone still uses the collection on the git URL from the webhook.

`@bot /ask …` on a GitLab MR or Azure PR comment is not a Yaver job. That command is routed to another agent. The webhook returns `ignored /ask handoff` and does not enqueue. `/asking` and `/ask-review` still start a job. Merge and PR lifecycle hooks are unchanged.

Schedules can still follow up on an existing GitLab MR: look up a saved project or paste a URL, store a prompt, post it on the MR at fire time, then run the usual note job.

Azure webhook, REST, Settings Test, git, and PR workflow steps now write `[azure]` lines (never a PAT or Authorization header). A rejected comment says why — token header, configured vs extracted mentions, empty body, bot reply. Lines that arrive before `job_id` exists are copied onto the job, so Job → Logs shows intake.

Dashboard first paint stays on the ops card. Slow or failed `/api/meta` shows Retry instead of a blank Loading, and that endpoint no longer waits on blocking Jira or GitLab work.

`plan_execute` with no plan file comments on Jira and stays at `plan_ready`. Pasted git hosts keep a non-default port so on-prem PATs match Settings. A last-turn pending `question` tool leaves compact-wait for the unattended nudge.

Azure auth is still HTTP Basic `pat:<PAT>` (Settings Test, clone, push, PR). Mention the bot on a PR comment; Yaver replies on the PR. Completed or abandoned PRs delete the matching temp clone.

Settings has an Azure tab: host PATs, bot username, webhook enable, and webhook secret. URL: `POST /webhooks/azure` with `X-Azure-Token`.

Standalone exe zips still ship **one** OpenCode kit: `opencoderman/agents` (**derman-build** and **derman-plan** only — not gitlab-reviewer) and `opencoderman/skills`. There is no second `opencode_configs/` tree. Windows zips include `install-opencode-agents.bat`; Linux zips include `install-opencode-agents.sh`. That script copies only those two agents plus skills into the OpenCode home.

Plan-refactor reads every Jira comment page, so a late `@bot` mention is not missed on busy tickets.

The exact OpenCoderman submodule commit is in `opencoderman.pin` and `opencoderman-<sha>.zip` on this release.

Changelog: see `CHANGELOG.md` in the source tree.

## What to download

### Standalone executables (no host Python)

| File | Platform |
|------|----------|
| `yaver-windows-x64-*.zip` | Windows x64 |
| `yaver-linux-x64-ubuntu-18.04-*.zip` or `.tar.gz` | Ubuntu 18.04 (glibc 2.27) |
| `yaver-linux-x64-ubuntu-20.04-*.zip` or `.tar.gz` | Ubuntu 20.04 (glibc 2.31) |
| `yaver-linux-x64-ubuntu-22.04-*.zip` or `.tar.gz` | Ubuntu 22.04 (glibc 2.35) |
| `yaver-linux-x64-ubuntu-24.04-*.zip` or `.tar.gz` | Ubuntu 24.04 (glibc 2.39) |

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
