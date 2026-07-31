# Sisyphus Direct Execution Prompt

## Instructions
1. Analyze the task and current codebase
2. Create todos for multi-step work
3. Implement the solution following existing patterns
4. Run verification (tests, type checking)
5. **COMMIT YOUR CHANGES**: If you modified any code files, you MUST commit with a meaningful message
6. Report completion with summary of changes and commit hash

## Commit Requirements
- ALWAYS commit after making code changes - this is MANDATORY
- Use Conventional Commits format: feat:, fix:, chore:, docs:, refactor:, test:
- Include the JIRA issue key in the commit message
- See commitMsgFormat.md for detailed commit message guidelines
- Do NOT commit .env, credentials, or secret files

if [[ -n $INVALID_COMMITS ]]; then
    echo "❌ Hatalı commit mesajları:"
    echo "$INVALID_COMMITS"
    echo ""
    echo "Doğru format örnekleri:"
    echo "  [VOLKAN-1905] feat: Yeni özellik eklendi"
    echo "  [VOLKAN-1905] fix:  Hata düzeltildi"
    echo "  [VOLKAN-1905] refactor:  Kodun çalışma şeklini değiştirmeyen iyileştirme"
    echo "  [VOLKAN-1905] docs:  Dökümantasyon işleri"
    echo "  [VOLKAN-1905] test:  Birim testler"
    echo "  [VOLKAN-1905] perf:  Çalışma mantığını değiştirmeyen performans iyileştirmesi"
    echo "  [VOLKAN-1905] ci:  CI/CD değişiklikleri"
    echo "  [VOLKAN-1905] build:  Build sistemi ile ilgili değişiklikler"
    echo "  [VOLKAN-1905] revert: Kodu geri almak"
    echo "  [VOLKAN-1905] chore:  Genel işler, küçük düzeltmeler"
    echo ""
    exit 1
fi

## Constraints
- Follow existing code style
- Add tests for new functionality
- Do not break existing tests
- Minimal, focused changes
