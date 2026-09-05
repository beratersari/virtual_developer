# Commit message format (target product repos)

**Source of truth:** the **target repo** — its `AGENTS.md` and
`git log`. Both **derman-build** and **derman-plan** discover that
format. Yaver only passes the ticket id in the user message.

1. Read target `AGENTS.md` / `CONTRIBUTING.md` / commitlint if present.
2. Read `git log -20 --format=%s`.
3. Match that pattern. Place any ticket id from the user message the
   way that history already does.
4. Only if nothing is documented and history is mixed, fall back to
   conventional `type(scope): summary`, with the ticket in the scope
   or as a `[KEY]` prefix.

Branch: `feature/{JIRA_ISSUE_ID}` — agent commits; Yaver pushes and
opens the MR.

Note: conventions for **this** virtual_developer repository itself are
in `AGENTS.md` (`type(scope): summary`) and differ on purpose.
