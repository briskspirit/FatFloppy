# src/fatfloppy/gui/worker.py
"""
Worker thread for long-running disk operations.
"""

import inspect
from typing import Callable

from PyQt6.QtCore import QThread, pyqtSignal


class DiskOperationWorker(QThread):
    """
    Worker thread for executing long-running disk operations.

    Signals:
        progress: Emitted with (current, total, message) for progress updates
        finished: Emitted with result when operation completes successfully
        error: Emitted with exception when operation fails
    """

    progress = pyqtSignal(int, int, str)
    finished = pyqtSignal(object)
    error = pyqtSignal(Exception)

    def __init__(self, operation: Callable, *args, **kwargs):
        """
        Initialize worker thread.

        Args:
            operation: The function to execute in the thread
            *args: Positional arguments for the operation
            **kwargs: Keyword arguments for the operation
        """
        super().__init__()
        self.operation = operation
        self.args = args
        self.kwargs = kwargs
        self._is_cancelled = False

    def cancel(self) -> None:
        """Request cancellation of the operation."""
        self._is_cancelled = True

    def run(self) -> None:
        """Execute the operation in the thread."""
        try:

            def progress_callback(current: int, total: int, message: str = ""):
                if not self._is_cancelled:
                    self.progress.emit(current, total, message)

            sig = inspect.signature(self.operation)
            if "progress_callback" in sig.parameters:
                result = self.operation(
                    *self.args, progress_callback=progress_callback, **self.kwargs
                )
            else:
                result = self.operation(*self.args, **self.kwargs)

            if not self._is_cancelled:
                self.finished.emit(result)
        except Exception as e:
            if not self._is_cancelled:
                self.error.emit(e)
