# Commit message format (target product repos)

**Source of truth:** `agent/AGENT_PROMPT.md` → section `## §policy.commit`.

Agents in temp clones use:

```text
[JIRA-ISSUE-ID] <type>: <description>
```

Types: `feat` · `fix` · `refactor` · `docs` · `test` · `perf` · `ci` · `build` · `revert` · `chore`

Branch: `feature/{JIRA_ISSUE_ID}` — agent commits; system pushes and opens the MR.

Example:

```bash
git commit -m "[PROJ-123] fix: division by zero in calculator"
```

Note: conventions for **this** virtual_developer repository itself are in
`AGENTS.md` (`type(scope): summary`) and differ on purpose.
