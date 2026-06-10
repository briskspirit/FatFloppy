"""
Bug-proof regression tests for Group 1: physical-disk safety.

These tests encode the audit's critical/high findings for the Greaseweazle driver
and the physical-disk GUI path. They are written to FAIL against the pre-fix code
and PASS once the fixes land.

Covered findings:
  * greaseweazle.py:494/797 - 0-based logical sector vs physical IDAM id off-by-one
  * greaseweazle.py:481     - read-your-writes (read_sector ignores pending writes)
  * greaseweazle.py:524     - set_physical_format leaves stale decoded caches
  * dialogs.py:254          - DriveSelectionDialog.get_selection references missing widgets
  * disk_manager.py:619     - detect_format() 3-tuple unpacked into 2 variables
  * disk_manager.py:97      - "Create Disk Image" reformats the currently open disk
"""

import copy
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from fatfloppy.core.drivers.greaseweazle import (  # noqa: E402
    GREASEWEAZLE_AVAILABLE,
    GreaseweazleDriver,
)
from fatfloppy.core.filesystem_registry import FilesystemRegistry  # noqa: E402

_ALL_FORMATS = FilesystemRegistry.get_all_formats()
FMT_144 = _ALL_FORMATS["ibm_3.5_1.44m"]

pytestmark = pytest.mark.skipif(
    not GREASEWEAZLE_AVAILABLE, reason="greaseweazle library not installed"
)


def _make_driver():
    """Builds a GreaseweazleDriver with FMT_144 geometry, no real USB."""
    driver = GreaseweazleDriver()
    driver.physical_format = copy.deepcopy(FMT_144.physical_format)
    driver.initialized = True  # skip USB initialize()
    driver.usb = MagicMock()
    return driver


# --------------------------------------------------------------------------- #
# greaseweazle.py:494/797 - logical/physical sector mapping
# --------------------------------------------------------------------------- #


def test_read_sector_logical_zero_returns_first_physical_sector():
    """Logical sector 0 must return the first on-disk sector, not a zero buffer."""
    driver = _make_driver()
    tf = driver.physical_format.track_formats[0]
    bps = driver.physical_format.bytes_per_sector
    id_start = tf.id_start
    spt = tf.sectors_per_track

    # On-disk sectors are keyed by their IDAM id (id_start .. id_start+spt-1).
    driver.track_data[(0, 0)] = {
        id_start + i: bytes([id_start + i]) * bps for i in range(spt)
    }

    # Logical index i -> physical id (id_start + i).
    assert driver.read_sector(0, 0, 0) == bytes([id_start]) * bps
    assert driver.read_sector(0, 0, spt - 1) == bytes([id_start + spt - 1]) * bps


def test_write_sector_targets_correct_physical_sector():
    """A write to logical sector i must encode into physical id (id_start + i)."""
    driver = _make_driver()
    tf = driver.physical_format.track_formats[0]
    bps = driver.physical_format.bytes_per_sector
    id_start = tf.id_start

    payload = bytes([0xAB]) * bps
    driver.write_sector(0, 0, 0, payload)

    # The buffered dirty sector must be keyed by the physical id of logical 0.
    assert id_start in driver.dirty_sectors[(0, 0)]
    assert driver.dirty_sectors[(0, 0)][id_start] == payload


# --------------------------------------------------------------------------- #
# greaseweazle.py:481 - read-your-writes
# --------------------------------------------------------------------------- #


def test_read_returns_pending_write_before_flush():
    """Reading a sector after writing it (before flush) returns the new data."""
    driver = _make_driver()
    tf = driver.physical_format.track_formats[0]
    bps = driver.physical_format.bytes_per_sector
    id_start = tf.id_start
    spt = tf.sectors_per_track

    # Pretend the track was already read with old contents.
    driver.track_data[(0, 0)] = {id_start + i: bytes([0x00]) * bps for i in range(spt)}

    payload = bytes([0xCC]) * bps
    driver.write_sector(0, 0, 5, payload)

    assert driver.read_sector(0, 0, 5) == payload


# --------------------------------------------------------------------------- #
# greaseweazle.py:524 - set_physical_format must invalidate stale caches
# --------------------------------------------------------------------------- #


