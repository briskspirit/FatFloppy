"""End-to-end CBM detection through DiskController."""

from pathlib import Path

import pytest

from fatfloppy.core.controller import DiskController

RESOURCES = Path(__file__).parent.parent / "resources" / "CBM"
CASES = [
    ("vic1541_bam.d64", "1541"),
    ("c128_tutorial.d64", "1541"),
    ("endless_forms.d64", "1541"),
    ("1571_demo.d71", "1571"),
    ("1581_demo.d81", "1581"),
]


@pytest.mark.parametrize("image,variant", CASES)
def test_controller_detects_cbm(image, variant):
    path = RESOURCES / image
    if not path.exists():
        pytest.skip(f"resource {image} not present")
    controller = DiskController()
    assert controller.open_disk(str(path), disk_type="auto")
    fs = controller.filesystem
    assert fs is not None, "expected a filesystem to be detected"
    config = fs.get_specific_config()
    assert config.variant == variant
    files = controller.list_directory("/")
    assert files
    controller.close_disk()


def test_cpm_plus_d64_detects_as_cpm():
    # Real C128 CP/M Plus disk in a D64 container: CP/M must win, CBM must not.
    path = RESOURCES / "cpm_plus_30.d64"
    if not path.exists():
        pytest.skip("resource not present")
    controller = DiskController()
    assert controller.open_disk(str(path), disk_type="auto")
    fs = controller.filesystem
    assert fs is not None
    assert fs.filesystem_type == "CPM"
    controller.close_disk()


def test_profiles_registered():
    controller = DiskController()
    names = [name for name, _desc in controller.list_formats()]
    assert any("d64" in n for n in names)
    assert any("d71" in n for n in names)
    assert any("d81" in n for n in names)
