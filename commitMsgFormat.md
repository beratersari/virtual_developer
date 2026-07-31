# Commit Message Format

This document defines the commit message format for the JIRA Virtual Developer project.

## Agent Instructions - YOU MUST FOLLOW THESE STEPS

As an AI agent working on JIRA issues, you are RESPONSIBLE for creating commits yourself.

### Step 1: Create Feature Branch (if not exists)
```bash
git checkout -b feature/{JIRA_ISSUE_ID}
```

### Step 2: Make Your Changes
Implement the requested changes in the codebase.

### Step 3: Stage Changes
```bash
git add .
```

### Step 4: Create Commit (YOU MUST DO THIS)
**CRITICAL:** You must create a commit after completing your work. Use this exact format:

```bash
git commit -m "[{JIRA_ISSUE_ID}] {Short description}

{Detailed description of what you changed and why}

Closes: {JIRA_ISSUE_ID}"
```

**Example:**
```bash
git commit -m "[PROJ-123] Fix division by zero in calculator

The divide method was not checking for zero divisor.
Added validation to raise ValueError when divisor is zero.
Updated tests to cover this edge case.

Closes: PROJ-123"
```

### Step 5: DO NOT PUSH OR CREATE MR
**DO NOT run:**
- `git push` (the system will do this)
- `glab mr create` (the system will do this)

Your job ends after Step 4 (commit). The system will handle push and merge request creation.

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

## Commit Message Format Details

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

## Rules for Agents

1. **ALWAYS commit** — After completing work, you MUST create a commit
2. **Follow the format** — Use `[JIRA-ID] summary` format exactly
3. **Include Closes** — Every commit must end with `Closes: JIRA-ID`
4. **One logical change per commit** — If you fix multiple things, consider multiple commits
5. **DO NOT push** — Never run `git push` or create MRs
6. **Stay on feature branch** — All work on `feature/{ID}` branches

## Git Operations Reference

**Configure git identity (first time):**
```bash
git config user.name "DevBot"
git config user.email "devbot@example.com"
```

**Check status:**
```bash
git status
```

**Create commit:**
```bash
git add .
git commit -m "[JIRA-ID] Your message

Details...

Closes: JIRA-ID"
```

**View commit history:**
```bash
git log --oneline -5
```
