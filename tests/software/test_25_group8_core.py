"""
Regression tests for Group 8: core orchestration fixes.

Covers:
  * controller.py:414 - export failure cleanup must not delete a pre-existing
    target, and export must refuse source == target
  * controller.py:432 - flush failures propagate (covered in test_12 too)
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from fatfloppy.core.controller import DiskController  # noqa: E402


def _open_img(tmp_path):
    ctrl = DiskController()
    profile_name = "ibm_5.25_160k"
    profile = ctrl.get_format_by_name(profile_name)
    img = tmp_path / "src.img"
    img.write_bytes(b"\x00" * profile.physical_format.total_bytes)
    assert ctrl.open_disk(
        str(img), disk_type="IMG", format_info={"format_name": profile_name}
    )
    assert ctrl.format_disk_media(profile_name)
    return ctrl, img


def test_export_failure_preserves_preexisting_target(tmp_path):
    ctrl, _ = _open_img(tmp_path)
    target = tmp_path / "important.imd"
    target.write_bytes(b"PRECIOUS USER DATA")

    with (
        patch(
            "fatfloppy.core.controller.DriverFactory.create",
            side_effect=OSError("boom"),
        ),
        pytest.raises(OSError),
    ):
        ctrl.export_disk(str(target), "IMD")

    assert target.read_bytes() == b"PRECIOUS USER DATA", (
        "export failure must not delete a target the user already had"
    )


def test_export_refuses_source_equals_target(tmp_path):
    ctrl, img = _open_img(tmp_path)
    with pytest.raises(ValueError):
        ctrl.export_disk(str(img), "IMD")  # cross-format onto the source file
    ctrl.close_disk()
