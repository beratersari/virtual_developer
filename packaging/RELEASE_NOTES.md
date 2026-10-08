# Yaver 0.9.81

Opening a scheduled ticket shows the prompt, the repositories and branches, the mode, the model, and the scheduled time before any run exists. The jobs list says the run has not started yet.

Settings Test, Remove host, Remove collection, Add host, Add collection, and Reload from tokens use the same buttons as the rest of the dashboard.

The dashboard checks the release site about every 15 minutes. When a newer package is published, a banner at the top names that version. The info button explains how to run `update.bat` or `./update.sh` in the Yaver folder. The dashboard does not download the package and does not replace files.

# Yaver 0.9.80

A tagged release attaches `yaver-executables-X.Y.Z.zip`. That file contains `yaver-windows-x64-X.Y.Z.zip` and `yaver-linux-x64-ubuntu-18.04-X.Y.Z.zip` through `24.04`. Those Ubuntu zips are no longer separate files on the release page. `yaver-windows-x64-X.Y.Z.zip` and `yaver-windows-latest.zip` stay on the release page. The office site takes this one zip and still offers each system as its own download. That same zip contains `RELEASE_NOTES.txt`, the `# Yaver X.Y.Z` section from these notes. The office site reads that file and shows it as the release note for the version.

Analytics status filters show the job count beside Completed, Error, and Cancelled, the same way Executing already does.

# Yaver 0.9.79

`update.bat` and `update.sh` write the published version into `VERSION` and `_internal/VERSION` when either file is still an older version. A root `VERSION` that already matches no longer leaves `_internal/VERSION` unchanged. Robocopy copies a version file even when the new text is the same length. A blank `RELEASE_HOST` uses `15.210.7.55`. A blank `RELEASE_PORT` uses `8090`. A host or port already in `.env` is kept. Copy the new `update.bat` or `update.sh` from this zip into the Yaver folder once before you run it. The script that is already in the folder stays until you replace it.

# Yaver 0.9.78

Starting a Jira job assigns the issue to the trigger user from Settings. The PAT user is used when that name is empty or Jira rejects the assign.

# Yaver 0.9.77

Scheduled merge requests and pull requests can run as a review. On Scheduled → MR, Mode **GitLab review** starts a GitLab review. On Scheduled → PR, Mode **Azure review** starts an Azure review. Existing issues and new issues stay on plan, build, and test. An empty model uses the review model from Settings.

Settings Board ID accepts several Jira boards, comma-separated (for example `2, 5`). The poller reads each board. Each Scrum board still uses only its first active sprint. A failure on one board leaves the others in that cycle, and an issue that sits on two boards is taken once.

Windows `update.bat` copies program folders with robocopy. It writes the published version into `VERSION` and `_internal/VERSION`, including when the package still has the previous text in those files. Copy the new `update.bat` from this zip into the Yaver folder once before you run it. The script that is already running is left in place.

# Yaver 0.9.76

The Windows offline package and the OpenCode, Claude Code, and Codex zips are built again. The 0.9.75 Windows dist deleted the downloaded binaries and then tried to pack those zips from the missing files. The zips are packed first, and the download cache is removed after that.

# Yaver 0.9.75

A finished GitLab `/review`, `/ask`, or `/yaver` reply is marked reviewed, so the merge request shows Re-request review. `/ask` posts the overview and leaves approval to `/review`. When a `/review` findings JSON is empty, Yaver approves the merge request and still posts the overview note.

Update an executable install from the Yaver folder. On Windows run `update.bat`. On Ubuntu run `./update.sh`. The script reads `RELEASE_HOST` and `RELEASE_PORT` from `.env`, checks the package, stops Yaver in that folder, and replaces the program files (`yaver.exe` or `yaver`, `_internal`, `.env.example`, `opencoderman`, and `install-agents.bat` or `install-agents.sh`). `.env` and the script you ran stay. Start Yaver after the line that begins with Updated to. An install that does not have `update.bat` or `update.sh` yet can take the script from the published zip: download the zip once, copy that script into the Yaver folder, and run it there. `yaver update` and `yaver --update` print that instruction and leave the install unchanged.

