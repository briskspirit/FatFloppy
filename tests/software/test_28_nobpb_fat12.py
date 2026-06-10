"""
No-BPB FAT12 detection (pre-DOS-2.0 / early OEM disks).

DOS 1.x and several early OEM disks have no BIOS Parameter Block in the boot
sector - the format was identified by the FAT media-descriptor byte. These tests
assert FatFloppy now auto-detects and reads such disks (both embedded-geometry
IMD/IMD-8inch images and raw IMG images), using real public-domain disk images.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from fatfloppy.core.controller import DiskController  # noqa: E402
from fatfloppy.core.filesystems.fat12_fs import FATFilesystem  # noqa: E402

RES = Path(__file__).parent.parent / "resources" / "nobpb_fat12"

# (filename, disk_type, an expected file that must list and read back)
CASES = [
    ("pcdos100_160k.imd", "IMD", "COMMAND.COM"),
    ("pcdos100_160k.img", "IMG", "COMMAND.COM"),
    ("msdos125_compaq_320k.img", "IMG", "COMMAND.COM"),
    ("msdos125_panasonic_160k.imd", "IMD", "COMMAND.COM"),
    ("msdos200_scp_8inch.imd", "IMD", "COMMAND.COM"),
]


@pytest.mark.parametrize(
    "filename,disk_type,expected_file", CASES, ids=[c[0] for c in CASES]
)
def test_nobpb_fat12_detects_and_reads(filename, disk_type, expected_file):
    path = RES / filename
    if not path.exists():
        pytest.skip(f"resource missing: {path}")

    controller = DiskController()
    assert controller.open_disk(str(path), disk_type=disk_type), (
        f"failed to open {filename}"
    )
    assert isinstance(controller.filesystem, FATFilesystem), (
        f"{filename} not detected as FAT12 (got {type(controller.filesystem).__name__})"
    )

    listing = [item["name"] for item in controller.list_directory("/")]
    assert expected_file in listing, (
        f"{expected_file} not in {filename} listing: {listing[:12]}"
    )

    data = controller.read_file("/" + expected_file)
    assert data, f"could not read {expected_file} from {filename}"
    controller.close_disk()


CPM_RES = Path(__file__).parent.parent / "resources" / "cpm_extra"


@pytest.mark.parametrize("filename", ["kaypro2_cpm22.imd", "cpm85_z100.imd"])
def test_nobpb_path_does_not_false_detect_cpm_as_fat(filename):
    """The no-BPB FAT12 path must never make a CP/M disk score as valid FAT12."""
    from fatfloppy.core.disk import Disk
    from fatfloppy.core.drivers.imd import IMDImageDriver

    path = CPM_RES / filename
    if not path.exists():
        pytest.skip(f"resource missing: {path}")

    driver = IMDImageDriver(str(path))
    disk = Disk(driver)
    disk.set_geometry(driver.physical_format)
    fs = FATFilesystem(disk)

    # The synthesizer must refuse this CP/M disk, so the FAT score stays low.
    assert fs._try_synthesize_nobpb_bpb() is None, (
        f"{filename} wrongly synthesized FAT12"
    )
    assert fs.get_validity_score() < fs.validity_threshold, (
        f"{filename} (CP/M) wrongly scored as valid FAT12"
    )


@pytest.mark.parametrize(
    "filename,disk_type",
    [("empty_formatted_144m.img", "IMG"), ("imd_720k.imd", "IMD")],
)
def test_bpb_fat12_detection_still_works(filename, disk_type):
    """Adding no-BPB support must not break normal BPB-based FAT12 detection."""
    path = Path(__file__).parent.parent / "resources" / filename
    if not path.exists():
        pytest.skip(f"resource missing: {path}")
    controller = DiskController()
    assert controller.open_disk(str(path), disk_type=disk_type)
    assert isinstance(controller.filesystem, FATFilesystem)
    # The synthesized-no-BPB OEM tag must NOT be used for a real BPB disk.
    cfg = controller.active_filesystem_config
    assert getattr(cfg, "oem_id", "") != "FATFLNBP"
    controller.close_disk()
