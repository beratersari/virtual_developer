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


def test_module_level_wrappers_removed():
    for name in ("debug", "info", "warning", "error", "critical", "exception"):
        assert not hasattr(logger_mod, name)


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


def test_write_stream_survives_cp1252_console(monkeypatch):
    """Frozen Windows stdio is cp1252 — ≈ must not abort the process."""
    from src.logger import _write_stream

    class _Cp1252:
        encoding = "cp1252"

        def __init__(self):
            self.writes = []

        def write(self, s):
            s.encode("cp1252")
            self.writes.append(s)

        def flush(self):
            pass

    broken = _Cp1252()
    _write_stream(broken, "applied≈48 dash—ok")
    # Primary print may fail; replacement path must not raise.
    class _Buf:
        def __init__(self):
            self.data = b""

        def write(self, b):
            self.data += b

        def flush(self):
            pass

    class _WithBuf(_Cp1252):
        def __init__(self):
            super().__init__()
            self.buffer = _Buf()

    stream = _WithBuf()
    _write_stream(stream, "applied≈48")
    assert b"applied" in stream.buffer.data


def test_logger_debug_almost_equal_does_not_raise(capsys):
    lg = Logger()
    lg.set_color_output(False)
    lg.debug("Loaded dotenv keys (applied≈3)")
    out = capsys.readouterr().out
    assert "applied" in out


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