OpenCode, Claude Code, and Codex release zips are named with that tool's version. A Windows OpenCode 1.18.10 package is `yaver-opencode-windows-x64-1.18.10.zip`. Claude Code and Codex use the same pattern, and the Linux names do too. The Codex zip packs the file named `codex.exe`.

Settings Sync copies agent files into the OpenCode and Claude homes and reloads OpenCode when no job is running. Saving or creating an agent writes the catalog and leaves OpenCode running. The separate Reload OpenCode button is gone.

# Yaver 0.9.74

A review that already printed its answer is no longer run again just because one todo stayed in progress. The turn is complete when OpenCode stops and no todo is still pending. A todo that is still pending keeps the job incomplete, and a build is asked to finish that work.

The sidebar OpenCode line says "OpenCode healthy, reload waiting" when the process is healthy and a reload is waiting for the current job. A reload in progress reads "OpenCode reloading". The other lines say whether OpenCode is healthy, up but not answering, not running, failed, or the status could not be read.

# Yaver 0.9.73

Stop Yaver, then run yaver update or yaver.exe --update. The command reads RELEASE_HOST and RELEASE_PORT, or --host and --port, downloads the package for this computer, replaces the install, and starts Yaver again. Settings no longer has Check or Update. The release address stays in .env. The dashboard does not download or replace Yaver.

The updater keeps the executable bit stored in the zip, so a Linux yaver can be started after the swap. If that program cannot be started, the previous executable folder is put back. The Linux shell helper waits until the dashboard port opens, and puts the previous folder back when the new copy does not. It also refuses to replace files while that port is still open. The Windows PowerShell helper checks the dashboard address from the plan, not only 127.0.0.1. A dashboard address that does not answer is treated as closed after a short wait, so one check cannot use the whole health budget.

Windows and Linux releases attach one zip each for OpenCode, Claude Code, and Codex. Each zip is that CLI, its host config, and its install command. The combined yaver-clis zip is no longer built.

A job whose merge request or pull request was merged or closed no longer stays live. The row is cancelled with the message that the review closed while the job was still open, and that review is stopped.

# Yaver 0.9.72

Settings → Runtime can point at a release server on the office network. Check looks up the Windows package or the matching Ubuntu package. Update closes Yaver, downloads that zip, and starts Yaver again. The .env file and the data folder stay, including when that data folder sits inside the executable folder. A git checkout is left alone.

On Windows, Update finishes replacing yaver.exe and starts it again. The helper stays alive while the running copy is stopped, including when Python is a launcher in front of the real interpreter, so the new folder is not left unfinished. PowerShell is not started as a detached process, because that flag makes powershell.exe quit before it reads the update plan. If the new copy does not open its dashboard, that new process is stopped, the previous folder is put back, and that previous copy is started again. This includes the case where the new process was started by the helper and its working directory is the install folder.

A tagged release attaches one zip per standalone binary with latest in the name: yaver-windows-latest.zip and yaver-ubuntu-18.04-latest.zip through yaver-ubuntu-24.04-latest.zip. Each file is that platform’s executable zip. The version is the VERSION file inside it, for upload to the office release site. Standalone executable zips and tar.gz files list yaver.exe or yaver and the other files at the archive root. The Linux offline zip and tar.gz do the same for install-dashboard.sh, src/, and vendor/.

The dashboard can switch the phosphor console to a light workbench. The choice is saved in this browser. The wink mark is transparent, so the dark theme fills it with navy and the light theme uses the page color. A disabled secondary button uses muted text, a transparent background, and the normal border, so a disabled Look up control does not look ready.

