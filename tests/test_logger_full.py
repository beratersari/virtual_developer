"""Coverage for logger module."""

from src import logger as logger_mod
from src.logger import LogLevel, Logger, logger, set_color_output, set_level


def test_all_levels():
    logger.debug("d")
    logger.info("i")
    logger.warning("w")
    logger.error("e")
    try:
        raise ValueError("x")
    except ValueError as exc:
        logger.exception("failed", exc)


def test_set_level_and_color():
    set_level(LogLevel.DEBUG)
    set_level(LogLevel.INFO)
    set_color_output(False)
    set_color_output(True)
    logger.info("after color toggle")


def test_logger_instance_methods():
    lg = Logger()
    lg.set_level(LogLevel.DEBUG)
    lg.set_color_output(False)
    lg.debug("d")
    lg.info("i")
    lg.warning("w")
    lg.error("e")
    lg.set_level(LogLevel.ERROR)
    lg.debug("hidden")
    lg.set_color_output(True)
    try:
        1 / 0
    except ZeroDivisionError as e:
        # Custom logger requires (message, exc)
        lg.exception("div", e)


def test_module_level_wrappers():
    logger_mod.debug("d")
    logger_mod.info("i")
    logger_mod.warning("w")
    logger_mod.error("e")


def test_format_has_no_emoji_and_stable_columns():
    """Professional layout: timestamp, level, [file:line], function, message — no icons."""
    lg = Logger()
    lg.set_color_output(False)
    text = lg._format_message(
        LogLevel.INFO,
        "hello world",
        "client.py",
        44,
        "__init__",
    )
    for emoji in ("🐛", "ℹ️", "⚠️", "❌", "🔥"):
        assert emoji not in text
    assert "INFO" in text
    assert "[client.py:44]" in text
    assert "__init__" in text
    assert "hello world" in text
    # timestamp shape YYYY-MM-DD
    assert text[:4].isdigit()


def test_info_line_includes_caller_file_and_line(capsys):
    """Live logger.info must print the caller's file and line number."""
    lg = Logger()
    lg.set_color_output(False)
    lg.set_level(LogLevel.DEBUG)
    lg.info("caller location check")
    out = capsys.readouterr().out
    assert "test_logger_full.py:" in out
    assert "caller location check" in out


def test_format_collapses_multiline_message():
    lg = Logger()
    lg.set_color_output(False)
    text = lg._format_message(
        LogLevel.WARNING,
        "line1\nline2",
        "daemon.py",
        10,
        "stop",
    )
    assert "line1 line2" in text
    assert "\nline2" not in text.split("WARNING")[-1] or "line1 line2" in text
