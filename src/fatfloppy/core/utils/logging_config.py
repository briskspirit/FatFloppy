# src/fatfloppy/core/utils/logging_config.py
"""
Configures the application-wide logging setup.

This module initializes the root logger with a specific format, sets up both
console and file-based logging, and provides a utility function to get
a named logger instance, automatically inferring the name from the caller's
context if not provided.

The `setup_logger()` function is called immediately upon module import to
ensure that logging is configured as early as possible.

Functions:
    setup_logger: Configures the root logger for the application.
    get_logger: Retrieves a logger instance with an appropriate name.
"""
import inspect
import logging
import os
import sys
from datetime import datetime
from typing import Optional

# Constants for log formatting
LOG_FORMAT: str = (
    '%(asctime)s - %(levelname)s - %(name)s.%(funcName)s:%(lineno)d - %(message)s'
)
DATE_FORMAT: str = '%Y-%m-%d %H:%M:%S'


def setup_logger() -> None:
    """
    Sets up the root logger for the application.

    This function configures the basic logging settings, including the level
    (DEBUG), format, and handlers. It directs logs to both standard output
    and a timestamped file in a 'logs' directory. It also sets the logging
    level for noisy third-party libraries to WARNING.
    """
    logging.basicConfig(
        level=logging.DEBUG,
        format=LOG_FORMAT,
        datefmt=DATE_FORMAT,
        stream=sys.stdout,
    )
    logging.getLogger('PyQt6').setLevel(logging.WARNING)

    log_dir = 'logs'
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    file_handler = logging.FileHandler(f'{log_dir}/fatfloppy_{timestamp}.log')
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT))

    logging.getLogger().addHandler(file_handler)


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
            if 'self' in frame.f_locals:
                name = frame.f_locals['self'].__class__.__name__
            else:
                name = os.path.splitext(os.path.basename(frame.f_code.co_filename))[0]
        finally:
            del frame
    return logging.getLogger(name)


setup_logger()
