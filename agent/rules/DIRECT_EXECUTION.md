# Sisyphus Direct Execution Prompt

## Instructions
1. Analyze the task and current codebase
2. Create todos for multi-step work
3. Implement the solution following existing patterns
4. Run verification (tests, type checking)
5. **COMMIT YOUR CHANGES** (mandatory if you modified any files)
6. Report completion with summary of changes and commit hash

## Commit message format (MANDATORY — same as EXECUTION.md)

Every commit subject line MUST use:

```text
[JIRA-ISSUE-ID] <type>: <description>
```

### Allowed types
`feat` · `fix` · `refactor` · `docs` · `test` · `perf` · `ci` · `build` · `revert` · `chore`

### Doğru format örnekleri
```text
[JIRA-ISSUE-ID] feat: Yeni özellik eklendi
[JIRA-ISSUE-ID] fix: Hata düzeltildi
[JIRA-ISSUE-ID] refactor: Kodun çalışma şeklini değiştirmeyen iyileştirme
[JIRA-ISSUE-ID] docs: Dökümantasyon işleri
[JIRA-ISSUE-ID] test: Birim testler
[JIRA-ISSUE-ID] perf: Çalışma mantığını değiştirmeyen performans iyileştirmesi
[JIRA-ISSUE-ID] ci: CI/CD değişiklikleri
[JIRA-ISSUE-ID] build: Build sistemi ile ilgili değişiklikler
[JIRA-ISSUE-ID] revert: Kodu geri almak
[JIRA-ISSUE-ID] chore: Genel işler, küçük düzeltmeler
```

**Example for a real issue:**
```bash
git add .
git commit -m "[PROJ-123] fix: division by zero in calculator"
```

### Rules
- Subject MUST be `[ISSUE-KEY] type: description`
- You create the commit yourself; do **not** push or open an MR
- Do not commit `.env`, credentials, or secret files
- Work on branch `feature/{JIRA_ISSUE_ID}` only

## Constraints
- Follow existing code style
- Add tests for new functionality
- Do not break existing tests
- Minimal, focused changes
