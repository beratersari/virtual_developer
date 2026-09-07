# derman-plan job (Yaver)

OpenCode agent: **derman-plan**. Strictly unattended daemon job — no
human reply path. Do **not** ask any questions. Do not inspect leftover
`.omo/run-continuation/*.json`.

- Ticket: `{ISSUE_KEY}`
- Plan file (write here, absolute path outside the repo): `{PLAN_PATH}`

Write the plan to `{PLAN_PATH}` only. That path may be outside the
git clone (host data dir). The product repo is **this working
directory**. Do not explore the plan file's parent tree as the
project. Do not copy the plan into the repository and do not
commit it. Do not implement product code.
End with:

PLAN_DONE
file: `{PLAN_PATH}`
implement: no
questions: none
The commit checkbox in the plan must tell **derman-build** to match
this repo's `AGENTS.md` + `git log` and include `{ISSUE_KEY}` the way
that history already does.

The Jira request is below.
