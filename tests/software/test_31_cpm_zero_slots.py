"""
CP/M disks with all-zero directory slots + skew disambiguation robustness.

Some CP/M disks carry an unused directory entry that is all-zero (0x00) rather
than the usual 0xE5 deleted marker. The validator used to treat any such slot as
a fatal "wrong interleave" signal and reject the whole disk - so a real 8" SSSD
CP/M disk (here a NorthStar Z-System disk, present as both a raw .img and a
variable-sectors-per-track .imd) failed to detect at all.

All-zero slots are now tolerated but *penalized*: a wrong sector skew on a sparse
disk pulls empty (all-zero) data sectors into the directory region, so the
cleanest reading must still win. These tests assert both halves: the zero-slot
disk now reads correctly, AND a known disk with two shipped interleave variants
still resolves to the correct one (no silent mis-skew / data corruption).
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from fatfloppy.core.controller import DiskController  # noqa: E402
from fatfloppy.core.filesystems.cpm_fs import CPMFilesystem  # noqa: E402

RES = Path(__file__).parent.parent / "resources" / "cpm_zero_slots"
CPM_RES = Path(__file__).parent.parent / "resources" / "CPM"

# The full directory of the NorthStar Z-System disk. Asserting the complete set
# pins the sector skew: a wrong skew yields a different listing. Both the .img
# (raw, matched to the 8" SSSD profile) and the .imd (embedded, variable SPT)
# must decode to exactly this.
DISK3_FILES = {
    "AUTO.COM",
    "CPM52.COM",
    "DDT.COM",
    "DEBUG.COM",
    "DISK017.[#]",
    "ET.COM",
    "KLEEN.COM",
    "KLEENDOC.",
    "LOAD.COM",
    "NZBIOS.ASM",
    "NZBIOS.PRN",
    "SYSGEN.COM",
    "TORX.COM",
    "VID.ASM",
    "WSU.COM",
    "XDIR.COM",
    "ZASM.COM",
    "ZBOOT.ASM",
    "ZBOOT.HEX",
    "ZBUG.REL",
}


@pytest.mark.parametrize(
    "filename,disk_type",
    [
        ("northstar_zsys_disk3.img", "IMG"),
        ("northstar_zsys_disk3.imd", "IMD"),
    ],
)
def test_cpm_zero_slot_disk_detects_and_reads(filename, disk_type):
    path = RES / filename
    if not path.exists():
        pytest.skip(f"resource missing: {path}")

    controller = DiskController()
    assert controller.open_disk(str(path), disk_type=disk_type), (
        f"failed to open {filename}"
    )
    assert isinstance(controller.filesystem, CPMFilesystem), (
        f"{filename} not detected as CP/M (got {type(controller.filesystem).__name__})"
    )

    listing = [item["name"] for item in controller.list_directory("/")]
    # No phantom all-zero entry must leak into the listing as a null-named file.
    assert "" not in listing and all(name.strip("\x00") for name in listing), (
        f"{filename} listing contains a phantom empty entry: {listing}"
    )
    assert set(listing) == DISK3_FILES, (
        f"{filename} listing {sorted(listing)} != expected (wrong skew?)"
    )

    for name in listing:
        assert controller.read_file("/" + name) is not None, (
            f"could not read {name} from {filename}"
        )
    controller.close_disk()


def test_zero_slot_tolerance_preserves_skew_disambiguation():
    """A disk with two shipped interleave variants must resolve to the correct
    one. disk1.img reads as garbage under interleave4 but cleanly under
    interleave6; tolerating zero-slots must not let the wrong variant win."""
    path = CPM_RES / "disk1.img"
    if not path.exists():
        pytest.skip(f"resource missing: {path}")
    controller = DiskController()
    assert controller.open_disk(str(path), disk_type="IMG")
    detected_format_name, _, _ = controller.detect_format()
    assert detected_format_name == "cpm_8_sssd_250k_interleave6", (
        f"disk1.img mis-detected as {detected_format_name} (wrong interleave)"
    )
    # The correct interleave reads this ASM file as a comment banner, not a
    # mid-file fragment - a direct content check that the skew is right.
    data = controller.read_file("/DISKTEST.ASM")
    assert data is not None and data.lstrip().startswith(b";"), (
        "DISKTEST.ASM content looks mis-skewed"
    )
    controller.close_disk()
