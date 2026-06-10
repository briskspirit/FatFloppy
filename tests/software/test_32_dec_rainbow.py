"""
DEC Rainbow (RX50) MS-DOS FAT12 with 2:1 sector interleave.

The DEC Rainbow stores its 400KB RX50 disks (80 tracks, 1 side, 10 sectors,
512 bytes) as a no-BPB MS-DOS FAT12 with a 2:1 software sector interleave and
the first two tracks reserved. Read in raw physical order the FAT reads as
`fa e9 c6` (garbage) instead of `fa ff ff`, so nothing detected. These tests
assert FatFloppy now applies the RX50 interleave and reads the disk, using a
real public-domain MS-DOS 2.05 OEM image.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from fatfloppy.core.controller import DiskController  # noqa: E402
from fatfloppy.core.filesystems.fat12_fs import FATFilesystem  # noqa: E402

RES = Path(__file__).parent.parent / "resources" / "dec_rainbow"
IMAGE = "msdos205_rainbow_400k.imd"

# The full directory. Asserting the complete set pins the interleave: a wrong
# interleave reads a different/garbage listing. Real MS-DOS 2.05 system disk.
RAINBOW_FILES = {
    "BACKUP.EXE",
    "BASIC.COM",
    "BASICA.COM",
    "CHKDSK.COM",
    "COMMAND.COM",
    "CONFIG.SYS",
    "CREF.EXE",
    "DEBUG.COM",
    "DISKCOPY.COM",
    "EDLIN.COM",
    "EXE2BIN.EXE",
    "FC.EXE",
    "FIND.EXE",
    "FORMAT.COM",
    "IO.SYS",
    "LINK.EXE",
    "MASM.EXE",
    "MDRIVE.COM",
    "MDRIVE.SYS",
    "MEDIACHK.EXE",
    "MORE.COM",
    "MSDOS.SYS",
    "PRINT.COM",
    "RDCPM.EXE",
    "README.HLP",
    "RECOVER.COM",
    "SORT.EXE",
    "SYS.COM",
}


def _open():
    path = RES / IMAGE
    if not path.exists():
        pytest.skip(f"resource missing: {path}")
    controller = DiskController()
    assert controller.open_disk(str(path), disk_type="IMD"), "failed to open Rainbow"
    return controller


def test_dec_rainbow_detects_and_lists():
    controller = _open()
    assert isinstance(controller.filesystem, FATFilesystem), (
        f"Rainbow not detected as FAT12 (got {type(controller.filesystem).__name__})"
    )
    listing = {item["name"] for item in controller.list_directory("/")}
    assert listing == RAINBOW_FILES, (
        f"Rainbow listing {sorted(listing)} != expected (wrong interleave?)"
    )
    controller.close_disk()


def test_dec_rainbow_reads_all_files_with_correct_content():
    controller = _open()
    listing = [item["name"] for item in controller.list_directory("/")]
    for name in listing:
        assert controller.read_file("/" + name) is not None, f"could not read {name}"

    # A direct content check pinning the reserved-area/root-dir mapping: the
    # interleave must be exactly right for data clusters, not just the directory.
    config_sys = controller.read_file("/CONFIG.SYS")
    assert config_sys is not None
    assert config_sys.startswith(b"DEVICE=MDRIVE.SYS"), (
        f"CONFIG.SYS content looks wrong: {config_sys[:32]!r}"
    )
    controller.close_disk()
