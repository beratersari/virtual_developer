# Commit Message Format

This document defines the commit message format for the JIRA Virtual Developer project.

## Branch Naming Convention

All feature work must be done on a feature branch named:

```
feature/{JIRA_ISSUE_ID}
```

**Examples:**
- `feature/PROJ-123`
- `feature/ENG-456`
- `feature/DEV-789`

### Branch Collision Handling

If a branch already exists, a version suffix is appended:

- First attempt: `feature/PROJ-123`
- If exists: `feature/PROJ-123-v2`
- If exists: `feature/PROJ-123-v3`
- ... and so on until a unique branch name is found

## Commit Message Format

Commits must follow this structure:

```
[{JIRA_ISSUE_ID}] {Short description}

{Detailed description (optional)}

Closes: {JIRA_ISSUE_ID}
```

### Components

1. **JIRA Issue ID** (required): The issue identifier in square brackets
2. **Short description** (required): A brief summary of the change (max 72 chars for title line)
3. **Detailed description** (optional): Additional context, motivation, or explanation
4. **Closes reference** (required): Links the commit to the JIRA issue

### Examples

**Simple fix:**
```
[PROJ-123] Fix division by zero in calculator

The divide method was not checking for zero divisor, causing runtime errors.
Added a guard clause to raise ValueError for division by zero.

Closes: PROJ-123
```

**Feature addition:**
```
[ENG-456] Add modulo and square root functions

Implemented modulo() and sqrt() methods in the Calculator class.
Added corresponding unit tests to verify behavior with edge cases.

Closes: ENG-456
```

**Refactoring:**
```
[DEV-789] Refactor Calculator class with type hints

Added type annotations to all methods for better IDE support and
documentation. No functional changes.

Closes: DEV-789
```

## Rules

1. **One commit per logical change** — If you fix two bugs, make two commits
2. **Atomic commits** — Each commit should represent a complete, working change
3. **Reference JIRA** — Every commit must include the JIRA issue ID
4. **Branch isolation** — All work happens on `feature/{ID}` branches
5. **Clean history** — Use `git commit --amend` for local fixes before pushing
