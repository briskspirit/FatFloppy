# src/fatfloppy/core/utils/logging_config.py
"""
Configures the application-wide logging setup.

This module initializes the root logger with a specific format, sets up both
console and file-based logging, and provides a utility function to get
a named logger instance, automatically inferring the name from the caller's
context if not provided.

`setup_logger()` is invoked by the application entry point (main.py), not at
import time, so importing the library does not reconfigure the embedding
application's logging or create log files as a side effect.

Functions:
    setup_logger: Configures the root logger for the application.
    get_logger: Retrieves a logger instance with an appropriate name.
"""

import contextlib
import inspect
import logging
import os
import platform
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

# Constants for log formatting
LOG_FORMAT: str = (
    "%(asctime)s - %(levelname)s - %(name)s.%(funcName)s:%(lineno)d - %(message)s"
)
DATE_FORMAT: str = "%Y-%m-%d %H:%M:%S"

_LOG_FILE_HANDLER_ATTACHED: bool = False


def setup_logger() -> None:
    """
    Sets up the root logger for the application.

    Configures console logging (DEBUG) and, if possible, a timestamped file in
    the platform log directory. If the log directory or file cannot be created,
    logging falls back to console-only instead of crashing the application.
    Safe to call more than once: the file handler is attached at most once.
    """
    global _LOG_FILE_HANDLER_ATTACHED

    logging.basicConfig(
        level=logging.DEBUG,
        format=LOG_FORMAT,
        datefmt=DATE_FORMAT,
        stream=sys.stdout,
    )
    logging.getLogger("PyQt6").setLevel(logging.WARNING)

    if _LOG_FILE_HANDLER_ATTACHED:
        return

    app_name = "FatFloppy"

    if platform.system() == "Darwin":
        # macOS: ~/Library/Logs/[AppName]
        log_dir = Path.home() / "Library" / "Logs" / app_name
    elif platform.system() == "Windows":
        # Windows: %LOCALAPPDATA%/FatFloppy/Logs
        local_app_data = Path(os.environ.get("LOCALAPPDATA") or Path.home())
        log_dir = local_app_data / app_name / "Logs"
    else:
        # Linux: ~/.local/share/FatFloppy/Logs (Standard XDG location)
        log_dir = Path.home() / ".local" / "share" / app_name / "Logs"

    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        file_handler = logging.FileHandler(str(log_dir / f"fatfloppy_{timestamp}.log"))
        file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
        logging.getLogger().addHandler(file_handler)
        _LOG_FILE_HANDLER_ATTACHED = True
    except (OSError, RuntimeError) as e:
        # Console logging still works; just note that file logging is unavailable.
        with contextlib.suppress(Exception):
            logging.getLogger(__name__).warning(
                "File logging unavailable (%s); continuing with console only.", e
            )


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """
    Retrieves a logger instance, automatically determining the name if not provided.

    If a name is not explicitly given, this function inspects the call stack to
    infer the name from the calling class or module, providing a convenient
    way to get a contextually named logger.

    Args:
        name: The explicit name for the logger. If None, the name is inferred.

    Returns:
        A configured logger instance.
    """
    if name is None:
        frame = inspect.currentframe().f_back
        try:
            if "self" in frame.f_locals:
                name = frame.f_locals["self"].__class__.__name__
            else:
                name = Path(frame.f_code.co_filename).stem
        finally:
            del frame
    return logging.getLogger(name)
