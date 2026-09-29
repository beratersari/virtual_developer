"""Analytics fills the main column and keeps breakdown columns on screen."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"


def test_analytics_fills_the_main_column_and_keeps_table_columns() -> None:
    shell = (WEB / "src/app/Shell.tsx").read_text(encoding="utf-8")
    css = (WEB / "src/index.css").read_text(encoding="utf-8")
    page = (WEB / "src/pages/analytics/AnalyticsPage.tsx").read_text(encoding="utf-8")
    assert "startsWith('/analytics')" in shell
    assert "vd-main-wide" in shell
    assert ".vd-main-inner.vd-main-wide" in css
    assert "max-width: none" in css
    assert "vd-table-fit" in page
    assert "table-layout: fixed" in css
    assert 'className="min-w-0"' in page
    chart = (WEB / "src/ui/LineChart.tsx").read_text(encoding="utf-8")
    assert "h-auto w-full" not in chart
    assert "height={height}" in chart
    assert "vd-analytics-chart" in page
    assert ".vd-analytics-chart" in css
    assert "Category mix" in page
    assert page.index("Jobs over time") < page.index("Opened by us")
    assert "chartIndexAt" in chart
    assert "onPointerMove={onPlotMove}" in chart
