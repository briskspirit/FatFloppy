"""
No-DPB CP/M detection (disks with no matching DPB profile).

CP/M has no on-disk signature or BPB; detection needs a Disk Parameter Block.
Disks whose geometry isn't covered by a shipped profile (Zenith Z-100, Kaypro II,
...) previously read as "no filesystem". These tests assert FatFloppy now infers
the layout and reads them - using real public-domain images - while never
mis-detecting a FAT12 disk as CP/M.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from fatfloppy.core.controller import DiskController  # noqa: E402
from fatfloppy.core.filesystems.cpm_fs import CPMFilesystem  # noqa: E402
from fatfloppy.core.filesystems.fat12_fs import FATFilesystem  # noqa: E402

CPM_RES = Path(__file__).parent.parent / "resources" / "cpm_extra"

# (filename, an expected file that must list and read back)
CASES = [
    ("kaypro2_cpm22.imd", "PIP.COM"),
    ("cpm85_z100.imd", "ASM.COM"),
]


@pytest.mark.parametrize("filename,expected_file", CASES, ids=[c[0] for c in CASES])
def test_nodpb_cpm_detects_and_reads(filename, expected_file):
    path = CPM_RES / filename
    if not path.exists():
        pytest.skip(f"resource missing: {path}")

    controller = DiskController()
    assert controller.open_disk(str(path), disk_type="IMD"), (
        f"failed to open {filename}"
    )
    assert isinstance(controller.filesystem, CPMFilesystem), (
        f"{filename} not detected as CP/M (got {type(controller.filesystem).__name__})"
    )

    listing = [item["name"] for item in controller.list_directory("/")]
    assert expected_file in listing, (
        f"{expected_file} not in {filename} listing: {listing[:12]}"
    )

    data = controller.read_file("/" + expected_file)
    assert data, f"could not read {expected_file} from {filename}"
    controller.close_disk()


@pytest.mark.parametrize(
    "filename,disk_type",
    [
        ("nobpb_fat12/pcdos100_160k.imd", "IMD"),
        ("nobpb_fat12/msdos125_compaq_320k.img", "IMG"),
        ("empty_formatted_144m.img", "IMG"),
    ],
)
def test_nodpb_cpm_scan_does_not_false_detect_fat(filename, disk_type):
    """The CP/M layout scan must never claim a FAT12 disk as CP/M."""
    path = Path(__file__).parent.parent / "resources" / filename
    if not path.exists():
        pytest.skip(f"resource missing: {path}")
    controller = DiskController()
    controller.open_disk(str(path), disk_type=disk_type)
    assert isinstance(controller.filesystem, FATFilesystem), (
        f"{filename} (FAT12) was mis-detected as {type(controller.filesystem).__name__}"
    )
    controller.close_disk()
