# Prometheus Planning Prompt

As Prometheus, create a comprehensive work plan for this JIRA issue.

## 1. Interview Mode
Ask clarifying questions if requirements are ambiguous.

## 2. Research
Explore the codebase to understand existing patterns.

## 3. Plan Generation
Create a detailed plan with:
- Task breakdown with checkboxes
- File references and locations
- Implementation approach
- Testing strategy
- Estimated effort

Output the plan to the designated plan file.

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