Windows and Linux start scripts read OPENCODE_SERVER_PASSWORD from the process environment or .env and send it on the OpenCode health check. A 401 stops the wait instead of treating the server as down. The password stays off the command line. The scripts expand ${NAME} and ${NAME:-default} the same way the daemon does, so a password written that way is the same secret on the next start. Linux import keeps a trailing newline in the password.

# Yaver 0.9.71

Yaver starts OpenCode serve when it is down, and starts that child again if it exits. A serve that is already healthy is left running. Saving, creating, or syncing an agent copies the catalog into the OpenCode and Claude homes and reloads serve when no job is planning or executing. A running job keeps the current process until it finishes. Settings has Reload OpenCode for a change made outside the editor.

The sidebar shows OpenCode serve health at the bottom left on every page. The line reads healthy, restarting, reload waiting, not answering, down, or failed. Hover shows the detail from the serve check. A status that cannot be read stays unavailable.

Restarting Yaver while an agent save is still waiting reloads OpenCode before the next job starts. The running serve is kept only until that restart. The saved agents are not left behind in the old process.

Saving or creating an agent while a job is running leaves the jobs that are still queued where they are. Those jobs start after OpenCode reloads, so they see the new agent. The running job keeps its current serve process.

A failed OpenCode reload leaves queued jobs queued. They start only after a later reload succeeds and the new process is using the saved agents. The queue also stays put when that reload state cannot be read.

OpenCode serve stays up when the work queue cannot be read. A locked or unreadable queue is not treated as an empty queue. A queue database that never opened is the same case, because listing it returns no rows and does not raise.

OpenCode serve started with OPENCODE_SERVER_PASSWORD stays up. Health checks and job requests send that password. OPENCODE_SERVER_USERNAME overrides the default user opencode. A 401 from a missing password is not treated as a crashed serve.

A planning or executing issue still blocks a reload when the issue-state database cannot be read. That read used to look like no jobs, so an agent save restarted serve during clone.

A missed OpenCode health check no longer stops serve while a job already has a session. The process stays up when it is still listening. A job that starts during a reload waits for that restart, and Yaver does not open a second serve beside it. A job that appears at the moment of a stop keeps the current process.

A job that has not reached OpenCode fails within a few seconds when serve is listening but does not answer. That opening check does not use the agent time budget. The job leaves executing, and the quiet process can then be replaced.

Storage and Sessions is one sidebar tab. Details on a folder lists the OpenCode chats whose working directory is that clone or a checkout inside it. An old /sessions address opens Storage. Claude Code and Codex replies stay on the job Transcript tab.

Storage and Sessions lists OpenCode chats whose clone folder was deleted. Age delete keeps the session so a later job can resume it. Those chats stay on the page, with Reset, after the folder is gone.

The dashboard is a dark phosphor console with square controls. The top bar shows the open issue name, the same name as the browser tab.

List pages no longer repeat their name in a kicker. Job pages no longer repeat the issue name, status, and worker. Settings and storage keep the rules that stop a bad action.

# Yaver 0.9.70

