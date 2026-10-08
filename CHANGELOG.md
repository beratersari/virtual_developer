# Changelog

All notable changes to Yaver are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [SemVer](https://semver.org/) from the repo root `VERSION` file.
GitHub Releases are cut from tags `vMAJOR.MINOR.PATCH`.

## [Unreleased]

A fresh install that leaves `YAVER_BASE_DIR` unset keeps data in `C:\yaver_data` on Windows and `~/yaver_data` on Linux. Data is `{base}/yaver` and clones are `{base}/t`. A path already written in `.env` stays, including `YAVER_DATA_DIR` and `TEMP_DIR_BASE`. Packages already published, through 0.9.81, keep the default they shipped with.

### Added

- The version check is a GET and does not send Analytics. `GET /api/analytics/install` returns this install’s jobs, merge requests, and the other Analytics counts to the release site when an admin selects this address. That path checks a built-in token. It is not a setting. `GET /api/analytics` stays behind the dashboard password.

### Changed

- When `YAVER_BASE_DIR` is unset, the data folder is `C:\yaver_data` on Windows and `~/yaver_data` on Linux. The Linux release package leaves that key commented. It no longer writes `YAVER_BASE_DIR=/var/tmp/yaver`.
- `JIRA_ENABLED=false` stops the board poller. Plan, progress, error, and completion comments still post when `JIRA_HOST` and `JIRA_API_TOKEN` are set, including a scheduled issue and a newly created issue. With no host or token, those comments stay off.

## [0.9.81] — 2026-10-08

Opening a scheduled ticket shows its prompt and repositories before a run exists. Settings action rows use the shared buttons. A banner names a newer published package and explains the update script.

### Added

- The dashboard checks the release site about every 15 minutes. When a newer package is published, a banner at the top names that version. The info button explains how to run `update.bat` or `./update.sh` in the Yaver folder. The dashboard does not download the package.

### Fixed

- Opening a scheduled ticket shows its prompt, repositories, branches, mode, model, and time. The jobs list says the run has not started yet.
- Settings Test, Remove host, Remove collection, Add host, Add collection, and Reload from tokens use the same buttons as the rest of the dashboard.

[0.9.81]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.81

## [0.9.80] — 2026-10-07

A tagged release attaches one zip of the Windows and Ubuntu executables, and that zip carries the release note. Analytics status filters show every count.

### Changed

- A tagged release attaches `yaver-executables-X.Y.Z.zip`. That file contains `yaver-windows-x64-X.Y.Z.zip` and `yaver-linux-x64-ubuntu-18.04-X.Y.Z.zip` through `24.04`. Those Ubuntu zips are no longer separate files on the release page. The office site takes this one zip and still offers each system as its own download.
- That same zip contains `RELEASE_NOTES.txt`, the `# Yaver X.Y.Z` section from `packaging/RELEASE_NOTES.md`. The office site reads that file and shows it as the release note for the version.

### Fixed

- Analytics status filters show the job count beside Completed, Error, and Cancelled, the same way Executing already does.

[0.9.80]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.80

## [0.9.79] — 2026-10-07

`update.bat` and `update.sh` write the published version into `VERSION` and `_internal/VERSION` when either file is still older.

### Fixed

- `update.bat` and `update.sh` write the published version into `VERSION` and `_internal/VERSION` when either file is still older. A blank `RELEASE_HOST` uses `15.210.7.55`. A blank `RELEASE_PORT` uses `8090`.

[0.9.79]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.79

## [0.9.78] — 2026-10-07

Starting a Jira job assigns the issue to the trigger user from Settings.

### Changed

- Starting a Jira job assigns the issue to the trigger user from Settings. The PAT user is used when that name is empty or Jira rejects the assign.

[0.9.78]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.78

## [0.9.77] — 2026-10-07

Scheduled merge requests and pull requests can run as a review. Settings Board ID accepts several Jira boards. Windows `update.bat` copies folders with robocopy and writes the published version.

### Added

- Scheduled merge requests and pull requests can run as a review. On Scheduled → MR, Mode **GitLab review** starts a GitLab review. On Scheduled → PR, Mode **Azure review** starts an Azure review. Existing issues and new issues stay on plan, build, and test. An empty model uses the review model from Settings.
- Settings Board ID accepts several Jira boards, comma-separated (for example `2, 5`). The poller reads each board. Each Scrum board still uses only its first active sprint. A failure on one board leaves the others in that cycle, and an issue that sits on two boards is taken once.

### Fixed

- Windows `update.bat` copies program folders with robocopy. It writes the published version into `VERSION` and `_internal/VERSION`, including when the package still has the previous text in those files. Copy the new `update.bat` from this zip into the Yaver folder once before you run it. The script that is already running is left in place.

[0.9.77]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.77

## [0.9.76] — 2026-10-07

The Windows offline package and the OpenCode, Claude Code, and Codex zips are built again.

### Fixed

- The Windows dist deleted the downloaded OpenCode, Claude Code, and Codex binaries, then tried to pack those CLI zips from the missing files. The zips are packed first, and the download cache is removed after that.

[0.9.76]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.76

## [0.9.75] — 2026-10-07

A finished GitLab review shows Re-request review. Update an install by running `update.bat` or `./update.sh` in the Yaver folder.

### Changed

- A finished GitLab `/review`, `/ask`, or `/yaver` reply is marked reviewed, so the merge request shows Re-request review. `/ask` posts the overview and leaves approval to `/review`. When a `/review` findings JSON is empty, Yaver approves the merge request and still posts the overview note.
- OpenCode, Claude Code, and Codex release zips are named with that tool's version. A Windows OpenCode 1.18.10 package is `yaver-opencode-windows-x64-1.18.10.zip`. Claude Code and Codex use the same pattern, and the Linux names do too. The Codex zip packs the file named `codex.exe`.
- Update an executable install from the Yaver folder. On Windows run `update.bat`. On Ubuntu run `./update.sh`. The script reads `RELEASE_HOST` and `RELEASE_PORT` from `.env`, checks the package, stops Yaver in that folder, and replaces the program files (`yaver.exe` or `yaver`, `_internal`, `.env.example`, `opencoderman`, and `install-agents.bat` or `install-agents.sh`). `.env` and the script you ran stay. Start Yaver after the line that begins with Updated to.
- An install that does not have `update.bat` or `update.sh` yet can take the script from the published zip: download the zip once, copy that script into the Yaver folder, and run it there.
- `yaver update` and `yaver --update` print that instruction and leave the install unchanged.
- Settings Sync copies agent files into the OpenCode and Claude homes and reloads OpenCode when no job is running. Saving or creating an agent writes the catalog and leaves OpenCode running. The separate Reload OpenCode button is gone.

[0.9.75]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.75

## [0.9.74] — 2026-10-06

A finished review is no longer retried when the only leftover is a todo stuck in progress. The sidebar names each OpenCode state.

### Fixed

- A review, plan, or build that stops with finish=stop is complete when no todo is still pending. One todo left in progress is treated as a checkbox the model forgot to close. Todos that are still pending stay incomplete, and a build retries to finish them.

### Changed

- The sidebar OpenCode line says "OpenCode healthy, reload waiting" when the process is healthy and a reload is waiting for the current job. A reload in progress reads "OpenCode reloading". The other lines say whether OpenCode is healthy, up but not answering, not running, failed, or the status could not be read.

[0.9.74]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.74

## [0.9.73] — 2026-10-06

Stop Yaver and run `yaver update`. Each command-line tool has its own zip. A merged review no longer leaves its job live.

### Added

- Stop Yaver, then run `yaver update` or `yaver.exe --update`. The command reads `RELEASE_HOST` and `RELEASE_PORT`, or `--host` and `--port`, downloads the package for this computer, replaces the install, and starts Yaver again.

### Fixed

- The updater keeps the executable bit stored in the zip, so a Linux `yaver` can be started after the swap. If that program cannot be started, the previous executable folder is put back.
- The Linux shell helper waits until the dashboard port opens, and puts the previous folder back when the new copy does not. It also refuses to replace files while that port is still open. The Windows PowerShell helper checks the dashboard address from the plan, not only 127.0.0.1. A dashboard address that does not answer is treated as closed after a short wait, so one check cannot use the whole health budget.
- A job whose merge request or pull request was merged or closed no longer stays live. The row is cancelled with the merge error, and that review is stopped.

### Changed

- Settings no longer has Check or Update. The release address stays in `.env`. The dashboard does not download or replace Yaver.
- Windows and Linux releases attach one zip each for OpenCode, Claude Code, and Codex. Each zip is that CLI, its host config, and its install command. The combined `yaver-clis` zip is no longer built.

[0.9.73]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.73

## [0.9.72] — 2026-10-05

Settings can install a package from the office network. The dashboard has a light theme, and the start scripts send the OpenCode password.

### Added

- Settings → Runtime can point at a release server on the office network. Check looks up the Windows package or the matching Ubuntu package. Update closes Yaver, downloads that zip, and starts Yaver again. The `.env` file and the data folder stay, including when that data folder sits inside the executable folder. A git checkout is left alone.
- A tagged release attaches one zip per standalone binary with `latest` in the name: `yaver-windows-latest.zip` and `yaver-ubuntu-18.04-latest.zip` through `yaver-ubuntu-24.04-latest.zip`. Each file is that platform’s executable zip. The version is the `VERSION` file inside it, for upload to the office release site.
- The release site is a separate repository. Its home page lists the current packages and the install steps. The admin page uploads one Windows zip and one zip for Ubuntu 18.04, 20.04, 22.04, and 24.04.
- The dashboard can switch the phosphor console to a light workbench. The choice is saved in this browser. The wink mark is transparent, so the dark theme fills it with navy and the light theme uses the page color.

### Fixed

- On Windows, Update finishes replacing `yaver.exe` and starts it again. The helper stays alive while the running copy is stopped, including when Python is a launcher in front of the real interpreter, so the new folder is not left unfinished. PowerShell is not started as a detached process, because that flag makes `powershell.exe` quit before it reads the update plan. If the new copy does not open its dashboard, that new process is stopped, the previous folder is put back, and that previous copy is started again. This includes the case where the new process was started by the helper and its working directory is the install folder.
- Windows and Linux start scripts read `OPENCODE_SERVER_PASSWORD` from the process environment or `.env` and send it on the OpenCode health check. A 401 stops the wait instead of treating the server as down. The password stays off the command line.
- The start scripts expand `${NAME}` and `${NAME:-default}` the same way the daemon does. A password written that way is the same secret on the next start. Linux import keeps a trailing newline in the password.
- A disabled secondary button uses muted text, a transparent background, and the normal border. A disabled Look up control no longer looks ready.

### Changed

- Standalone executable zips and tar.gz files list `yaver.exe` or `yaver` and the other files at the archive root. The Linux offline zip and tar.gz do the same for `install-dashboard.sh`, `src/`, and `vendor/`.

[0.9.72]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.72

## [0.9.71] — 2026-10-04

OpenCode serve starts with Yaver and reloads when agents change. Storage and Sessions is one tab, and the dashboard names the open issue.

### Added

- The sidebar shows OpenCode serve health at the bottom left on every page. The line reads healthy, restarting, reload waiting, not answering, down, or failed. Hover shows the detail from the serve check. A status that cannot be read stays unavailable.
- The daemon always starts OpenCode serve when it is down, and starts that child again if it exits. A serve that is already healthy is left running. Saving, creating, or syncing an agent copies the catalog into the OpenCode and Claude homes and reloads serve when no job is planning or executing. A running job keeps the current process until it finishes. Settings has Reload OpenCode for a change made outside the editor.
- Storage and Sessions is one sidebar tab. Details on a folder lists the OpenCode chats whose working directory is that clone or a checkout inside it. An old /sessions address opens Storage. Claude Code and Codex replies stay on the job Transcript tab.

### Fixed

- Storage and Sessions lists OpenCode chats whose clone folder was deleted. Age delete keeps the session so a later job can resume it. Those chats stay on the page, with Reset, after the folder is gone.
- Restarting Yaver while an agent save is still waiting reloads OpenCode before the next job starts. The running serve is kept only until that restart; the saved agents are not left behind in the old process.
- Saving or creating an agent while a job is running leaves the jobs that are still queued where they are. Those jobs start after OpenCode reloads, so they see the new agent. The running job keeps its current serve process.
- A failed OpenCode reload leaves queued jobs queued. They start only after a later reload succeeds and the new process is using the saved agents. The queue also stays put when that reload state cannot be read.
- OpenCode serve stays up when the work queue cannot be read. A locked or unreadable queue is not treated as an empty queue. A queue database that never opened is the same case, because listing it returns no rows and does not raise.
- OpenCode serve started with ``OPENCODE_SERVER_PASSWORD`` stays up. Health checks and job requests send that password. ``OPENCODE_SERVER_USERNAME`` overrides the default user ``opencode``. A 401 from a missing password is not treated as a crashed serve.
- A planning or executing issue still blocks a reload when the issue-state database cannot be read. That read used to look like no jobs, so an agent save restarted serve during clone.
- A missed OpenCode health check no longer stops serve while a job already has a session. The process stays up when it is still listening. A job that starts during a reload waits for that restart, and Yaver does not open a second serve beside it. A job that appears at the moment of a stop keeps the current process.
- A job that has not reached OpenCode fails within a few seconds when serve is listening but does not answer. That opening check does not use the agent time budget. The job leaves executing, and the quiet process can then be replaced.

### Changed

- The dashboard is a dark phosphor console with square controls. The top bar shows the open issue name, the same name as the browser tab.
- List pages no longer repeat their name in a kicker. Job pages no longer repeat the issue name, status, and worker. Settings and storage keep the rules that stop a bad action.

[0.9.71]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.71

## [0.9.70] — 2026-10-03

A follow-up on one repository of a multi-repo job keeps the other repository on `feature/{KEY}`. Dashboard pages keep a connection free, and storage labels every review `!N`.

### Fixed

- A comment on one repository of a multi-repo job checks the other repository out on `feature/{KEY}` when its saved source is a primary base (`develop`, `main`, `master`, `trunk`, `dev`, or `release/*`). The follow-up reuses the first run's folder, so that repository's feature commits stay in the workspace. The commented repository stays on the branch named in the review.
- Dashboard pages no longer stay blank or look empty while an earlier read still holds the browser's connections. Leaving a page cancels that page's read, a live update waits for the read already in flight, and Jobs, Sessions, and Scheduled stay on Loading until the response for the list you opened arrives.
- Storage review links use `!N` for every merge request on a folder, including a multi-repo folder. The repository path stays in the link target.
- A `file:` clone is allowed when a GitLab or Azure token is configured. That remote has no hostname and does not receive a token.

### Changed

- On Linux, Yaver and OpenCode serve run as the same user. The unit sets `User=` and `Group=`. After a root run, `chown` the data and OpenCode trees once, then start both as that user. Port 8080 does not need root.

[0.9.70]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.70

## [0.9.69] — 2026-10-02

Jobs, schedules, and issue state live in one database. The storage page lists every merge request on a multi-repo folder, and copying a dashboard address pastes the page you have open.

### Added

- Jobs, the queue, schedules, session binds, and issue state are stored in `yaver.sqlite` in the Yaver data folder. The first start imports the JSON files that were already there. Plans, session logs, and clone folders stay on disk. Analytics still counts a run after merge cleanup deletes the issue state, because the job row remains.

### Fixed

- Copying a dashboard address pastes the page you are on. A job link uses that job's title, an issue link uses the issue title, and the other screens use their own names, such as New issue, Settings - Jira, Jobs - In flight, and Analytics.
- Saved projects and repo sets are stored in `saved_catalog.json` in the Yaver data folder. The dashboard shows that list after a refresh. Saving another setting does not replace it. Reload from tokens still imports the repositories the GitLab and Azure tokens can read.
- The storage page lists every merge request on a multi-repo folder. Each delivery keeps its own link and state. Updating one review does not drop the others.
- The job log lists the commits that are about to be pushed, then the text git prints while the push runs.
- Stopping a review whose issue state was removed by merge cleanup cancels that job. A restart no longer leaves the row executing. The job row stays for Analytics.

[0.9.69]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.69

## [0.9.68] — 2026-09-30

Review and test follow-ups stay on their own job. The Sessions page lists OpenCode chats, and stopping a job aborts the OpenCode session.

### Fixed

- A review or test timeout, error retry, clarifying-question nudge, or idle continue stays on that job. A review follow-up finishes the review. A test follow-up finishes the unit tests and may commit those tests. A build job still receives the build continue line.
- Stopping a job aborts its OpenCode session, including when the configured worker is Codex. Shutting Yaver down waits for that abort.
- Codex keeps the thread id from the start of the run when a later message quotes another id.
- A Codex job does not resume a Claude chat from another branch of the same issue. A Codex thread on another branch of that issue still resumes.
- The Sessions page lists OpenCode chats in one read. A multi-repo row shows the session id, the repositories, and the clone folder. Reset names that one session, and the other sessions on the same branch stay. Claude and Codex replies stay on the job Transcript tab.

### Changed

- The example agent timeout in `.env.example` is 14400 seconds. An install that already saved a timeout keeps that saved value.

[0.9.68]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.68

## [0.9.67] — 2026-09-30

The Jobs Queue tab shows waiting items when you open it. Settings keep the values you saved, and saved projects stay hidden until Reload from tokens.

### Fixed

- The Jobs Queue tab shows waiting items when you open it. Switching to Queue keeps the list you already loaded, and a new waiting item shows up without waiting for the next poll. Finished queue history is not read again on each visit.
- Settings values stay on what you saved. Saving another setting, then restarting Yaver, no longer puts the old `.env` line back for Board ID, poll interval, concurrent jobs, clone age, agent timeout and retries, default model, review model, worker, Jira/GitLab/Azure trigger names, or the Azure webhook switch. An older Jira host stored in Settings does not replace the host in `.env` on that restart.
- Saved projects stay hidden until you press Reload from tokens. Opening Yaver, Scheduled, or Settings does not load that list. A save before that reload does not replace the stored projects.
- Code review jobs are counted in the job tables. Merge request cards and the merge request list count ticket work and /yaver follow-ups.
- Saved projects are no longer capped. A token reload keeps every repository the tokens can read, and saving that list keeps every row.

[0.9.67]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.67

## [0.9.66] — 2026-09-30

A follow-up on one repository of a multi-repo job keeps every repository on its own branch. Each schedule row can use the issue key, and a resumed chat keeps the new instruction.

### Added

- Schedule repositories are a list. The pencil edits one row, the trash icon removes it, and adding a repo set appends to the repositories already on the form.

### Fixed

- Schedule dispatch keeps each repository's source and target. A follow-up on a later merge request or pull request prepares the other repositories on their own branches, in the same shared folder.
- A job that already stored a blank first row fills those branches from the issue description on the next follow-up.
- A GitLab or Azure follow-up names the reviewed repository's branch in the prompt and records that branch. The prompt lists each clone.
- A repository marked with the issue key gets `feature/{KEY}` on every repository, including ones after the first. This applies to new tickets, existing tickets, and Azure work items.
- A plan timeout or compact continue keeps the plan instruction.
- A plan revise, a review comment, a test run, and a merge-request follow-up keep that instruction when Codex or Claude Code resumes the chat. A second run of the same build still uses the short continue line.

[0.9.66]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.66

## [0.9.65] — 2026-09-29

A review comment on a multi-repo job delivers only that repository. Settings can search and delete saved projects, and Analytics uses the width of the page.

### Added

- Saved projects can be searched by name. Select all applies to the visible rows, and Delete selected removes them after confirmation.
- Analytics shows a category mix beside the jobs chart.

### Fixed

- A follow-up on one merge request or pull request pushes only the matching repository and reuses that review. A push of the shared folder fails, because that folder is not a git repository. The other repositories stay local.
- A one-repository rework clears the stored repository set. An explicit list with fewer than two repositories clears it even when the ticket text still names the old pair. A description that does not parse, and that does not include a repository list, leaves the set in place.
- A comment on a review this job opened stays on that issue when the title has no key or names another ticket. Each delivery stores the review id and the target branch.
- Merging one review of a multi-repo job keeps the plan and the local issue until every recorded review is done.
- Settings save accepts the project list the token import stores.
- Opening Settings → Projects keeps the saved list. Reload from tokens is what imports repositories.
- Schedule and repo-set fields find a saved repository by name or URL.
- Analytics tables stay on the page, and the jobs chart keeps a fixed height so the hover marker stays on the point under the pointer.
- The standalone executable loads its agents from the folder beside the program.

[0.9.65]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.65

## [0.9.64] — 2026-09-29

A dashboard job can work in several repositories at once. Each one keeps its own branch, commit, and merge request. Settings lists the Git repositories your GitLab and Azure tokens can read.

### Added

- A saved repo set, or extra repositories on a schedule, clones each repository, pushes it, and opens its own merge request. The job and issue pages show each repository's commit and merge request.
- Each repository on a schedule has its own source and target. The issue description lists every repository.
- Schedule forms put the repositories and the worker together. One repository runs as a single job. Two or more run as one multi-repo job. Mode, backend, and model sit in that same form.
- Repo sets and saved projects are lists. Plus adds a row, the pencil edits it in a popup, and the trash icon removes it.
- Settings → Projects loads every Git repository the saved GitLab and Azure tokens can read. A project you already saved keeps its label and branches. Azure DevOps Server is asked with API 7.1, then 7.0, 6.1, and 6.0. A server without a collection-wide list is read one team project at a time.
- The repo-set popup and the schedule repository pickers filter saved projects by name or URL.

### Fixed

- A failed build still pushes and opens a merge request when this run moved that repository's HEAD. Each clone is compared with its own start.
- Delivery counts each repository on its own work branch, including branches named in the issue description.
- The queue locks every repository in the set, so a job on a later repository does not start on the same branch.
- A multi-repo folder stays until every recorded review is merged or closed. The hourly age purge still deletes an unchanged folder after the configured number of days.
- Cancelling a finished multi-repo folder keeps it. A child that never finished cloning still deletes the set.
- A later single-repo job does not resume or replace the multi-repo chat on the first repository.
- A rejected push names the repository that failed.
- A second push to the same merge request keeps the new commit.

[0.9.64]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.64

## [0.9.63] — 2026-09-28

The Completed total on Analytics opens a jobs list that includes a finished plan. Back to Analytics returns to the same period, including a custom range.

### Fixed

- The completed jobs list and index include a `plan_ready` job, matching the Analytics completed total. The Plan ready pill still lists only those jobs.
- Back to Analytics opens `/analytics/7d` (or `/analytics/custom` with the same from and to) instead of a query the chart ignores.

[0.9.63]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.63

## [0.9.62] — 2026-09-25

Jobs, schedules, and session resume stay available when the search index cannot rebuild.
A cancelled issue can be scheduled again and shows up in Jobs.
The Queue tab loads its rows when you open it.
Linux releases include the CLI zip, and installers keep the previous binary with today's date on the name.

### Added

- Linux releases include `yaver-clis-linux-x64-*.zip` with OpenCode, Codex, and Claude Code.
- Installing a CLI finds the binary already on PATH, renames it with the date at the end, and copies the new one into that same directory.

### Fixed

- A failed job, schedule, or session index rebuild no longer hides the JSON files behind an empty index.
- Stop and a finishing worker no longer overwrite each other's queue status.
- Scheduling an issue after Stop starts a new job when the old queue row was still running.
- Opening Queue loads the waiting rows immediately.
- Settings keeps each project's source branch.
- A failed replan does not put `plan_execute` back on the ticket.
- GitLab fallback keys include the host and no longer treat `acme/demo` and `acme-demo` as the same issue.
- Cancel removes the token from the clone's origin URL.
- An Azure review of `.github/...` is posted on that file.
- Jira answer comments name the backend.
- The dashboard hides the notice that appears when the Jira poller is turned off.

[0.9.62]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.62

## [0.9.61] — 2026-09-25

A resumed Claude job shows the original prompt, not only the continue
line. The Claude model list comes from the server. Reply comments name
the backend.

### Added

- Choosing Claude lists the models from ANTHROPIC_BASE_URL, plus any
  model saved in Claude's own settings.

### Fixed

- A later Claude run keeps the first prompt and the earlier log in the
  transcript. The model still receives the short continue line.
- An API retry in the transcript shows the attempt count, HTTP status,
  wait, and request id.
- Jira, GitLab, and Azure comments name OpenCode, Codex, or Claude
  Code next to the version and model.
- Opening a collapsed tool row in the transcript no longer traps the
  mouse wheel.

[0.9.61]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.61

## [0.9.60] — 2026-09-25

Jira keys written in a job title or description open that ticket.
A later build finds a plan that was saved for Claude.
Claude API errors show up on the job, and a finished turn is not
treated as another question.

### Added

- Each Jira key in a job title, a job description, an issue title, or
  an issue description links to that ticket on the configured Jira
  host. A key already inside a URL stays text. GL- and AZ- ids stay
  text.

### Fixed

- A plan bind stored with a backend is found when a new ticket builds
  the same repository, source, and target.
- A Claude result marked as an error is the failure text on the job
  and in the Jira comment.
- AskUserQuestion from the turn that was nudged does not stay on the
  next finish.
- Text after a closed error object stays in the Claude transcript. A
  failed tool result keeps its message. Two tool calls in one turn
  show each result on its own tool.
- Diagnostic zips redact Anthropic and Codex key values, the dashboard
  password, and an Authorization Basic header from the daemon log.

[0.9.60]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.60

## [0.9.59] — 2026-09-24

Claude Code is a third unattended worker beside OpenCode and Codex.
Jobs run `claude` in print mode with the derman agents, stream the
transcript, and resume that session later. The dashboard shows those
jobs in the worker views. The offline CLI zip installs pinned
OpenCode, Codex, and Claude, and install-agents copies the derman
agents into both homes.

### Added

- Claude Code backend for plan, build, test, and review. One question
  gets a single follow-up on the same process. A silent stream is
  stopped, and the session id from the first line is kept for resume.
- Claude jobs, transcripts, and the worker picker on the dashboard.
- Pinned offline CLI installers for OpenCode, Codex, and Claude Code.
  install-agents copies the derman agents into the OpenCode home and
  the Claude home.

### Changed

- OpenCode, Codex, and Claude each keep their own session for the
  same repository, branch, target, and kind. A Claude resume uses the
  Claude continue prompt. The reviewer agent cannot edit files or run
  a shell.
- The cost on the Claude result line is stored on the job.

### Fixed

- A plan command on an Azure work item is kept when the same save
  also changes state. Jira comment paging continues while total says
  more comments remain.
- A failed disk write no longer looks saved. A cancelled queue row
  stays cancelled. Analytics search treats % and _ as literal text.
  If the SQLite index write fails, Jobs, schedules, and sessions read
  the JSON files.
- Diagnostic zips redact environment names that contain KEY, including
  CODEX_API_KEY.
- Job transcripts can be read from the legacy .jira-agent folder.
- Azure Boards jobs are labeled Azure Boards. A Claude user line is
  labeled You. A multiline Claude reply is one transcript row.

[0.9.59]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.59

## [0.9.58] — 2026-09-23

A reused clone no longer keeps leftover commits or dirty files from a
failed push. The next job starts from a clean work branch.

### Fixed

- Before the next job, the work tree is hard-reset. The remote branch
  is fetched when it exists, and recreated from the target when it
  does not.

[0.9.58]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.58

## [0.9.57] — 2026-09-23

A plan-ready job can be implemented or revised from the dashboard.
Only the latest plan for a ticket shows Plan ready, and that page
has a Plan tab. A failed revise can be revised again. On Windows the
dashboard keeps listening after a dropped connection.

### Added

- Implement and Revise on a plan-ready job. They write the same Jira
  label or Azure work-item comment the poller and webhook already
  accept. The next poll does the work.
- The current plan file is a Plan tab on the latest plan-ready job,
  in the same row as Prompt.
- Revise on the latest plan job when that job is error. It reopens
  the ticket to plan_ready and queues another revision from the plan
  file still on disk.

### Changed

- Only the newest job for a ticket shows Plan ready. Older plan rows
  say Superseded.
- Revise after Implement removes plan_execute as well as plan_ready,
  so the next poll revises instead of starting the build.

### Fixed

- A missing `{params}` block no longer says the ticket moved to In
  Progress when the board stayed on To Do. The hint matches the column.
- Invalid Mode errors list plan, build, and test.
- On Windows, a client that disappears during accept no longer closes
  port 8080. The poller keeps running and the dashboard accepts the
  next connection.

[0.9.57]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.57

## [0.9.56] — 2026-09-22

Schedules and Sessions use a SQLite index. Looking up an existing
issue fills the same fields as a new issue. The Analytics chart shows
exact counts on hover. Merge-request failures include the remote
error. Submodules update after the work branch is checked out.

### Added

- Local SQLite indexes for schedules (`schedules.sqlite`) and OpenCode
  session binds (`opencode-binds.sqlite`). Created on first daemon
  start, including when the JSON files are already there. JSON remains
  the full record. The Sessions page lists every live bind.

### Changed

- Looking up an existing issue puts repository, source, target, and
  mode in the same fields as a new issue. The prompt box does not
  include the `{params}` block. Schedule or Run now writes those
  fields back to Jira only when they changed. The source choice is
  labeled custom branch.
- Hovering a point on the Analytics jobs chart shows the time bucket
  and the exact count for each series that is turned on.

### Fixed

- When a merge request cannot be created, the job log and the Jira
  comment include the remote status and the server message.
- Submodules are updated after the work branch is checked out, so the
  pins match the branch the job edits.

[0.9.56]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.56

## [0.9.55] — 2026-09-22

Analytics and the Jobs list read a local SQLite index. Data lives
under one per-user folder. The chart step follows the time range.
The mark is phosphor green.

### Added

- Local SQLite job index (`{YAVER_BASE_DIR}/yaver/jobs.sqlite`) for Analytics and
  Jobs list/count. Created on first daemon start. Full `job_*.json` files
  stay on disk. No extra install; each computer keeps its own file.

### Changed

- Storage is one folder, `YAVER_BASE_DIR`. The default is a per-user
  directory: `%LOCALAPPDATA%\Yaver` on Windows and `~/.local/share/yaver`
  on Linux. Yaver keeps its data in `{base}/yaver` and temp clones in
  `{base}/t`. An existing `YAVER_DATA_DIR` or `TEMP_DIR_BASE` still wins.
- The Jobs list still opens `job_*.json` for the current page, so error
  text, delivery status, and the session id stay on the row.
- The Analytics chart step is chosen from the time range. There is no
  separate Hour / Day / Week / Month control. A long range is not drawn
  as hours.
- The Yaver mark is phosphor green, and the in-app wink is 64 pixels.

[0.9.55]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.55

## [0.9.54] — 2026-09-21

GitLab review jobs keep the overview comment when an inline
finding cannot be posted. Linux scripts are LF for WSL.

### Fixed

- A GitLab review keeps the overview comment when an inline finding
  cannot be posted. That skip is logged and no longer fails the MR job.
- Linux install and start scripts are stored as LF, so WSL bash can
  run them.

[0.9.54]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.54

## [0.9.53] — 2026-09-21

Analytics splits MRs Yaver opened from MRs we only commented on.
Cards open the unique-MR list. Search and issue-key filters are gone.

### Added

- Analytics Open / Merged / Closed cards go to a list of unique MR/PR
  links. Counts split into **Opened by us** (Jira / Azure Boards) and
  **Contributed** (existing GitLab MR or Azure PR comments). Review and
  build jobs on the same URL still count once.

### Changed

- Analytics no longer has Search or Issue key filters.

[0.9.53]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.53

## [0.9.52] — 2026-09-21

Selected code on GitLab and Azure review comments reaches the agent.
Scheduled list pages. Analytics counts unique MRs.

### Added

- Analytics Open / Merged / Closed / Total unique merge-request cards
  from stored job history.
- Scheduled jobs list paginates like Jobs (page size 25).

### Fixed

- GitLab inline ``/yaver`` (and ``/review`` / ``/ask``) on a selected
  range puts file, lines, and the clone snippet in the prompt. A reply
  on that thread loads the original range and earlier notes.
- Azure PR file-thread comments load file, lines, and parent comments
  from the thread API (the webhook does not send them).

[0.9.52]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.52

## [0.9.51] — 2026-09-21

A second GitLab ``/yaver`` on the same MR no longer fails to push.

### Fixed

- Queued follow-up jobs on an existing MR fetch the first job's push and
  rebase onto it before delivering. The local branch no longer stays
  behind origin (``tip of your current branch is behind``).

[0.9.51]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.51

## [0.9.50] — 2026-09-21

Analytics no longer hangs. Issue key is exact. Azure /yaver on a
waiting plan posts a note.

### Fixed

- Live ticks no longer abort an in-flight Analytics GET (that looked
  like a hang or Request timed out). Changing period or filters still
  cancels the previous GET. The daemon stops a cancelled walk and
  returns 504 if aggregation exceeds 55s.
- Analytics Issue key is an exact match (``KAN-24`` no longer pulls
  ``KAN-240``). Search still contains. Comma-separated keys are allowed.
- Azure ``/yaver`` on a ``plan_ready`` ticket posts the same wait note
  GitLab already posts on the MR. Implement still waits for
  ``plan_execute``.
- GET ``/api/analytics`` runs off the event loop so Jobs and Stop stay
  responsive.

### Changed

- Plan ready and in flight are first-class outcomes on cards, chart,
  and tables. Repository URLs with or without ``.git`` count as one
  repo. Jobs with no model appear as ``(unset)`` so shares sum to 100%.
  There is a By agent table and a repository facet. Custom range copies
  the current from/to instead of jumping to all-time.

[0.9.50]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.50

## [0.9.49] — 2026-09-20

Analytics no longer times out when you change the chart bucket.

### Fixed

- Switching period or bucket (for example 24 hours then Month) cancels
  the previous Analytics GET instead of showing Request timed out.
  The request budget is 60s. Bucket series is capped so a coarse
  bucket cannot hang the handler.

[0.9.49]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.49

## [0.9.48] — 2026-09-20

Analytics page. Merged reviews keep job history. Plan tickets survive /review.

### Added

- Dashboard Analytics (``/analytics``) charts job counts over time with
  filters for status, model, category, source, and more.

### Changed

- When a GitLab MR or Azure PR is merged or closed, Yaver still deletes
  the clone, session logs, and OpenCode rows, but keeps ``job_*.json``
  so Analytics can count those runs. Manual Jobs → Delete still removes
  a row.

### Fixed

- ``/review`` and ``/ask`` no longer reset a waiting ``plan_ready``
  ticket; they rebind onto a synthetic GL/AZ key.
- Failed ``plan_execute`` keeps the label retryable instead of renaming
  to ``plan_executed`` before implement completes.

[0.9.48]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.48

## [0.9.47] — 2026-09-20

Windows clone delete no longer wipes plan files.

### Fixed

- Storage Delete and merged GitLab MR / Azure PR clone cleanup no longer
  follow the Windows ``.yaver-plans`` junction into
  ``{YAVER_DATA_DIR}/plans``. Other tickets' plan files stay.

[0.9.47]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.47

## [0.9.46] — 2026-09-20

Merged reviews clean up their jobs and logs. Serve session logs record finish.

### Added

- After each OpenCode ``GET /session/{id}/message``, the job session log
  records the last assistant ``finish`` / ``info.finish`` / ``step-finish``.

### Changed

- When a GitLab MR or Azure PR is merged or closed, Yaver also deletes
  that review's jobs, session logs, plan file, issue state, and OpenCode
  session rows. The shared daemon log is kept. Live jobs are skipped.

[0.9.46]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.46

## [0.9.45] — 2026-09-20

Dashboard uses the new Yaver mark. The sidebar icon winks.

### Changed

- Sidebar, login, and boot show icon A as a short wink GIF.
- README uses lockup C (YAVER / Sanal Geliştirici).

[0.9.45]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.45

## [0.9.44] — 2026-09-20

Sessions workspace detail no longer times out while measuring clone size.

### Fixed

- Workspace detail GET uses the Storage size cache instead of
  walking the temp clone on the request path. The SPA aborts
  GETs at 15s; a real clone on Windows/WSL could take longer.

[0.9.44]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.44

## [0.9.43] — 2026-09-19

Sessions workspace shows MR links.
Sessions list is paginated and searchable.

### Added

- Workspace detail lists every distinct linked GitLab MR
  or Azure PR URL from jobs on that repo + source + target.
- Sessions list pagination (25 per page) and search by
  repository, branch, issue key, or kind.

[0.9.43]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.43

## [0.9.42] — 2026-09-19

Sessions lists one workspace per repo + source + target.
GitLab/Azure queue holds the issue lock.

### Added

- Dashboard **Sessions** lists unique repository + source +
  target workspaces. Click-through shows linked OpenCode
  sessions (plan/build/test), jobs, the temp clone (Storage
  delete when not in use), and plan files when a plan bind
  exists.

### Fixed

- GitLab and Azure jobs started from the webhook queue now
  take the same per-issue lock as the handle path. Stop + a
  leftover ``/yaver`` cannot start a second OpenCode session
  beside the cancelled worker.

[0.9.42]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.42

## [0.9.41] — 2026-09-19

Windows Dist no longer fails when Defender eats opencode.exe.

### Fixed

- Payload assert lists every missing path. If
  ``vendor/opencode-home.zip`` is in the zip, a missing
  ``opencode.exe`` (runner AV quarantine) is a warning, not a
  failed GitHub Release.

[0.9.41]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.41

## [0.9.40] — 2026-09-19

Windows offline zip attaches again.

### Fixed

- Windows Distribution CI disables runner Defender on ``opencode.exe``
  and restores it from ``vendor/bin`` if AV ate the copy under
  ``opencoderman/vendor/bin/windows``. That assert was failing on
  every 0.9.36–0.9.39 Windows zip, so the GitHub Release never got
  the offline zip.

[0.9.40]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.40

## [0.9.39] — 2026-09-19

A compact recap with ``finish=None`` is not a crash and not success.

### Fixed

- OpenCode 1.18 lists the compact recap (``agent=compaction``,
  ``summary=true``) before ``info.finish`` is set, while the UI
  already shows the summary. The work-turn log is
  ``finish='stop' summary=None``. That recap is **incomplete**
  (wait for auto-resume), not an unfinished crash and not
  COMPLETE. Success only after a later non-recap assistant turn.

[0.9.39]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.39

## [0.9.38] — 2026-09-19

Windows offline zip builds again.

### Fixed

- Windows Distribution CI requires
  ``opencoderman/agents/derman-reviewer.md`` (the agent in the
  tree) instead of the old ``code-reviewer.md`` alias, and greps
  current Settings copy so the zip attaches to the GitHub Release.

[0.9.38]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.38

## [0.9.37] — 2026-09-19

Unused clones older than 7 days are deleted. A later mention
reclones and resumes the same OpenCode session.

### Added

- Unused temp clones older than **7 days** are deleted hourly
  (``TEMP_CLONE_MAX_AGE_DAYS``, Settings). Live jobs are never
  removed. Set 0 to turn the policy off. The OpenCode session bind
  is kept so a later mention on the same MR/PR reclones and
  **resumes** that session.

[0.9.37]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.37

## [0.9.36] — 2026-09-19

Dashboard stays up while many clones run.

### Fixed

- Git clone, checkout, push, and clone-delete use a dedicated
  ``yaver-git`` thread pool (size = max concurrent jobs). They no
  longer occupy FastAPI's default executor, so ``/api/jobs`` and
  ``/ws`` do not hang behind ``git clone``.

[0.9.36]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.36

## [0.9.35] — 2026-09-18

Re-request after a failed review starts again. Install copies derman-reviewer.

### Fixed

- GitLab/Azure assign after an **error** no longer reuses the failed
  queue row (``review-update-{iid}``). A new assign/re-request
  enqueues a new job.
- ``install-opencode-agents.bat`` / ``.sh`` copy ``derman-reviewer.md``
  (they only copied build/plan/test).

[0.9.35]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.35

## [0.9.34] — 2026-09-18

Review assign matches aMIR-mini. Separate review model. Jira can be turned off.

### Added

- Separate **Review model** in Settings (``DEFAULT_REVIEW_MODEL``).
  Plan, build, test, and ``/yaver`` keep **Default model**. Empty
  review model still uses Default model. Per-issue ``Model:`` still
  wins.
- Settings **Jira → Enabled**. Off skips the board poller and Jira
  comments (``JIRA_ENABLED=false``). GitLab and Azure jobs still run.

### Fixed

- GitLab and Azure **assign as reviewer** match the PAT user id
  (GitLab ``GET /user``, Azure ``connectionData``), not only the
  trigger-user string. Azure accepts
  ``git.pullrequest.reviewers.update`` like aMIR-mini.

[0.9.34]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.34

## [0.9.33] — 2026-09-18

Azure DevOps Server **2020 Update 1.1** works next to 2022.

### Fixed

- Azure REST calls walk ``7.1 → 7.0 → 6.1 → 6.0``. Server 2022 still
  uses 7.x. Server 2020 Update 1.1 (REST 6.0 only) no longer fails
  after 7.1/7.0. Settings Test, PR comments, threads, and work items
  use the same list.

[0.9.33]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.33

## [0.9.32] — 2026-09-18

Always-on GitLab and Azure **code review**. Jobs use
``derman-reviewer`` and show ``gitlab-review`` / ``azure-review``.

### Added

- MR/PR **code review** is always on. Yaver uses the Creasy review
  rules (not Creasy clone layout): `@bot /review`, `@bot /ask`,
  reviewer assign / re-request, and MR/PR open when the bot is already
  a reviewer. New commits do not re-review. Overview posts when there
  is no thread; thread replies stay in-thread. No push or new MR.
  Work-item `/review` and `/ask` stay silent. A `/review` result
  that includes an `opencoderman-findings` fence opens one inline
  file/line thread per finding (GitLab discussion / Azure file
  thread). `/ask` stays on the request thread only. The OpenCoderman
  review agent is **derman-reviewer** (`code-reviewer` /
  `gitlab-reviewer` remain install aliases). Jobs show
  ``gitlab-review`` / ``azure-review``.

[0.9.32]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.32

## [0.9.31] — 2026-09-17

Azure collection URLs work with or without a ``/tfs`` virtual directory.

### Changed

- Azure collection URLs no longer require a ``/tfs`` virtual directory.
  ``https://host/tfs/DefaultCollection`` and ``https://host/DefaultCollection``
  both save. Host-only and ``/tfs`` with no collection name are still
  rejected. The saved path is kept as entered; ``/tfs`` is not inserted.

[0.9.31]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.31

## [0.9.30] — 2026-09-16

Plan, build, and test keep three OpenCode sessions. A failed
implement can be retried with ``plan_execute``. Queue reap no longer
starts a second job during accept. Settings Projects has no default
source.

### Fixed

- Plan, build, and test keep three separate OpenCode sessions for the
  same repo + source + target. ``Mode: test`` no longer continues a
  ``derman-build`` chat (and the reverse). Dashboard Reset still
  forgets the bind.
- After a failed implement, In Progress + ``plan_execute`` retries
  the same ticket. ``ERROR`` / ``CANCELLED`` no longer ignore that
  label; ``COMPLETED`` still does. To Do + ``Mode: plan`` is still
  re-plan.
- Queue reap no longer treats a live ``PENDING`` accept as a leftover
  after Stop. A finishing job cannot free the same issue for a
  GitLab/Azure ``/yaver`` while the board accept is still talking to
  Jira.

### Changed

- Settings → Projects no longer has a Default source field. New-issue
  source stays ``feature/{KEY}`` unless the operator sets a named
  branch on the schedule form.

[0.9.30]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.30

## [0.9.29] — 2026-09-16

Operator comments drop the “Yapay zekâ” banners. Ticket params stay
multiline. Sidebar footer is one clock, Connected, then stacked buttons.

### Changed

- Jira, Azure, and GitLab operator comments no longer start with
  ``Yapay zekâ — …`` headings.
- New and updated issue descriptions wrap ``{params}`` in ``{code}`` so
  Jira Server/DC shows each field on its own line.
- Azure work-item descriptions post as HTML so TFS keeps those line
  breaks.
- Dashboard sidebar: one local clock, Connected, then stacked
  Report issue and Sign out.

[0.9.29]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.29

## [0.9.28] — 2026-09-16

Azure PAT assign no longer continues unassigned. Schedule collections
and team projects are restored.

### Fixed

- If the collection PAT user cannot be resolved or Assigned To cannot
  be written, Yaver posts an error and does **not** start or schedule
  the job. Identity lookup retries the Settings Test probe last.
- Schedule Existing/New collection dropdowns use saved
  ``AZURE_COLLECTION_PATS`` keys again (no extra Azure keys in ``.env``).
- Team project list for New issue pages past the first 200 projects.

### Changed

- Operator comments on Jira, Azure, and GitLab are Turkish (commands
  such as ``/yaver`` stay English).

[0.9.28]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.28

## [0.9.27] — 2026-09-16

Storage retries Azure PR status after restart instead of sticking on
Unknown.

### Fixed

- Failed live MR/PR lookups are no longer cached as ``unknown``.
  Refresh can load Azure PR status after a daemon restart.

### Changed

- Storage warning when a folder has no GitLab MR or Azure PR URL is
  clearer (Yaver will not auto-delete that clone).

[0.9.27]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.27

## [0.9.26] — 2026-09-16

Azure work items start only on Assigned To and comments.
Settings writes only the collection PAT map.

### Changed

- Azure work-item **updated** hooks start a job only on Assigned To.
  State / Kanban column moves, description, and tags are ignored.
  Comments stay on the comment path.
- Saving Azure credentials writes only ``AZURE_COLLECTION_PATS``
  (same idea as ``GITLAB_HOST_PATS``). It no longer writes
  ``AZURE_COLLECTION_URLS``, ``AZURE_HOST_PATS``, or leftover
  ``AZURE_PAT``.

[0.9.26]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.26

## [0.9.25] — 2026-09-16

Azure work-item webhooks no longer freeze the ops dashboard.

### Fixed

- Work-item decide / fetch / assign / usage-note HTTP runs off the
  dashboard event loop. GitLab and Jira were already non-blocking.
  The Settings Test probe is not used on the webhook path.

[0.9.25]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.25

## [0.9.24] — 2026-09-16

Azure work-item assign uses the Settings Test identity path.
Updated hooks only listen for assignee, comments, and board position.

### Changed

- Azure work-item **updated** hooks only start a job on Assigned To or
  board position (State / Kanban column). Description, title, and tag
  edits are ignored. Comments stay on the comment path.
- `AZURE_TRIGGER_LABEL` is no longer on Settings or `.env.example`.
  Intake stays assignee-only (the field remains in code, always empty).

### Fixed

- Assign to the PAT user no longer fails with ``identity empty`` when
  Settings Test works. Lookup uses ``X-TFS-FedAuthRedirect: Suppress``
  and the same connectionData probe as Test.

[0.9.24]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.24

## [0.9.23] — 2026-09-16

Operator docs cover every intake path. Azure work items start on To Do /
In Progress and the same process-template columns.

### Changed

- Azure work-item intake is **To Do** or **In Progress** and the same
  process-template columns (New, Proposed, Approved, Active, Doing,
  Committed). Jira is unchanged. Resolved and Done still do not start a job.
  After accept the item moves to that type’s In Progress name (Active /
  Doing / Committed / In Progress). After the job finishes it stays there.
- English and Turkish READMEs document Jira, GitLab MR, Azure PR, and
  Azure work-item flows with usage examples.

[0.9.23]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.23

## [0.9.22] — 2026-09-16

Azure/GitLab review comments bind to work items more reliably. The agent
sees ticket **42**; the dashboard still shows ``WIT-BETA-42``.

### Added

- Work-item keys are ``WIT-{PROJECT}-{id}``. PR/MR comments resolve Jira,
  ``WIT-…``, Azure ``#42`` (collection-scoped), then repo/source/target,
  then ``AZ-…`` / ``GL-…``.
- GitLab MRs use the same bind order (Jira first).
- Storage warns when a clone has no linked MR/PR (will not auto-delete).

### Changed

- Agent Ticket / ``{ISSUE_KEY}`` for work items is the numeric TFS id.
  Local state and the dashboard stay ``WIT-…``.

[0.9.22]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.22

## [0.9.21] — 2026-09-15

Azure work-item jobs start on any open column, assign the PAT user, and
clone without a trailing ``.git``.

### Changed

- Azure work-item webhooks accept any open board column (Active, Doing,
  New, …), not only New/To Do. Done/Closed is still ignored. Active → New
  while assigned still does not re-queue.
- New Azure work items, webhook accept, and ``/planExecute`` assign the
  collection PAT user (same as Jira).
- Job detail uses a generic **Description** label for Jira, Azure, MR,
  and PR.

### Fixed

- Work-item comment + History twin hook posts one usage note (180s claim).
- PAT identity is cached per collection URL and token, not shared across
  collections on the same TFS host.
- Clone and ``{params}`` help drop a trailing ``.git`` (TFS ``/_git/``
  rejects it; GitLab still clones).

[0.9.21]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.21

## [0.9.20] — 2026-09-15

### Added

- Scheduled → New issue can create an Azure work item (collection + team
  project + type), same picker as Jira.

### Fixed

- Scheduled Azure work-item lookup loads the PAT from the collection URL
  (empty host no longer 401s).
- Azure credentials are a single collection URL → PAT map
  (`AZURE_COLLECTION_PATS`), like GitLab host PATs.

[0.9.20]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.20

## [0.9.19] — 2026-09-15

Azure Boards work-item intake and Scheduled lookup, aligned with Jira.

### Added

- Scheduled → Existing issue → Azure work item (collection + id). Look up
  works without `{params}`. Schedule / Run now moves to Active and assigns
  the PAT user. Ticket name is the work item id (`42`).

### Changed

- Azure PATs are `AZURE_COLLECTION_PATS` (collection URL → PAT).
  `AZURE_HOST_PATS` is removed.
- Work-item lookup lives on Scheduled only (not Settings).
- Work-item comments are HTML (Jira wiki / PR markdown unchanged).

### Fixed

- Ignore `workitem.updated` from the collection PAT so Schedule/Run now
  cannot start a second job.

[0.9.19]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.19

## [0.9.18] — 2026-09-15

### Added

- Scheduled → Existing issue can look up an Azure work item (collection +
  id), same picker as Jira.

[0.9.18]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.18

## [0.9.17] — 2026-09-15

Azure Boards work items can start jobs via service hooks. Settings only
accept a TFS collection URL (`/tfs/<Collection>`), not a hostname.

### Added

- Azure DevOps Server 2022.2 work-item intake on `POST /yaver/webhook/azure`
  (`workitem.created` / `workitem.updated` / `workitem.commented`) whenever the
  Azure webhook is enabled. New work is first assignment on a New item.
  Active → New while still assigned does not re-queue. After a plan, revise or
  implement with `@mention /planRefactor <prompt>` or `@mention /planExecute`
  (not tags). Mention without those commands gets a work-item usage note.
  Jira / GitLab / Azure PR comments are unchanged.
- Settings → Azure: collection URLs (hostname-only is rejected), optional
  trigger tags, and a work-item lookup (collection + id).

[0.9.17]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.17

## [0.9.16] — 2026-09-15

Issue reports can include several jobs and much more serve/log context.
GitLab/Azure review comments now put the selected file range in the agent prompt.

### Added

- Report issue: multi-job select; zip includes `serve.json`, OpenCode serve logs,
  storage, recent jobs, and safe env. Several jobs land under `jobs/<id>/`.
- GitLab DiffNote and Azure file-thread comments include file, lines, thread,
  and a snippet from the clone.

[0.9.16]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.16

## [0.9.15] — 2026-09-14

Linux standalone `yaver` is one freeze per Ubuntu. The 0.9.14 binary was built on Ubuntu 24.04 and failed on older glibc (`GLIBC_2.38 not found`).

### Fixed

- Standalone Executables ships `yaver-linux-x64-ubuntu-18.04`, `20.04`, `22.04`, and `24.04`. Each is frozen inside that Ubuntu image. Download the archive that matches the host.

[0.9.15]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.15

## [0.9.14] — 2026-09-13

Stuck-job watchdog aborts the live OpenCode session. A cancelled GitLab/Azure worker cannot complete a newer run. Builds do not resume the plan chat.

### Fixed

- Watchdog POSTs `/session/{id}/abort` on the daemon loop before marking ERROR and dropping the clone (dashboard Stop already did this).
- `_complete_work` CAS requires the caller's task/job ids so a cancelled GitLab/Azure stack cannot stamp **COMPLETED** on a later run of the same ticket.
- GitLab/Azure builds no longer resume a `kind=plan` OpenCode session when the MR source is not the plan work branch.

[0.9.14]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.14

## [0.9.13] — 2026-09-13

Dashboard Stop kills leftover tools on macOS. Cancel no longer takes down shared OpenCode serve. Leftover GitLab PAT still works when Azure hosts are set. Stop during clone stays Cancelled. Stop is refused on plan_ready.

### Fixed

- Dashboard Stop on macOS kills clone tools (`pgrep -P`, `ps`/`lsof`, `/var` vs `/private/var`).
- `killpg(getpgid(child))` no longer kills shared `opencode serve` (only the process-group leader).
- Leftover `GITLAB_PAT` authenticates a GitLab remote even when `AZURE_HOST_PATS` is set.
- Stop during clone stays **CANCELLED** (does not stamp ERROR).
- Stop is refused on `plan_ready` so `plan_execute` is not discarded.

[0.9.13]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.13

## [0.9.12] — 2026-09-13

Jobs tab shows how many work-queue items are waiting.

### Changed

- Jobs filter tab shows the waiting count as **Queue (6)** (live from the work queue). GitLab/Azure follow-ups still count while the same ticket is in flight.

[0.9.12]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.12

## [0.9.11] — 2026-09-12

Oracle consult is gone. Only plan, build, and test remain.

### Removed

- Oracle consult path. Tickets no longer route on “should we / architecture / how to” wording. Only `Mode: plan`, `Mode: build`, and `Mode: test` remain. Consultative tickets without a Mode go to plan (same as any other ticket missing Mode).

[0.9.11]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.11

## [0.9.10] — 2026-09-12

Settings can save Jira project keys. Storage Delete works after a job ends. Issue pages, plan_ready MR comments, schedules, and state files no longer mix tickets.

### Added

- Settings **Project keys (JIRA_PROJECTS)** persists comma-separated keys so MR/PR titles like `feat(KAN-12)` bind to that Jira ticket.

### Fixed

- Storage Delete stays enabled after a job ends (session binds no longer mark the clone In use).
- Opening **KAN-1** no longer lists jobs for **KAN-10**.
- `@bot /yaver` on a `plan_ready` MR posts a wait note instead of staying silent.
- A late dropped-accept no longer overwrites **COMPLETED** or **CANCELLED**.
- `create_state` returns the real disk state when a terminal write is refused.
- `GL-KAN-12` and `GL_KAN-12` no longer share one state file.
- A future schedule still blocks To Do intake after 500 newer schedule rows; due rows still fire.

[0.9.10]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.10

## [0.9.9] — 2026-09-12

Storage cannot delete a live job's clone. Dispatching schedules cannot be cancelled. Turkish I matches ASCII trigger names.

### Fixed

- Storage Delete is refused (and the button disabled) while a job owns the clone.
- Dashboard schedule Cancel is refused for `dispatching` so it cannot abort a live job.
- Assignee `İrem` matches trigger `irem` (Turkish dotted/dotless I).

### Notes

- Jira Cloud ADF / smart-link / mention-chip gaps are out of scope (on-prem Server/DC).
- GitLab REST MR create/list stays HTTPS even when the clone URL is HTTP.
- Re-adding `plan_refactor` without a new `@bot` comment reuses the latest mention.

[0.9.9]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.9

## [0.9.8] — 2026-09-11

Reply headers always include ``job_id``. Queue claim scan is 1000.

### Fixed

- MR/PR/Jira reply headers always include ``job_id`` (Creasy: `**Yaver ver — Kind** · \`model\` · \`job_id\``).
- Queue claim scans 1000 queued rows (was 300) so a long blocked MR/PR backlog does not hide a free repo.

[0.9.8]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.8

## [0.9.7] — 2026-09-11

Scheduled MR/PR follow-ups post a regular note, then reply with the answer.

### Fixed

- Scheduled GitLab MR and Azure PR follow-ups post the prompt as a regular note (not a resolvable review thread). The model answer replies to that note.

[0.9.7]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.7

## [0.9.6] — 2026-09-11

Optional Jira trigger label. Silent `/ask` and `/review`. Thread follow-ups send a structured prompt. Replies use a Creasy-style header.

### Added

- `JIRA_TRIGGER_LABEL` (Settings and leftover `TRIGGER_LABELS`): when set, To Do intake needs bot assignee **and** one of those labels. Empty = assignee only.

### Changed

- `@mention /ask` and `@mention /review` stay silent (other agent). Other invalid mentions still get a usage note.
- Usage notes match Creasy’s shape and do not contain `@name` / `@mention`.
- GitLab, Azure, and Jira replies start with `**Yaver {version} — Kind** · model · job`.
- MR/PR comment jobs split the user message into **Replied message** and **Prompt**.

[0.9.6]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.6

## [0.9.5] — 2026-09-11

HTTP Azure DevOps Server remotes clone again. Leftover `GITLAB_PAT` authenticates MR replies.

### Fixed

- HTTP TFS / GitLab remotes (`http://host:8080/…`) stay HTTP when applying PAT `insteadOf`. HTTPS and SSH remotes still rewrite to HTTPS + PAT.
- Leftover `GITLAB_PAT` (no host map) is sent as `PRIVATE-TOKEN` on MR replies. Settings Test connection still does not send that leftover token to a newly typed host.

[0.9.5]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.5

## [0.9.4] — 2026-09-10

`@mention /yaver` starts comment jobs. `Mode: test` writes unit tests only. TFS identity chips and GUIDs match the bot. Plan/build stay on the work branch.

### Added

- `Mode: test` runs OpenCoderman **derman-test** (unit tests only; read the clone `AGENTS.md` first). Delivery is the same as build (push + MR). Plan, build, and test keep separate sessions.

### Changed

- GitLab and Azure comment jobs start on `@mention /yaver` (was `/execute`). A mention without `/yaver` still gets a usage note. `/ask` is unchanged.
- derman-build and derman-plan require unit tests for each change, following derman-test.

### Fixed

- TFS `@<VSID>` / `data-vss-mention` GUIDs, `CORP\user` triggers, and chip + `&nbsp;` / `<span>` before `/yaver`.
- PAT identity is seeded from `/tfs` `connectionData`; reviewer GUIDs on the PR count as the bot.
- Long `GL-` / `AZ-` fallback keys no longer collide after the 48-character cut.
- GitLab note ids and Azure comment ids are scoped per project/repo. `plan_ready` is not wiped by a forge comment. Stop keeps queued GitLab/Azure follow-ups. Cancelled queue rows cannot be requeued.
- derman-build cannot `git checkout` / `git switch` (last-match bash order). File restore still works.
- Offline zip CI looks for `code-reviewer.md` after the OpenCoderman rename.

[0.9.4]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.4

## [0.9.3] — 2026-09-10

GitLab replies stay in the MR discussion. Azure comments that reuse id 1 are not collapsed. Follow-ups stay on Queue. Storage deletes only the matching clone.

### Fixed

- GitLab usage notes and job replies use the Discussions API so they stay in the same MR thread.
- Azure webhook comments that omit `threadId` (comment id 1 on every new thread) are no longer treated as one queue item.
- GitLab and Azure follow-up `/execute` comments stay queued and visible while the same ticket is in flight.
- Storage no longer deletes a Jira plan folder, or another repo's clone, when a PR/MR is merged or closed.

[0.9.3]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.3

## [0.9.2] — 2026-09-10

Azure Schedule → Run now queues a job even when TFS comment ids restart at 1.

### Fixed

- Azure Schedule → Run now posted the prompt on the PR but did not queue a job. Dedup now uses PR + thread + comment, same queue path as GitLab.

[0.9.2]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.2

## [0.9.1] — 2026-09-10

Azure PR follow-up on Schedule. TFS PR create, commit links, and Storage status.

### Added

- Scheduled tab can follow up on an existing Azure DevOps PR the same way as a GitLab MR.

### Changed

- Usage notes, OpenCode results, and error replies stay in the triggering GitLab or Azure thread.

### Fixed

- Azure job-detail commit links use `/commit/{sha}?refName=refs/heads/{branch}`.
- Azure PR create no longer double-encodes project names with spaces (`Tank Projeleri`).
- Storage shows Azure PR status and only deletes clone folders that still exist.

[0.9.1]: https://github.com/beratersari/virtual_developer/releases/tag/v0.9.1

## [0.9.0] — 2026-09-10

GitLab and Azure comment jobs start only on `@mention /execute`.

### Changed

- GitLab and Azure comment jobs start only on `@mention /execute`. A mention without `/execute` gets a usage note in the same thread (not a new post). `@mention /ask` is still a silent handoff to the other agent. Comments from the bot user are ignored (no usage note). Replies stay in the existing MR discussion / PR thread.
- Webhook URLs are `POST /yaver/webhook/gitlab` and `POST /yaver/webhook/azure`. The old `/webhooks/gitlab` and `/webhooks/azure` paths still work.

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
