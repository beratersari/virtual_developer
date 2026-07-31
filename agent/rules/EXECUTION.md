# Atlas Execution Prompt

## Delegation Guidelines
- Use `category="visual-engineering"` for UI/UX work
- Use `category="deep"` for complex problem-solving
- Use `category="quick"` for simple fixes
- Use `subagent_type="oracle"` for architecture decisions
- Use `subagent_type="explore"` for codebase research

## Success Criteria
- All plan checkboxes checked
- Tests passing
- No type errors
- Code follows project conventions

## Commit message format (MANDATORY — project standard)

Every commit subject line MUST use:

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

### Rules
- Subject MUST be `[ISSUE-KEY] type: description` (key in square brackets, then conventional type)
- Do not omit the type (`feat:`, `fix:`, …)
- Do not use bare `feat:` without the `[KEY]` prefix
- Do not push or create merge requests (the system does that)
- Do not commit `.env`, credentials, or secret files
- Work on branch `feature/{JIRA_ISSUE_ID}` only
