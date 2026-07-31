"""
Centralized colored logging module for the entire application.

Usage:
    from logger import logger
    
    logger.debug("Debug message")
    logger.info("Info message")
    logger.warning("Warning message")
    logger.error("Error message")
    logger.critical("Critical message")
"""

import sys
import traceback
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional


class LogLevel(Enum):
    """Log levels with numeric values for comparison."""
    DEBUG = 0
    INFO = 1
    WARNING = 2
    ERROR = 3
    CRITICAL = 4


class Colors:
    """ANSI color codes for terminal output."""
    # Text colors
    BLACK = "\033[30m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    WHITE = "\033[37m"
    
    # Bright text colors
    BRIGHT_BLACK = "\033[90m"
    BRIGHT_RED = "\033[91m"
    BRIGHT_GREEN = "\033[92m"
    BRIGHT_YELLOW = "\033[93m"
    BRIGHT_BLUE = "\033[94m"
    BRIGHT_MAGENTA = "\033[95m"
    BRIGHT_CYAN = "\033[96m"
    BRIGHT_WHITE = "\033[97m"
    
    # Background colors
    BG_BLACK = "\033[40m"
    BG_RED = "\033[41m"
    BG_GREEN = "\033[42m"
    BG_YELLOW = "\033[43m"
    BG_BLUE = "\033[44m"
    BG_MAGENTA = "\033[45m"
    BG_CYAN = "\033[46m"
    BG_WHITE = "\033[47m"
    
    # Styles
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    ITALIC = "\033[3m"
    UNDERLINE = "\033[4m"


class Logger:
    """
    Centralized logger with colored output, timestamps, file:line info,
    and multiple log levels.
    """
    
    # Default minimum log level
    _min_level: LogLevel = LogLevel.DEBUG
    
    # Whether to use colors (auto-detected based on terminal support)
    _use_colors: bool = True
    
    # Level configurations: (name, color, bg_color, icon)
    _LEVEL_CONFIG = {
        LogLevel.DEBUG: ("DEBUG", Colors.BRIGHT_BLACK, None, "🐛"),
        LogLevel.INFO: ("INFO", Colors.BRIGHT_BLUE, None, "ℹ️"),
        LogLevel.WARNING: ("WARNING", Colors.BRIGHT_YELLOW, None, "⚠️"),
        LogLevel.ERROR: ("ERROR", Colors.BRIGHT_RED, None, "❌"),
        LogLevel.CRITICAL: ("CRITICAL", Colors.BRIGHT_WHITE, Colors.BG_RED, "🔥"),
    }
    
    def __init__(self):
        """Initialize logger with auto-detection of color support."""
        self._detect_color_support()
    
    def _detect_color_support(self) -> None:
        """Detect if terminal supports colors."""
        # Check if running in terminal that supports colors
        if not sys.stdout.isatty():
            self._use_colors = False
        else:
            # Windows Terminal, modern terminals support colors
            self._use_colors = True
    
    def set_level(self, level: LogLevel) -> None:
        """Set minimum log level."""
        self._min_level = level
    
    def set_color_output(self, enabled: bool) -> None:
        """Enable or disable colored output."""
        self._use_colors = enabled
    
    def _get_caller_info(self, stack_offset: int = 3) -> tuple[str, int, str]:
        """
        Get caller information: filename, line number, function name.
        
        Args:
            stack_offset: How far up the stack to look (default 3 to skip logger internals)
        
        Returns:
            Tuple of (filename, line_number, function_name)
        """
        try:
            frame = sys._getframe(stack_offset)
            filename = Path(frame.f_code.co_filename).name
            line_no = frame.f_lineno
            func_name = frame.f_code.co_name
            return filename, line_no, func_name
        except Exception:
            return "unknown", 0, "unknown"
    
    def _format_message(
        self,
        level: LogLevel,
        message: str,
        filename: str,
        line_no: int,
        func_name: str,
        exception: Optional[BaseException] = None
    ) -> str:
        """
        Format log message with all metadata.
        
        Args:
            level: Log level
            message: Main log message
            filename: Source file name
            line_no: Line number in source file
            func_name: Function name
            exception: Optional exception to include
        
        Returns:
            Formatted log string
        """
        # Get current timestamp
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        
        # Get level configuration
        level_name, level_color, bg_color, icon = self._LEVEL_CONFIG[level]
        
        # Format level name with fixed width for alignment
        level_str = f"[{level_name:8}]"
        
        # Format location (filename:line)
        location = f"{filename}:{line_no}"
        
        # Build the log line
        if self._use_colors:
            # Colored output
            timestamp_str = f"{Colors.DIM}{timestamp}{Colors.RESET}"
            
            if bg_color:
                # Critical level with background color
                level_formatted = f"{bg_color}{Colors.BOLD}{level_color}{level_str}{Colors.RESET}"
            else:
                level_formatted = f"{level_color}{level_str}{Colors.RESET}"
            
            location_str = f"{Colors.BRIGHT_BLACK}{location:30}{Colors.RESET}"
            func_str = f"{Colors.DIM}({func_name}){Colors.RESET}"
            icon_str = f"{level_color}{icon}{Colors.RESET}"
            message_str = f"{Colors.WHITE}{message}{Colors.RESET}"
            
            parts = [
                timestamp_str,
                level_formatted,
                location_str,
                func_str,
                icon_str,
                message_str
            ]
        else:
            # Plain output (no colors)
            parts = [
                timestamp,
                level_str,
                f"{location:30}",
                f"({func_name})",
                icon,
                message
            ]
        
        result = " ".join(parts)
        
        # Add exception info if provided
        if exception:
            exc_lines = traceback.format_exception(
                type(exception), exception, exception.__traceback__
            )
            exc_text = "".join(exc_lines)
            
            if self._use_colors:
                exc_formatted = f"\n{Colors.BRIGHT_RED}{exc_text}{Colors.RESET}"
            else:
                exc_formatted = f"\n{exc_text}"
            
            result += exc_formatted
        
        return result
    
    def _log(
        self,
        level: LogLevel,
        message: str,
        exception: Optional[BaseException] = None
    ) -> None:
        """
        Internal log method.
        
        Args:
            level: Log level
            message: Log message
            exception: Optional exception
        """
        # Check minimum level
        if level.value < self._min_level.value:
            return
        
        # Get caller info
        filename, line_no, func_name = self._get_caller_info()
        
        # Format message
        formatted = self._format_message(
            level, message, filename, line_no, func_name, exception
        )
        
        # Output to stderr for warnings and errors, stdout for info and debug
        if level.value >= LogLevel.WARNING.value:
            print(formatted, file=sys.stderr)
        else:
            print(formatted, file=sys.stdout)
        
        # Flush to ensure output is written immediately
        sys.stdout.flush()
        sys.stderr.flush()
    
    def debug(self, message: str) -> None:
        """Log debug message."""
        self._log(LogLevel.DEBUG, message)
    
    def info(self, message: str) -> None:
        """Log info message."""
        self._log(LogLevel.INFO, message)
    
    def warning(self, message: str) -> None:
        """Log warning message."""
        self._log(LogLevel.WARNING, message)
    
    def error(self, message: str, exception: Optional[BaseException] = None) -> None:
        """Log error message with optional exception."""
        self._log(LogLevel.ERROR, message, exception)
    
    def critical(self, message: str, exception: Optional[BaseException] = None) -> None:
        """Log critical message with optional exception."""
        self._log(LogLevel.CRITICAL, message, exception)
    
    def exception(self, message: str, exc: BaseException) -> None:
        """
        Log exception with full traceback.
        
        Args:
            message: Message describing the exception context
            exc: The exception to log
        """
        self._log(LogLevel.ERROR, message, exc)


# Global logger instance
logger = Logger()


# Convenience functions for direct import

def debug(message: str) -> None:
    """Log debug message using global logger."""
    logger.debug(message)


def info(message: str) -> None:
    """Log info message using global logger."""
    logger.info(message)


def warning(message: str) -> None:
    """Log warning message using global logger."""
    logger.warning(message)


def error(message: str, exception: Optional[BaseException] = None) -> None:
    """Log error message using global logger."""
    logger.error(message, exception)


def critical(message: str, exception: Optional[BaseException] = None) -> None:
    """Log critical message using global logger."""
    logger.critical(message, exception)


def exception(message: str, exc: BaseException) -> None:
    """Log exception with full traceback using global logger."""
    logger.exception(message, exc)


def set_level(level: LogLevel) -> None:
    """Set minimum log level for global logger."""
    logger.set_level(level)


def set_color_output(enabled: bool) -> None:
    """Enable or disable colored output for global logger."""
    logger.set_color_output(enabled)
