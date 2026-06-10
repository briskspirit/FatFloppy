"""
MITS Altair 5.25" minifloppy CP/M disk (35 tracks x 16 sectors x 137 bytes).

The Altair minifloppy uses the same 137-byte hard-sector framing as the 8"
disk but a smaller geometry (35x16) that the MITS driver previously rejected
(it hardcoded the 77x32 8" geometry and required the 8" file size). The driver
now derives its geometry from the file size and de-frames the mini, and the
detector's config-less CP/M scan infers the layout. Uses a real public-domain
Altair mini CP/M image.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from fatfloppy.core.controller import DiskController  # noqa: E402
from fatfloppy.core.filesystems.cpm_fs import CPMFilesystem  # noqa: E402

RES = Path(__file__).parent.parent / "resources" / "mits"

MINI_FILES = {
    "ACOPY.COM",
    "AFORMAT.COM",
    "ASM.COM",
    "COPY.COM",
    "DDT.COM",
    "DUMP.COM",
    "IOBYTE.TXT",
    "LOAD.COM",
    "LS.COM",
    "MOVCPM5.COM",
    "PCGET.COM",
    "PCPUT.COM",
    "PIP.COM",
    "STAT.COM",
    "SYSGEN.COM",
}


def test_mits_mini_cpm_detects_and_reads():
    path = RES / "cpm22at11_mini_5inch.dsk"
    if not path.exists():
        pytest.skip(f"resource missing: {path}")

    controller = DiskController()
    assert controller.open_disk(str(path), disk_type="MITS_DSK"), "failed to open"
    assert isinstance(controller.filesystem, CPMFilesystem), (
        f"not detected as CP/M (got {type(controller.filesystem).__name__})"
    )

    listing = {item["name"] for item in controller.list_directory("/")}
    assert listing == MINI_FILES, f"listing {sorted(listing)} != expected"

    # Content checks pin the de-framing + skew: STAT.COM carries DRI's copyright
    # banner, and IOBYTE.TXT is plain text.
    stat = controller.read_file("/STAT.COM")
    assert stat is not None and b"Copyri" in stat[:32], "STAT.COM looks wrong"
    iobyte = controller.read_file("/IOBYTE.TXT")
    assert iobyte is not None and iobyte.lstrip().startswith(b"CON device"), (
        "IOBYTE.TXT looks wrong"
    )
    for name in listing:
        assert controller.read_file("/" + name) is not None, f"could not read {name}"
    controller.close_disk()
