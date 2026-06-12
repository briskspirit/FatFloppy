# tests/software/test_57_gui_dual_viewer.py
"""
GUI feedback Task 1: viewing a file must populate BOTH the text and hex
viewers; the text/binary guess only decides which (tabified) dock is raised.

Previous behavior: view_file_content() loaded only the guessed viewer and
cleared the other (no earlier test pinned that clearing).

New contract:
  * Both viewers are always populated with the same file content, each
    exactly as if it had been opened directly (titles, state, paths).
  * Only the guessed dock is raised (the docks are tabified by
    main_window._setup_dock_layout, so raise_() switches the active tab).
  * Save-path safety: for a binary (hex-guessed) file the text view shows a
    lossy decoded-with-replacement rendering, so it is loaded READ-ONLY with
    the save path inert -- writing that text back would corrupt the file.
    For a text-guessed file the editor behaves exactly as before, and the
    hex viewer is read-only by construction.
"""

import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import pytest  # noqa: E402

from fatfloppy.gui.managers.editor_manager import EditorManager  # noqa: E402

TEXT_CONTENT = b"Hello, world!\r\nSecond line\r\n"
TEXT_DECODED = "Hello, world!\nSecond line\n"
# 30/256 sampled chars (controls minus \r\n\t, plus DEL) are non-printable
# in cp437 -> 11.7% > the 10% threshold -> guessed binary.
BINARY_CONTENT = bytes(range(256)) * 4

HEX_LINE_0_TEXT = (
    "00000000  48 65 6C 6C 6F 2C 20 77  6F 72 6C 64 21 0D 0A 53  Hello, world!..S"
)
HEX_LINE_0_BINARY = (
    "00000000  00 01 02 03 04 05 06 07  08 09 0A 0B 0C 0D 0E 0F  ................"
)


@pytest.fixture(scope="module")
def qapp():
    """Provides a single offscreen QApplication for the GUI tests."""
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


def _make_env(content, *, fs_type="FAT12", physical=False):
    """Builds a minimal main-window stand-in around a real EditorManager.

    Real Qt widgets where content/read-only state matters (viewers,
    buttons); MagicMock docks so raise_()/setWindowTitle() can be asserted.
    """
    from PyQt6.QtWidgets import QMainWindow, QPlainTextEdit, QPushButton

    window = QMainWindow()
    window.logger = logging.getLogger("test_dual_viewer")

    window.text_viewer = QPlainTextEdit()
    window.hex_viewer = QPlainTextEdit()
    window.hex_viewer.setReadOnly(True)
    window.save_button = QPushButton()
    window.save_button.setEnabled(False)
    window.discard_button = QPushButton()
    window.discard_button.setEnabled(False)
    window.text_viewer_dock = MagicMock()
    window.hex_viewer_dock = MagicMock()
    window.file_manager = MagicMock()

    node = SimpleNamespace(name="TESTFILE.BIN", is_dir=False)
    item = SimpleNamespace(node=node)
    window.file_list = SimpleNamespace(selectedItems=lambda: [item])
    window._build_full_path = lambda name: f"/{name}"

    window.controller = SimpleNamespace(
        driver=SimpleNamespace(
            driver_category="physical" if physical else "metadata_based"
        ),
        filesystem=SimpleNamespace(
            get_display_info=lambda: {"Filesystem Type": fs_type}
        ),
        read_file=lambda _path: content,
    )
    if physical:
        window.file_manager.read_file_threaded = lambda _path, on_success: on_success(
            content
        )

    manager = EditorManager(window)
    # Mirror the wiring in main_window.py (textChanged -> modified tracking).
    window.text_viewer.textChanged.connect(manager.on_text_editor_changed)
    window.editor_manager = manager
    return window, manager


@pytest.mark.usefixtures("qapp")
class TestDualViewerLoading:
    def test_text_file_populates_both_views_and_raises_text_dock(self):
        window, manager = _make_env(TEXT_CONTENT)

        manager.view_file_content()

        # Text editor: decoded LF-normalized content, editable, save path
        # armed exactly as before.
        assert window.text_viewer.toPlainText() == TEXT_DECODED
        assert not window.text_viewer.isReadOnly()
        assert manager.original_text_content == TEXT_DECODED
        assert manager.current_file_path == "/TESTFILE.BIN"
        window.text_viewer_dock.setWindowTitle.assert_called_with(
            "Text Editor - TESTFILE.BIN"
        )

        # Hex viewer: populated with the formatted dump of the same bytes,
        # exactly as if it had been opened directly.
        hex_lines = window.hex_viewer.toPlainText().splitlines()
        assert hex_lines, "hex viewer must be populated even for a text guess"
        assert hex_lines[0] == HEX_LINE_0_TEXT
        assert manager.current_hex_file_path == "/TESTFILE.BIN"
        window.hex_viewer_dock.setWindowTitle.assert_called_with(
            "Hex Viewer - TESTFILE.BIN (28 bytes)"
        )

        # The guess only picks the raised dock.
        window.text_viewer_dock.raise_.assert_called_once()
        window.hex_viewer_dock.raise_.assert_not_called()

    def test_binary_file_populates_both_views_hex_raised_text_read_only(self):
        window, manager = _make_env(BINARY_CONTENT)

        manager.view_file_content()

        # Hex viewer: populated and raised.
        hex_lines = window.hex_viewer.toPlainText().splitlines()
        assert hex_lines and hex_lines[0] == HEX_LINE_0_BINARY
        window.hex_viewer_dock.raise_.assert_called_once()
        window.text_viewer_dock.raise_.assert_not_called()

        # Text viewer: populated so a wrong guess still shows the file...
        assert window.text_viewer.toPlainText(), "text view must be populated"

        # ...but READ-ONLY with an inert save path: the text is a lossy
        # decoded-with-replacement rendering and writing it back would
        # corrupt the binary file.
        assert window.text_viewer.isReadOnly()
        assert manager.original_text_content is None
        assert not window.save_button.isEnabled()
        assert not window.discard_button.isEnabled()
        title = window.text_viewer_dock.setWindowTitle.call_args[0][0]
        assert "read-only" in title.lower()

        # Even a programmatic text change must not arm the save path.
        window.text_viewer.setPlainText("scribble")
        assert not manager.text_editor_modified
        assert not window.save_button.isEnabled()

        # And save_file() must never reach the filesystem write.
        assert manager.save_file() is True
        window.file_manager.save_file.assert_not_called()

    def test_text_after_binary_restores_editable_save_path(self):
        window, manager = _make_env(BINARY_CONTENT)
        manager.view_file_content()
        assert window.text_viewer.isReadOnly()

        window.controller.read_file = lambda _path: TEXT_CONTENT
        manager.view_file_content()

        assert not window.text_viewer.isReadOnly()
        assert window.text_viewer.toPlainText() == TEXT_DECODED
        assert manager.original_text_content == TEXT_DECODED

    def test_physical_read_path_also_populates_both_views(self):
        window, manager = _make_env(TEXT_CONTENT, physical=True)

        manager.view_file_content()

        assert window.text_viewer.toPlainText() == TEXT_DECODED
        assert window.hex_viewer.toPlainText().splitlines()[0] == HEX_LINE_0_TEXT
        window.text_viewer_dock.raise_.assert_called_once()
        window.hex_viewer_dock.raise_.assert_not_called()
