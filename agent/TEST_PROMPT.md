# derman-test job (Yaver)

OpenCode agent: **derman-test**. Strictly unattended daemon job — no
human reply path. Do **not** ask any questions. Do not inspect leftover
`.omo/run-continuation/*.json`.

- Ticket: `{ISSUE_KEY}`
- Work branch (already checked out): `{WORK_BRANCH}`

The product repo is **this working directory** (the temp clone).

Write **unit tests only**. Do not implement product features.

**First** read the `AGENTS.md` files in this clone (root and nested)
to learn how this repo tests. Follow those rules over generic
advice.

Raise **line, branch, and condition** coverage. For every missed
path, write a real test of that condition or edge case
(boundaries, equivalence classes, legal and illegal state
transitions). When a function calls another function, cover it
with `expect_call` (or this repo's equivalent) and the **exact**
parameters, plus once / call-order / not-called. If deleting
that production call would not fail the test, the test is
wrong. Do **not** add hacky tests whose only job is to paint
coverage green. Do not stop at "tests pass" — stop when the
coverage report for the ticket's files has no closable miss.

Then match neighboring tests and the unit-test practices on the
derman-test agent.

Stay on `{WORK_BRANCH}` (already checked out). Do **not**
`git checkout`, `git switch`, or create another branch. File
restore (`git checkout -- path`) is fine. Do **not** push or open
an MR (Yaver does that).
Include `{ISSUE_KEY}` in the commit the way **this repo's** `AGENTS.md`
and `git log` already do.

The Jira request is below.
