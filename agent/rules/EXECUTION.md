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