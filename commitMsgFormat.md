# Commit Message Format

**Source of truth for agents:** `agent/rules/EXECUTION.md` (same rules in `DIRECT_EXECUTION.md`).

## Format

```text
[JIRA-ISSUE-ID] <type>: <description>
```

### Allowed types

| type | meaning |
|------|---------|
| `feat` | Yeni özellik eklendi |
| `fix` | Hata düzeltildi |
| `refactor` | Kodun çalışma şeklini değiştirmeyen iyileştirme |
| `docs` | Dökümantasyon işleri |
| `test` | Birim testler |
| `perf` | Çalışma mantığını değiştirmeyen performans iyileştirmesi |
| `ci` | CI/CD değişiklikleri |
| `build` | Build sistemi ile ilgili değişiklikler |
| `revert` | Kodu geri almak |
| `chore` | Genel işler, küçük düzeltmeler |

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

## Agent steps

1. Branch: `feature/{JIRA_ISSUE_ID}`
2. Implement changes
3. Commit yourself with the format above, e.g.:

```bash
git add .
git commit -m "[PROJ-123] fix: division by zero in calculator"
```

4. **Do not** push or create an MR (the system does that)

## Rules

1. ALWAYS commit after completing work
2. Subject MUST be `[ISSUE-KEY] type: description`
3. Type must be one of: feat, fix, refactor, docs, test, perf, ci, build, revert, chore
4. Do NOT push or create MRs
5. Stay on `feature/{ID}` branches
