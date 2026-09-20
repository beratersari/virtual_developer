# Changelog

All notable changes to Yaver are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [SemVer](https://semver.org/) from the repo root `VERSION` file.
GitHub Releases are cut from tags `vMAJOR.MINOR.PATCH`.

## [Unreleased]

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
