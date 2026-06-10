"""
Robust, content-based driver auto-detection.

Driver selection used to be purely extension-based: opening a file routed it to
the driver registered for its extension (defaulting to IMG), so a correctly
formatted image with the "wrong" extension - or a programmatic open() that did
not name a driver - failed to detect. The factory now content-detects: it tries
file drivers in priority order and picks the first whose validate_for_opening
accepts the file, with the raw IMG fallback tried last. open_disk also defaults
to "auto".
"""

import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from fatfloppy.core.controller import DiskController  # noqa: E402
from fatfloppy.core.filesystems.cpm_fs import CPMFilesystem  # noqa: E402

RES = Path(__file__).parent.parent / "resources"


def test_auto_detects_misextensioned_mits(tmp_path):
    """A MITS Altair .dsk renamed to .img must still detect as CP/M under auto."""
    src = RES / "mits" / "lifeboat_cpm22_8inch.dsk"
    if not src.exists():
        pytest.skip(f"resource missing: {src}")
    renamed = tmp_path / "mislabeled.img"
    shutil.copy(src, renamed)

    controller = DiskController()
    assert controller.open_disk(str(renamed), disk_type="auto"), "failed to open"
    assert isinstance(controller.filesystem, CPMFilesystem), (
        f"misextensioned MITS not detected as CP/M "
        f"(got {type(controller.filesystem).__name__})"
    )
    assert type(controller.driver).__name__ == "MITSDSKDriver"
    controller.close_disk()


def test_open_disk_defaults_to_auto():
    """open_disk with no disk_type must content-detect, not force IMG."""
    path = RES / "imd_720k.imd"
    if not path.exists():
        pytest.skip(f"resource missing: {path}")
    controller = DiskController()
    assert controller.open_disk(str(path)), "default open failed"
    # The IMD must be opened with the IMD driver, not the raw IMG default.
    assert type(controller.driver).__name__ == "IMDImageDriver"
    assert controller.filesystem is not None
    controller.close_disk()


def test_auto_prefers_specific_driver_over_raw():
    """A real IMD opened as auto picks the IMD driver, not the raw IMG fallback."""
    path = RES / "imd_720k.imd"
    if not path.exists():
        pytest.skip(f"resource missing: {path}")
    controller = DiskController()
    assert controller.open_disk(str(path), disk_type="auto")
    assert type(controller.driver).__name__ == "IMDImageDriver"
    controller.close_disk()
