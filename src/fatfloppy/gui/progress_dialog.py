# src/fatfloppy/gui/progress_dialog.py
"""
Progress dialog for long-running operations.
"""

from typing import Optional
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QLabel,
    QProgressBar, QPushButton, QWidget
)


class ProgressDialog(QDialog):
    """
    Modal progress dialog with optional cancellation.
    """

    def __init__(
        self,
        title: str = "Operation in Progress",
        message: str = "Please wait...",
        cancelable: bool = False,
        parent: Optional[QWidget] = None
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

        # Use WindowModal instead of ApplicationModal to be less intrusive on macOS
        self.setModal(False)  # Don't use setModal(True)
        self.setWindowModality(Qt.WindowModality.WindowModal)  # Only blocks parent window

        self.setMinimumWidth(400)

        # Set window flags to prevent blocking system gestures on macOS
        if not cancelable:
            # Non-cancelable: prevent closing but don't be overly modal
            self.setWindowFlags(
                Qt.WindowType.Dialog |
                Qt.WindowType.CustomizeWindowHint |
                Qt.WindowType.WindowTitleHint |
                Qt.WindowType.WindowStaysOnTopHint  # Stay visible but don't block gestures
            )
        else:
            # Cancelable: allow normal window behavior
            self.setWindowFlags(
                Qt.WindowType.Dialog |
                Qt.WindowType.WindowStaysOnTopHint
            )

        self._cancelled = False

        layout = QVBoxLayout()

        # Message label
        self.message_label = QLabel(message)
        self.message_label.setWordWrap(True)
        layout.addWidget(self.message_label)

        # Progress bar
        self.progress_bar = QProgressBar()
        self.progress_bar.setMinimum(0)
        self.progress_bar.setMaximum(100)
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)

        # Detail label (optional, hidden by default)
        self.detail_label = QLabel("")
        self.detail_label.setWordWrap(True)
        self.detail_label.setStyleSheet("QLabel { color: gray; font-size: 9pt; }")
        self.detail_label.hide()
        layout.addWidget(self.detail_label)

        # Cancel button (if cancelable)
        if cancelable:
            self.cancel_button = QPushButton("Cancel")
            self.cancel_button.clicked.connect(self._on_cancel)
            layout.addWidget(self.cancel_button)
        else:
            self.cancel_button = None

        self.setLayout(layout)

    def update_progress(self, current: int, total: int, message: str = "") -> None:
        """
        Update progress bar and message.

        Args:
            current: Current progress value
            total: Total progress value (max)
            message: Optional detail message
        """
        if total > 0:
            percentage = int((current / total) * 100)
            self.progress_bar.setValue(percentage)
        else:
            # Indeterminate progress
            self.progress_bar.setMaximum(0)
            self.progress_bar.setMinimum(0)

        if message:
            self.detail_label.setText(message)
            self.detail_label.show()

    def set_message(self, message: str) -> None:
        """Update the main message label."""
        self.message_label.setText(message)

    def _on_cancel(self) -> None:
        """Handle cancel button click."""
        self._cancelled = True
        if self.cancel_button:
            self.cancel_button.setEnabled(False)
            self.cancel_button.setText("Cancelling...")
        self.set_message("Cancelling operation...")

    def was_cancelled(self) -> bool:
        """Check if operation was cancelled."""
        return self._cancelled
