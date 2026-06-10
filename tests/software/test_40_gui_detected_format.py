"""
GUI surfaces the auto-detected container/driver and format profile.

The "Detected Format" panel reports which driver opened the image (now chosen by
content, not extension) and which format profile matched - or that the layout
was inferred when no shipped profile applies. These tests drive
DiskManager.update_detected_format_info with a real opened controller and a
captured label, the same lightweight style as the other GUI tests.
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

from fatfloppy.core.controller import DiskController  # noqa: E402
from fatfloppy.gui.managers.disk_manager import DiskManager  # noqa: E402

RES = Path(__file__).parent.parent / "resources"


def _detected_text(image_path: str) -> str:
    controller = DiskController()
    assert controller.open_disk(image_path, disk_type="auto")
    label = MagicMock()
    manager = DiskManager.__new__(DiskManager)
    manager.logger = logging.getLogger("test")
    manager.parent = SimpleNamespace(controller=controller, detected_format_info=label)
    manager.update_detected_format_info()
    controller.close_disk()
    assert label.setText.called, "label was not updated"
    return label.setText.call_args[0][0]


def test_detected_format_shows_driver_and_matched_profile():
    """A profile-matched disk shows its container driver and the profile."""
    path = RES / "dec_rainbow" / "msdos205_rainbow_400k.imd"
    if not path.exists():
        pytest.skip(f"resource missing: {path}")
    text = _detected_text(str(path))
    assert "Container: IMD" in text
    assert "dec_rainbow_400k" in text
    assert "DEC Rainbow" in text  # the profile description


def test_detected_format_shows_mits_driver_for_dsk():
    """A MITS .dsk surfaces the MITS container (content-detected, not extension)."""
    path = RES / "mits" / "cpm22at11_mini_5inch.dsk"
    if not path.exists():
        pytest.skip(f"resource missing: {path}")
    text = _detected_text(str(path))
    assert "Container: MITS_DSK" in text


def test_detected_format_reports_inferred_when_no_profile():
    """An 86-DOS disk (no-BPB FAT12, no shipped profile) shows as auto-inferred."""
    path = RES / "dos86" / "86dos_110_scp.imd"
    if not path.exists():
        pytest.skip(f"resource missing: {path}")
    text = _detected_text(str(path))
    assert "Container: IMD" in text
    assert "auto-inferred (FAT" in text


def test_detected_format_inferred_cpm():
    """A no-DPB CP/M disk shows its driver and an inferred CP/M layout."""
    path = RES / "cpm_extra" / "kaypro2_cpm22.imd"
    if not path.exists():
        pytest.skip(f"resource missing: {path}")
    text = _detected_text(str(path))
    assert "Container: IMD" in text
    assert "auto-inferred (CPM)" in text
