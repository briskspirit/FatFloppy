"""
Heathkit/Zenith H17 hard-sectored CP/M detection (sector-skew handling).

Heath/Zenith CP/M on the H17 controller stores its 100KB hard-sectored disks
with a 4:1 software sector interleave. The .h17disk container exposes the raw
hard-sector order, so reading the filesystem requires applying the CP/M soft
skew - without it the directory comes out scrambled and nothing is detected.

These tests assert FatFloppy now infers the layout *and skew* and reads such
disks (real public-domain CP/M 2.2 images), while never mis-detecting a
Heathkit HDOS disk - which shares the very same physical geometry - as CP/M.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from fatfloppy.core.controller import DiskController  # noqa: E402
from fatfloppy.core.filesystems.cpm_fs import CPMFilesystem  # noqa: E402

RES = Path(__file__).parent.parent / "resources" / "cpm_h17"

# (filename, full expected directory). Asserting the complete file set (not
# just membership) pins the inferred skew: a wrong skew reads a different subset
# of the directory sectors and yields a different listing, so this is a strong
# regression guard for the skew-sweeping scan. These are stock CP/M 2.2 disks.
CASES = [
    (
        "cpm22_h17_disk_i.h17disk",
        {
            "ASM.COM",
            "ASSIGN.COM",
            "BIOS.SYS",
            "CONFIGUR.COM",
            "DDT.COM",
            "DUP.COM",
            "ED.COM",
            "FORMAT.COM",
            "LOAD.COM",
            "MOVCPM17.COM",
            "PIP.COM",
            "STAT.COM",
            "SUBMIT.COM",
            "SYSGEN.COM",
            "XSUB.COM",
        },
    ),
    (
        "cpm22_h17_disk_1.h17disk",
        {
            "ASM.COM",
            "BIOS.SYS",
            "CONFIGUR.COM",
            "DDT.COM",
            "DUMP.ASM",
            "DUMP.COM",
            "DUP.COM",
            "ED.COM",
            "FORMAT.COM",
            "LOAD.COM",
            "MOVCPM5.COM",
            "MOVCPM8.COM",
            "PIP.COM",
            "STAT.COM",
            "SUBMIT.COM",
            "SYSGEN.COM",
            "XSUB.COM",
        },
    ),
]


@pytest.mark.parametrize("filename,expected_files", CASES, ids=[c[0] for c in CASES])
def test_h17disk_cpm_detects_and_reads(filename, expected_files):
    path = RES / filename
    if not path.exists():
        pytest.skip(f"resource missing: {path}")

    controller = DiskController()
    assert controller.open_disk(str(path), disk_type="H17"), (
        f"failed to open {filename}"
    )
    assert isinstance(controller.filesystem, CPMFilesystem), (
        f"{filename} not detected as CP/M (got {type(controller.filesystem).__name__})"
    )

    listing = [item["name"] for item in controller.list_directory("/")]
    assert set(listing) == expected_files, (
        f"{filename} listing {sorted(listing)} != expected {sorted(expected_files)} "
        "(wrong sector skew?)"
    )

    # The skew must be correct for data reads too: every file must read back as
    # non-empty data (CP/M directory entries with rc>0 always have content).
    for name in listing:
        data = controller.read_file("/" + name)
        assert data, f"could not read {name} from {filename}"
    controller.close_disk()


def test_h17_cpm_skew_does_not_false_detect_hdos():
    """The H17 CP/M skew scan must never claim a Heathkit HDOS disk as CP/M.

    HDOS uses the identical 40t/1h/10s/256B hard-sectored geometry, so this is
    the key false-positive guard for the new skew-sweeping scan.
    """
    from fatfloppy.core.filesystems.hdos_fs import HDOSFilesystem

    path = Path(__file__).parent.parent / "resources" / "HDOS" / "HDOS_2-0_TEST.h17disk"
    if not path.exists():
        pytest.skip(f"resource missing: {path}")
    controller = DiskController()
    assert controller.open_disk(str(path), disk_type="H17")
    assert isinstance(controller.filesystem, HDOSFilesystem), (
        f"HDOS disk mis-detected as {type(controller.filesystem).__name__}"
    )
    controller.close_disk()
