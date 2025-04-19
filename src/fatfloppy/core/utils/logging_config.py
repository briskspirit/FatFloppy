# src/fatfloppy/utils/logging_config.py
import os
import sys
import inspect
import logging
from datetime import datetime

# Define log format to include timestamp, level, module/class/method, and message
LOG_FORMAT = '%(asctime)s - %(levelname)s - %(name)s.%(funcName)s:%(lineno)d - %(message)s'
DATE_FORMAT = '%Y-%m-%d %H:%M:%S'

def setup_logger():
    """Setup and configure the root logger."""
    # Configure root logger
    logging.basicConfig(
        level=logging.DEBUG,  # Most verbose level
        format=LOG_FORMAT,
        datefmt=DATE_FORMAT,
        stream=sys.stdout,  # Log to stdout for now
    )

    # Reduce verbosity of third-party libraries
    logging.getLogger('PyQt6').setLevel(logging.WARNING)

    # Create a file handler that logs to a timestamped file
    log_dir = 'logs'
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    file_handler = logging.FileHandler(f'{log_dir}/fatfloppy_{timestamp}.log')
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
    logging.getLogger().addHandler(file_handler)

def get_logger(name=None):
    """Get a logger with the given name or based on the caller's module/class."""
    if name is None:
        # Get the caller's frame info
        frame = inspect.currentframe().f_back
        try:
            # Try to determine if called from a class method
            if 'self' in frame.f_locals:
                # Get class name if called from a method
                name = frame.f_locals['self'].__class__.__name__
            else:
                # Otherwise use the module name
                name = os.path.splitext(os.path.basename(frame.f_code.co_filename))[0]
        finally:
            del frame  # Avoid reference cycles

    return logging.getLogger(name)

# Setup logging when this module is imported
setup_logger()
