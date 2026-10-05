"""Disabled secondary actions must not look like the button that works."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"


def _css_block(css: str, selector: str) -> str:
    needle = selector + " {"
    start = css.find(needle)
    assert start >= 0, selector
    end = css.find("}", start)
    assert end > start
    return css[start:end]


def test_disabled_secondary_button_stays_quieter_than_enabled() -> None:
    """Empty Look up stays disabled, and it must look disabled.

    The light-theme rule paints ``.vd-btn-secondary:disabled`` with full text
    color, a wash fill, and a strong border, and it sets opacity back to 1.
    The enabled secondary button is transparent with secondary text. On
    Scheduled, Look up is disabled until the form is filled, so the control
    that does nothing looks like the one that will.
    """
    css = (WEB / "src/index.css").read_text(encoding="utf-8")
    disabled = _css_block(css, ".vd-btn-secondary:disabled")
    enabled = _css_block(css, ".vd-btn-secondary")
    assert "background: transparent" in enabled
    assert "color: var(--text-secondary)" in enabled
    assert "background: var(--wash)" not in disabled
    assert "border-color: var(--border-strong)" not in disabled
    assert "color: var(--text);" not in disabled
    assert (
        "color: var(--text-muted)" in disabled or "color: var(--text-secondary)" in disabled
    )
    page = (WEB / "src/pages/schedules/SchedulesPage.tsx").read_text(encoding="utf-8")
    assert page.count('className="vd-btn vd-btn-secondary"') >= 1
