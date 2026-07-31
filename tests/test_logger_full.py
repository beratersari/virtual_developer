"""Coverage for logger module."""

from src.logger import LogLevel, Logger, logger, set_color_output, set_level
from src import logger as logger_mod


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
