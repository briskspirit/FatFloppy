"""
MITS Altair 8" CP/M disk with a non-standard DPB (Lifeboat CP/M).

The MITS Altair .DSK format stores 128-byte logical data in 137-byte hard
sectors. FatFloppy reads standard Altair CP/M disks via the shipped
DPB_8INCH_MITS profile, but Lifeboat's CP/M 2.2 distribution uses a different
disk layout (off=2 but a different block/skew config) that matches no shipped
DPB, so it scored 0 and did not detect. The MITS detector now falls back to a
config-less CP/M filesystem (the no-DPB scan) when no profiled DPB validates,
which infers this layout. Uses a real public-domain Lifeboat CP/M image.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from fatfloppy.core.controller import DiskController  # noqa: E402
from fatfloppy.core.filesystems.cpm_fs import CPMFilesystem  # noqa: E402

RES = Path(__file__).parent.parent / "resources" / "mits"

LIFEBOAT_FILES = {
    "ASM.COM",
    "CONFIG.COM",
    "COPY.COM",
    "DDT.COM",
    "DISKDEF.LIB",
    "DUMP.ASM",
    "DUMP.COM",
    "ED.COM",
    "FILECOPY.COM",
    "FORMAT.COM",
    "LOAD.COM",
    "MEMR.COM",
    "MEMR.DOC",
    "MOVCPM.COM",
    "PCGET.COM",
    "PCPUT.COM",
    "PIP.COM",
    "READ-ME.DOC",
    "SAVEUSER.COM",
    "SETCPM.COM",
    "STAT.COM",
    "SUBMIT.COM",
    "SYSGEN.COM",
    "USER.ASM",
    "XSUB.COM",
}


def test_lifeboat_mits_cpm_detects_and_reads():
    path = RES / "lifeboat_cpm22_8inch.dsk"
    if not path.exists():
        pytest.skip(f"resource missing: {path}")

    controller = DiskController()
    assert controller.open_disk(str(path), disk_type="MITS_DSK"), "failed to open"
    assert isinstance(controller.filesystem, CPMFilesystem), (
        f"not detected as CP/M (got {type(controller.filesystem).__name__})"
    )

    listing = {item["name"] for item in controller.list_directory("/")}
    assert listing == LIFEBOAT_FILES, (
        f"listing {sorted(listing)} != expected (wrong skew/DPB?)"
    )

    # Content check: PIP.COM carries DRI's copyright banner near its start.
    pip = controller.read_file("/PIP.COM")
    assert pip is not None and b"COPYR" in pip[:64], "PIP.COM content looks wrong"
    for name in listing:
        assert controller.read_file("/" + name) is not None, f"could not read {name}"
    controller.close_disk()
