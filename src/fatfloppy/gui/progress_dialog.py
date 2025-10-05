# src/fatfloppy/gui/progress_dialog.py
"""
Progress dialog for long-running operations.
"""

from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

DIALOG_MIN_WIDTH = 400
PROGRESS_MAX = 100
DETAIL_LABEL_FONT_SIZE = "9pt"


class ProgressDialog(QDialog):
    """Modal progress dialog with optional cancellation."""

    def __init__(
        self,
        title: str = "Operation in Progress",
        message: str = "Please wait...",
        cancelable: bool = False,
        parent: Optional[QWidget] = None,
    ):
        """
        Initialize progress dialog.

        Args:
            title: Dialog window title
            message: Initial message to display
            cancelable: Whether to show a cancel button
            parent: Parent widget
        """
        super().__init__(parent)
        self.setWindowTitle(title)

        self.setModal(False)
        self.setWindowModality(Qt.WindowModality.WindowModal)

        self.setMinimumWidth(DIALOG_MIN_WIDTH)

        if not cancelable:
            self.setWindowFlags(
                Qt.WindowType.Dialog
                | Qt.WindowType.CustomizeWindowHint
                | Qt.WindowType.WindowTitleHint
                | Qt.WindowType.WindowStaysOnTopHint
            )
        else:
            self.setWindowFlags(
                Qt.WindowType.Dialog | Qt.WindowType.WindowStaysOnTopHint
            )

        self._cancelled = False

        layout = QVBoxLayout()

        self.message_label = QLabel(message)
        self.message_label.setWordWrap(True)
        layout.addWidget(self.message_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setMinimum(0)
        self.progress_bar.setMaximum(PROGRESS_MAX)
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)

        self.detail_label = QLabel("")
        self.detail_label.setWordWrap(True)
        self.detail_label.setStyleSheet(
            f"QLabel {{ color: gray; font-size: {DETAIL_LABEL_FONT_SIZE}; }}"
        )
        self.detail_label.hide()
        layout.addWidget(self.detail_label)

        if cancelable:
            self.cancel_button = QPushButton("Cancel")
            self.cancel_button.clicked.connect(self._on_cancel)
            layout.addWidget(self.cancel_button)
        else:
            self.cancel_button = None

        self.setLayout(layout)

    def set_message(self, message: str) -> None:
        """
        Update the main message label.

        Args:
            message: The message to display.
        """
        self.message_label.setText(message)

    def update_progress(self, current: int, total: int, message: str = "") -> None:
        """
        Update progress bar and message.

        Args:
            current: Current progress value
            total: Total progress value (max)
            message: Optional detail message
        """
        if total > 0:
            percentage = int((current / total) * PROGRESS_MAX)
            self.progress_bar.setValue(percentage)
        else:
            self.progress_bar.setMaximum(0)
            self.progress_bar.setMinimum(0)

        if message:
            self.detail_label.setText(message)
            self.detail_label.show()

    def was_cancelled(self) -> bool:
        """
        Check if operation was cancelled.

        Returns:
            True if the operation was cancelled, False otherwise.
        """
        return self._cancelled

    def _on_cancel(self) -> None:
        """Handle cancel button click."""
        self._cancelled = True
        if self.cancel_button:
            self.cancel_button.setEnabled(False)
            self.cancel_button.setText("Cancelling...")
        self.set_message("Cancelling operation...")
