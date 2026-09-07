# derman-build job (Yaver)

OpenCode agent: **derman-build**. Strictly unattended daemon job — no
human reply path. Do **not** ask any questions. Do not inspect leftover
`.omo/run-continuation/*.json`.

- Ticket: `{ISSUE_KEY}`
- Work branch (already checked out): `{WORK_BRANCH}`
- Plan file (source of truth when it exists; may be outside the repo): `{PLAN_PATH}`

The product repo is **this working directory** (the temp clone). If
`{PLAN_PATH}` is outside the clone, it is only a plan file — do not
explore its parent directory as the project.

If `{PLAN_PATH}` exists, **implement that plan** (every checkbox).
Jira title/description below are context only — do not replace the
plan with a different scope. If the plan file is missing, implement
the Jira request.

Do **not** copy the plan into this repository and do **not** commit
it. Stay on `{WORK_BRANCH}`. Do **not** push or open an MR (Yaver
does that).
Include `{ISSUE_KEY}` in the commit the way **this repo's** `AGENTS.md`
and `git log` already do.

The Jira request is below.
