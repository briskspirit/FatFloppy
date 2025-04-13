# src/fatfloppy/gui/operations.py
from PyQt6.QtCore import QThread, pyqtSignal

class OperationWorker(QThread):
    progress_signal = pyqtSignal(float)
    error_signal = pyqtSignal(str)
    finished_signal = pyqtSignal(object)

    def __init__(self, operation):
        super().__init__()
        self.operation = operation
        self.result = None

    def run(self):
        try:
            # Pass the progress signal emitter as the callback
            self.result = self.operation(self.progress_signal.emit)
            self.finished_signal.emit(self.result)
        except Exception as e:
            self.error_signal.emit(str(e))
