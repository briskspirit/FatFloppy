from PyQt6.QtCore import QObject, QThread, pyqtSignal, pyqtSlot
from typing import Callable, Optional, Any, Dict


class Worker(QObject):
    """Worker object that runs time-consuming operations in a separate thread."""

    # Signals
    started = pyqtSignal()
    progress = pyqtSignal(int, str)  # percentage, message
    finished = pyqtSignal(dict)
    error = pyqtSignal(str)

    def __init__(self, func: Callable, args=None, kwargs=None):
        """
        Initialize worker with the function to execute.

        Args:
            func: The function to run in the worker thread
            args: Arguments to pass to the function
            kwargs: Keyword arguments to pass to the function
        """
        super().__init__()
        self.func = func
        self.args = args or ()
        self.kwargs = kwargs or {}
        self.is_running = False
        self.result = None

    @pyqtSlot()
    def run(self):
        """Run the function in the worker thread."""
        self.is_running = True
        self.started.emit()

        try:
            # Add progress_callback to kwargs
            self.kwargs['progress_callback'] = self.progress.emit

            # Execute the function
            result = self.func(*self.args, **self.kwargs)

            # Store and emit the result
            self.result = result
            self.finished.emit({'result': result})
        except Exception as e:
            self.error.emit(str(e))
        finally:
            self.is_running = False


class WorkerManager:
    """Manages worker threads and provides a simple interface for creating and monitoring them."""

    def __init__(self):
        self.threads = {}
        self.workers = {}

    def create_worker(self, name: str, func: Callable, args=None, kwargs=None) -> Worker:
        """
        Create a new worker and thread.

        Args:
            name: A unique name for this worker
            func: The function to run in the worker thread
            args: Arguments to pass to the function
            kwargs: Keyword arguments to pass to the function

        Returns:
            The created worker object
        """
        # Create a new thread and worker
        thread = QThread()
        worker = Worker(func, args, kwargs)

        # Move the worker to the thread
        worker.moveToThread(thread)

        # Connect signals
        thread.started.connect(worker.run)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)

        # Store references
        self.threads[name] = thread
        self.workers[name] = worker

        return worker

    def start(self, name: str) -> None:
        """Start the worker thread with the given name."""
        if name in self.threads:
            self.threads[name].start()

    def is_running(self, name: str) -> bool:
        """Check if the worker with the given name is running."""
        if name in self.workers:
            return self.workers[name].is_running
        return False
