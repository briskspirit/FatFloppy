# src/fatfloppy/core/utils/logging_config.py
import os
import sys
import inspect
import logging
from datetime import datetime

LOG_FORMAT = '%(asctime)s - %(levelname)s - %(name)s.%(funcName)s:%(lineno)d - %(message)s'
DATE_FORMAT = '%Y-%m-%d %H:%M:%S'

def setup_logger():
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

def get_logger(name=None):
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