def test_set_physical_format_invalidates_caches():
    """Changing the physical format must drop stale decoded sector data."""
    driver = _make_driver()
    driver.track_data[(0, 0)] = {1: b"stale"}
    driver.sector_cache[(0, 0, 0)] = b"stale"

    with patch(
        "fatfloppy.core.drivers.greaseweazle.create_greaseweazle_diskdef",
        return_value=MagicMock(),
    ):
        driver.set_physical_format(copy.deepcopy(FMT_144.physical_format))

    assert driver.track_data == {}
    assert driver.sector_cache == {}


# --------------------------------------------------------------------------- #
# GUI: dialogs.py:254 - DriveSelectionDialog.get_selection
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def qapp():
    """Provides a single offscreen QApplication for the GUI tests."""
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.mark.usefixtures("qapp")
def test_drive_selection_dialog_get_selection_returns_triple():
    """get_selection() must return (drive_letter, drive_size, format_info)."""
    from fatfloppy.gui.dialogs import DriveSelectionDialog

    # Make device-presence check fast/deterministic (no hardware).
    with patch("greaseweazle.tools.util.usb_open", side_effect=OSError("no device")):
        dialog = DriveSelectionDialog(parent=None)

    result = dialog.get_selection()

    assert isinstance(result, tuple)
    assert len(result) == 3
    drive_letter, drive_size, format_info = result
    assert drive_letter in ("A", "B")
    assert drive_size in ("3.5", "5.25", "8")
    assert isinstance(format_info, dict)


# --------------------------------------------------------------------------- #
# GUI: disk_manager.py:619 - detect_format() 3-tuple unpack
# --------------------------------------------------------------------------- #


def _make_disk_manager():
    """Builds a DiskManager attached to a throwaway offscreen main window."""
    from PyQt6.QtWidgets import QMainWindow

    from fatfloppy.gui.managers.disk_manager import DiskManager

    window = QMainWindow()
    window.logger = MagicMock()
    window.controller = MagicMock()
    dm = DiskManager(window)
    return dm, window


@pytest.mark.usefixtures("qapp")
def test_finalize_physical_open_does_not_crash_on_detect_format():
    """Finishing a physical open must not crash unpacking the 3-tuple result."""
    dm, window = _make_disk_manager()
    window.controller.detect_format.return_value = ("FAT12", None, None)

    # Must not raise ValueError("too many values to unpack").
    dm._finalize_disk_open(
        "Drive A", is_image=False, drive_letter="A", drive_size="3.5", format_info={}
    )

    window.controller.detect_format.assert_called_once()


# --------------------------------------------------------------------------- #
# GUI: disk_manager.py:97 - create_disk_image must not touch the open disk
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("qapp")
def test_create_disk_image_uses_fresh_controller():
    """Creating a new image must never reformat the currently open disk."""
    dm, window = _make_disk_manager()

    # Simulate an already-open disk on the live controller.
    open_controller = window.controller
    open_controller.disk = object()

    profile = MagicMock()
    profile.name = "ibm_3.5_1.44m"
    profile.physical_format = MagicMock()
    profile.filesystem_config = MagicMock()

    fresh_controller = MagicMock()
    fresh_controller.format_disk_media.return_value = True

    fake_dialog = MagicMock()
    fake_dialog.exec.return_value = True
    fake_dialog.get_selection.return_value = (
        "/tmp/new.img",
        {"profile_name": "ibm_3.5_1.44m"},
        "NONAME",
        "IMG",
    )

    with (
        patch("fatfloppy.gui.dialogs.CreateImageDialog", return_value=fake_dialog),
        patch(
            "fatfloppy.gui.managers.disk_manager.DiskController",
            return_value=fresh_controller,
        ),
        patch.object(dm, "_get_format_profile", return_value=profile),
        patch.object(dm, "_finalize_disk_open"),
    ):
        dm.create_disk_image()

    # The new image is created on a fresh controller, never by reformatting
    # the disk that is already open.
    open_controller.format_disk_media.assert_not_called()
    fresh_controller.format_disk_media.assert_called_once()