A follow-up comment on one repository of a multi-repo job checks the other repository out on feature/{KEY} when its saved source is a primary base (develop, main, master, trunk, dev, or release/*). The follow-up reuses the first run's folder, so that repository's feature commits stay in the workspace. The commented repository stays on the branch named in the review.

Dashboard pages no longer stay blank while an earlier read still holds the browser's connections. Leaving a page cancels that page's read. A live update waits for the read already in flight. Jobs, Sessions, and Scheduled stay on Loading until the response for the list you opened arrives.

Storage review links are !N for every merge request on a folder, including a multi-repo folder. The repository path stays in the link target.

A file: clone is allowed when a GitLab or Azure token is configured. That remote has no hostname and does not receive a token.

On Linux, Yaver and OpenCode serve have to run as the same user. A systemd unit sets User= and Group=. After a root run, chown the data and OpenCode trees once, then start both as that user. Port 8080 does not need root.

# Yaver 0.9.69

Jobs, the queue, schedules, session binds, and issue state now live in one yaver.sqlite file in the Yaver data folder. The first start imports the JSON files that were already there. Plans, session logs, and clone folders stay on disk. Analytics still counts a run after a merged review deletes the issue state, because the job row remains.

The storage page lists every merge request on a multi-repo folder. Each delivery keeps its own link and state, and updating one review does not drop the others.

The job log lists the commits that are about to be pushed, then the text git prints while the push runs.

Stopping a review whose local issue was already removed still cancels that job. After a restart, that row is no longer left executing. The job row stays so Analytics can count it.

Saved projects and repo sets stay in saved_catalog.json. The dashboard shows that list after a refresh, and saving another setting does not replace it. Reload from tokens still imports the repositories the GitLab and Azure tokens can read.

Copying a dashboard address pastes the page you have open. A job link uses that job's title, an issue link uses the issue title, and the other screens use their own names, such as New issue, Settings - Jira, Jobs - In flight, and Analytics.

# Yaver 0.9.68

A review or a test that hits a timeout, an error retry, a clarifying question, or an idle continue stays on that job. The review follow-up finishes the review. The test follow-up finishes the unit tests and may commit those tests. A build job still receives the build continue line.

Stopping a job aborts its OpenCode session, including when the configured worker is Codex. Shutting Yaver down waits for that abort before the process exits.

Codex keeps the thread id from the start of the run when a later message quotes another id. A Codex job does not resume a Claude chat from another branch of the same issue. A Codex thread on another branch of that issue still resumes.

The Sessions page lists OpenCode chats. A multi-repo row shows the session id, the repositories, and the clone folder. Reset names that one session, and the other sessions on the same branch stay. Claude and Codex replies stay on the job Transcript tab. The list is read once.

The example agent timeout in .env.example is 14400 seconds. An install that already saved a timeout keeps that saved value.

# Yaver 0.9.67

The Jobs Queue tab shows waiting items when you open it. Switching to Queue keeps the list already loaded, and a new waiting item shows up without waiting for the next poll. Finished queue history is not read again on each visit.

Settings values stay on what you saved. Saving another setting, then restarting Yaver, no longer puts the old .env line back for Board ID, poll interval, concurrent jobs, clone age, agent timeout and retries, default model, review model, worker, Jira, GitLab, and Azure trigger names, or the Azure webhook switch. An older Jira host stored in Settings does not replace the host in .env on that restart.

Saved projects stay hidden until you press Reload from tokens. Opening Yaver, Scheduled, or Settings does not load that list, and a save before that reload does not replace the stored projects. There is no cap on the list. A token reload keeps every repository the tokens can read, and saving that list keeps every row.

Code review jobs are counted in the job tables. Merge request cards and the merge request list count ticket work and /yaver follow-ups.

# Yaver 0.9.66

A follow-up on one repository of a multi-repo job keeps every repository on its own branch, in the same shared folder. Scheduling the job stores each repository's source and target. A job that already saved the first repository with empty branches fills those branches from the ticket description the next time you comment on a review. The prompt names the branch of the merge request or pull request you commented on, and it lists each clone with that clone's own work branch and target.

On a schedule, repositories are a list. The pencil edits one repository, the trash icon removes it, and adding a repo set appends those repositories to the ones already on the form. A repository set to the issue key uses feature/{KEY} on every repository, including the later ones.

A plan job that hits a timeout or a compact continue stays on the plan. Revising a plan, commenting on a review, running tests, or following up a merge request keeps that new instruction when the chat resumes on Codex or Claude Code. A second run of the same build still uses the short continue line.

# Yaver 0.9.65

A follow-up comment on one merge request or pull request of a multi-repo job pushes only that repository and reuses that review. The other repositories stay local. A push of the shared folder fails, because that folder is not a git repository. A later run that names one repository drops the previous set, including when a schedule sends an empty list while the ticket text still names the old pair. A description that does not parse, and that does not send a repository list, leaves the stored set in place.

A comment on a review this job already opened stays on that issue when the title has no key or names a different ticket. Merging one review keeps the plan and the local issue until every review recorded on the job is merged or closed. A later job that uses the same repository, source, and target waits until the running job finishes.

Settings → Projects keeps the list you already saved until you choose Reload from tokens. Saving accepts the full imported list. You can search projects by name, select the visible rows, and delete that selection. Schedule and repo-set pickers find a saved repository by name or URL.

Analytics uses the width of the page. The breakdown tables keep their columns on screen, the jobs chart keeps a fixed height, and category mix sits with that chart. The standalone executable reads its agents from the folder beside the program.

# Yaver 0.9.64

A dashboard job can work in every repository of a saved repo set, or in extra repositories you add on the schedule. Each repository keeps its own source and target, and Yaver pushes it and opens its own merge request. The job page and the issue page show each repository's commit and merge request. One repository on a schedule still runs as a single job. Mode, backend, and model sit with the repositories on the schedule form.

Repo sets and saved projects are lists. The plus button adds one, the pencil edits it in a popup, and the trash icon removes it. Opening Settings → Projects loads every Git repository the saved GitLab and Azure tokens can read, and a project you already saved keeps its label and branches. Azure DevOps Server is asked with API 7.1, then 7.0, 6.1, and 6.0, and a server without a collection-wide list is read one team project at a time. Search on a repo set and on a schedule filters those projects by name or URL.

A failed build still pushes and opens a merge request when this run moved that repository. Delivery and the queue use each repository's own branch, including branches written in the issue description. The shared folder stays until every merge request recorded on the job is merged or closed, and it also stays when you cancel a finished run. A later single-repo job does not resume the multi-repo chat. A rejected push names the repository, and a second push to the same merge request keeps the new commit.

# Yaver 0.9.63

The Completed total on Analytics opens a jobs list that includes a finished plan, which is the same set that total already counts. Back to Analytics returns to the period you were viewing, and a custom range keeps the same from and to.

# Yaver 0.9.62

Jobs, schedules, and saved sessions stay on the dashboard when the search index cannot rebuild, because Yaver reads the JSON files instead of an empty index.
Stopping a job and a worker finishing it no longer overwrite each other, so a cancelled queue row stays cancelled.
Scheduling that issue again starts a new job even when the old queue row was still marked running, and that run shows up in Jobs.
Opening the Queue tab loads the waiting rows immediately.
Saving projects in Settings keeps each repository's source branch.
A failed plan, after the ticket is sent back to To Do, no longer puts the implement label back on the ticket.
GitLab merge requests that do not name a Jira key now include the GitLab host in the fallback key, so two servers with the same project path no longer share one plan.
An Azure review of a file such as .github/workflows/ci.yml is posted on that file.
Cancelling a job removes the token from the clone's origin URL.
Installing OpenCode, Codex, or Claude Code renames the binary already on PATH with today's date and copies the new binary into that same directory.
Linux releases include yaver-clis-linux-x64, with the same three CLIs as the Windows CLI zip.
Jira answer comments name OpenCode, Codex, or Claude Code, and the dashboard no longer warns when the Jira poller is turned off.

# Yaver 0.9.61

A Claude job that resumes an earlier run now shows that run's prompt and log in the transcript, then the short continue line and the new reply.
Choosing Claude in Settings lists the models served at ANTHROPIC_BASE_URL, and any model saved in Claude's own settings.
An API retry in the transcript shows the attempt count, the HTTP status, how long Claude will wait, and the request id when Claude sends one.
Jira, GitLab, and Azure comments name the backend, OpenCode, Codex, or Claude Code, on the same line as the version and model.
Opening a collapsed tool row in the transcript no longer traps the mouse wheel.

# Yaver 0.9.60

Jira keys written in a job title or description, and on the issue page, now open that ticket on the configured Jira host.
A key that is already inside a URL stays as text, and GL- and AZ- ids stay as text because those are Yaver's own GitLab and Azure keys.
A new build ticket finds a plan that was saved for Claude on the same repository, source, and target.
A Claude result marked as an error is the text on the job and in the Jira comment, instead of a generic execution failure.
AskUserQuestion from the turn that was nudged does not make the next finish look like another question.
The Claude transcript keeps the sentence after a closed error object, keeps the message on a failed tool result, and shows each tool result on its own tool when one turn calls two tools.
Diagnostic zips redact Anthropic and Codex key values, the dashboard password, and an Authorization Basic header copied from the daemon log.

# Yaver 0.9.59

Claude Code is now a third unattended worker beside OpenCode and Codex.
A plan, build, test, or review job can run claude in print mode with the derman agents, stream the transcript to the dashboard, and resume that same session later.
The dashboard shows Claude jobs in the worker views.
The offline CLI zip installs pinned OpenCode, Codex, and Claude Code, and install-agents copies the derman agents into both homes.
OpenCode, Codex, and Claude each keep their own session for the same repository, branch, target, and kind.
A Claude resume uses the Claude continue prompt, and the one follow-up after a question stays on that process.
A stream that goes silent is stopped, and the session id from the first line is kept so the next run can resume.
The reviewer agent cannot edit files or run a shell, and the cost on the Claude result line is stored on the job.
A plan command on an Azure work item is kept when the same save also changes state.
Jira comment paging continues while total says more comments remain.
A failed disk write no longer looks saved, a cancelled queue row stays cancelled, and Analytics search treats percent and underscore as literal text.
If the SQLite index write fails, Jobs, schedules, and sessions read the JSON files.
Diagnostic zips redact environment names that contain KEY, including CODEX_API_KEY.
Job transcripts can be read from the legacy .jira-agent folder.
Azure Boards jobs are labeled Azure Boards, a Claude user line is labeled You, and a multiline Claude reply is one transcript row.

# Yaver 0.9.58

A failed push used to leave unpushed commits and dirty files in the reused clone, so the next push was rejected.
Yaver now hard-resets that work tree before the next job, fetches the remote branch when it exists, and recreates the branch from the target when it does not.

# Yaver 0.9.57

Implement and Revise on a plan-ready job write the same Jira label or Azure work-item comment the poller and webhook already accept, and the next poll does the work.
Revise after Implement removes plan_execute as well as plan_ready, so the next poll revises instead of starting the build.
Only the newest job for a ticket shows Plan ready. Older plan rows say Superseded.
The current plan file is a Plan tab on that job, in the same row as Prompt.
If that revision fails, Revise stays on the latest error plan job. It reopens the ticket to plan_ready and queues another revision from the plan file still on disk.
A missing {params} block no longer says the ticket moved to In Progress when the board stayed on To Do.
Invalid Mode errors list plan, build, and test.
On Windows, a client that disappears during accept no longer closes port 8080. The poller keeps running and the dashboard accepts the next connection.

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
| `yaver-executables-*.zip` | Windows and Ubuntu 18.04, 20.04, 22.04, and 24.04. One versioned zip for each system is inside this file. |
| `yaver-windows-x64-*.zip` | Windows x64. The same Windows zip is also inside `yaver-executables-*.zip`. |

The Ubuntu executable zips are inside `yaver-executables-*.zip`. Their names are `yaver-linux-x64-ubuntu-18.04-<version>.zip`, `yaver-linux-x64-ubuntu-20.04-<version>.zip`, `yaver-linux-x64-ubuntu-22.04-<version>.zip`, and `yaver-linux-x64-ubuntu-24.04-<version>.zip`. Upload that one file to the office release site. The version is in those names.

Each archive is an **onedir** folder:

- `yaver.exe` / `yaver` — CLI + daemon
- `_internal/` — bundled Python runtime, SPA, prompts
- `opencoderman/agents` + `opencoderman/skills` — derman-build, derman-plan, skills
- `install-agents.bat` (Windows) or `install-agents.sh` (Linux)
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
install-agents.bat    # Windows
./install-agents.sh   # Linux
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
