# src/fatfloppy/gui/worker.py
"""
Worker thread for long-running disk operations.
"""

from typing import Any, Callable, Optional
from PyQt6.QtCore import QThread, pyqtSignal


class DiskOperationWorker(QThread):
    """
    Worker thread for executing long-running disk operations.

    Signals:
        progress: Emitted with (current, total, message) for progress updates
        finished: Emitted with result when operation completes successfully
        error: Emitted with exception when operation fails
    """

    progress = pyqtSignal(int, int, str)  # current, total, message
    finished = pyqtSignal(object)  # result
    error = pyqtSignal(Exception)  # exception

    def __init__(
        self,
        operation: Callable,
        *args,
        **kwargs
    ):
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

    def run(self) -> None:
        """Execute the operation in the thread."""
        try:
            # Create a progress callback that emits our signal
            def progress_callback(current: int, total: int, message: str = ""):
                if not self._is_cancelled:
                    self.progress.emit(current, total, message)

            # Check if operation accepts progress_callback
            import inspect
            sig = inspect.signature(self.operation)
            if 'progress_callback' in sig.parameters:
                # Pass the progress callback to the operation
                result = self.operation(*self.args, progress_callback=progress_callback, **self.kwargs)
            else:
                # Operation doesn't support progress reporting
                result = self.operation(*self.args, **self.kwargs)

            if not self._is_cancelled:
                self.finished.emit(result)
        except Exception as e:
            if not self._is_cancelled:
                self.error.emit(e)

    def cancel(self) -> None:
        """Request cancellation of the operation."""
        self._is_cancelled = True
