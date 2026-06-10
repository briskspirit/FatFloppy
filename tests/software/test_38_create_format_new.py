"""
Create-and-format new images for the format profiles added in this work.

format_disk_media creates a blank image and lays down an empty filesystem. These
tests create each new profile from scratch, confirm the fresh disk is empty and
valid, then write a file and read it back after reopening - exercising the
create + format + write path (including the MITS mini blank-geometry fix and the
DEC Rainbow interleaved layout).
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from fatfloppy.core.controller import DiskController  # noqa: E402
from fatfloppy.core.filesystems.cpm_fs import CPMFilesystem  # noqa: E402
from fatfloppy.core.filesystems.fat12_fs import FATFilesystem  # noqa: E402

# (profile, file extension, driver type, expected filesystem class)
CASES = [
    ("dec_rainbow_400k", ".imd", "IMD", FATFilesystem),
    ("dec_rainbow_400k", ".img", "IMG", FATFilesystem),
    ("cpm_5.25_100k", ".img", "IMG", CPMFilesystem),
    ("cpm_8_mits_dsk_308k", ".dsk", "MITS_DSK", CPMFilesystem),
    ("cpm_5.25_mits_mini_70k", ".dsk", "MITS_DSK", CPMFilesystem),
]

PAYLOAD = b"FRESHLY FORMATTED IMAGE 98765"


@pytest.mark.parametrize(
    "profile,ext,driver,fs_class", CASES, ids=[f"{c[0]}{c[1]}" for c in CASES]
)
def test_create_format_write(profile, ext, driver, fs_class, tmp_path):
    img = tmp_path / f"new{ext}"

    controller = DiskController()
    assert controller.format_disk_media(
        format_name=profile,
        volume_label="TEST",
        file_path=str(img),
        disk_type=driver,
    ), f"create/format failed for {profile}"
    assert isinstance(controller.filesystem, fs_class), (
        f"{profile}: wrong fs {type(controller.filesystem).__name__}"
    )
    # A freshly formatted disk has no files.
    assert controller.list_directory("/") == [], f"{profile}: not empty after format"

    controller.write_file("/RT.TST", PAYLOAD)
    controller.flush()
    controller.close_disk()

    # Reopen via auto-detection and verify the written file.
    verifier = DiskController()
    assert verifier.open_disk(str(img), disk_type="auto"), f"reopen failed: {profile}"
    assert isinstance(verifier.filesystem, fs_class)
    listing = {item["name"] for item in verifier.list_directory("/")}
    assert listing == {"RT.TST"}, f"{profile}: unexpected listing {listing}"
    readback = verifier.read_file("/RT.TST")
    assert readback is not None and readback[: len(PAYLOAD)] == PAYLOAD, (
        f"{profile}: content wrong {readback[:48]!r}"
    )
    verifier.close_disk()
