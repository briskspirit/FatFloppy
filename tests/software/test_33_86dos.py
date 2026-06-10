"""
86-DOS (Seattle Computer Products) no-BPB FAT12 with 2 reserved tracks.

86-DOS - the direct ancestor of MS-DOS - stored its 8" SSSD disks
(77t/1h/26s/128b, FM) as a no-BPB FAT12: x86 boot code (no BPB), media
descriptor 0xFE, and the first two tracks reserved so the FAT begins at track
2 (logical sector 52). The step-1 no-BPB synthesizer only looked for the FAT
at the standard reserved=1, so these disks did not detect. These tests assert
FatFloppy now recognizes the two-reserved-track layout and reads them, using
real public-domain SCP OEM images (including one with a re-formatted track).
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from fatfloppy.core.controller import DiskController  # noqa: E402
from fatfloppy.core.filesystems.fat12_fs import FATFilesystem  # noqa: E402

RES = Path(__file__).parent.parent / "resources" / "dos86"

# (filename, full expected directory). The complete set guards the reserved /
# FAT-offset geometry; a wrong reserved count reads a different listing.
CASES = [
    (
        "86dos_110_scp.imd",
        {
            "86DOS.SYS",
            "ASM.COM",
            "BOOT.ASM",
            "CHK.COM",
            "CHKDSK.COM",
            "COMMAND.COM",
            "CPMTAB.ASM",
            "DATE.COM",
            "DEBUG.COM",
            "DOSIO.ASM",
            "EDLIN.COM",
            "FLORIDA.BBS",
            "HEX2BIN.COM",
            "INIT.ASM",
            "INITLARG.COM",
            "INITSMAL.COM",
            "MAKRDCPM.COM",
            "MON.ASM",
            "RDCPM.BAK",
            "RDCPM.COM",
            "READTHIS.DOC",
            "RESET.ASM",
            "RESET.COM",
            "SYS.COM",
            "TIME.COM",
            "TRANS.COM",
        },
    ),
    (
        "86dos_114_scp.imd",
        {
            "86DOS.SYS",
            "ASM.COM",
            "BOOT.ASM",
            "CHKDSK.COM",
            "COMMAND.COM",
            "CPMTAB.ASM",
            "DATE.COM",
            "DEBUG.COM",
            "DOSIO.ASM",
            "EDLIN.COM",
            "HEX2BIN.COM",
            "INIT.ASM",
            "INIT.COM",
            "MAKRDCPM.COM",
            "MON.ASM",
            "NEWS.DOC",
            "RDCPM.COM",
            "READTHIS.DOC",
            "SYS.COM",
            "TIME.COM",
            "TRANS.COM",
        },
    ),
]


@pytest.mark.parametrize("filename,expected_files", CASES, ids=[c[0] for c in CASES])
def test_86dos_detects_and_reads(filename, expected_files):
    path = RES / filename
    if not path.exists():
        pytest.skip(f"resource missing: {path}")

    controller = DiskController()
    assert controller.open_disk(str(path), disk_type="IMD"), (
        f"failed to open {filename}"
    )
    assert isinstance(controller.filesystem, FATFilesystem), (
        f"{filename} not detected as FAT12 (got {type(controller.filesystem).__name__})"
    )

    listing = {item["name"] for item in controller.list_directory("/")}
    assert listing == expected_files, (
        f"{filename} listing {sorted(listing)} != expected (wrong reserved offset?)"
    )

    for name in listing:
        assert controller.read_file("/" + name) is not None, (
            f"could not read {name} from {filename}"
        )
    controller.close_disk()
