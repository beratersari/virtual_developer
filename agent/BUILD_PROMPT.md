# derman-build job (Yaver)

OpenCode agent: **derman-build**. Strictly unattended daemon job — no
human reply path. Do **not** ask any questions. Do not inspect leftover
`.omo/run-continuation/*.json`.

- Ticket: `{ISSUE_KEY}`
- Work branch (already checked out): `{WORK_BRANCH}`
- Plan file (if present): `{PLAN_PATH}`

Stay on `{WORK_BRANCH}`. Do **not** push or open an MR (Yaver does that).
Include `{ISSUE_KEY}` in the commit the way **this repo's** `AGENTS.md`
and `git log` already do.

The Jira request is below.
