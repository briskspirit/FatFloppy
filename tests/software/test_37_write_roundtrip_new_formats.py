"""
Write-read round-trip on the vintage formats added in this work.

Each format was added read-first; these tests prove writing works too: open a
real image, write a new file, flush, reopen (via auto-detection) and verify the
new file reads back while every pre-existing file stays intact. This covers the
skew/interleave write paths (H17, Rainbow, MITS) and guards the MITS mini write
fix (data tracks were being reframed as system tracks on write).
"""

import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from fatfloppy.core.controller import DiskController  # noqa: E402

RES = Path(__file__).parent.parent / "resources"

# (label, relative image path). Mix of FAT and CP/M across IMG/IMD/H17/MITS.
CASES = [
    ("nobpb_fat12", "nobpb_fat12/pcdos100_160k.img"),
    ("nodpb_cpm", "cpm_extra/kaypro2_cpm22.imd"),
    ("h17_cpm", "cpm_h17/cpm22_h17_disk_i.h17disk"),
    ("cpm_zero_slots", "cpm_zero_slots/northstar_zsys_disk3.img"),
    ("dec_rainbow", "dec_rainbow/msdos205_rainbow_400k.imd"),
    ("dos86", "dos86/86dos_110_scp.imd"),
    ("mits_8inch", "mits/lifeboat_cpm22_8inch.dsk"),
    ("mits_mini", "mits/cpm22at11_mini_5inch.dsk"),
]

PAYLOAD = b"WRITE ROUNDTRIP 0123456789 ABCDEFG"


@pytest.mark.parametrize("label,relpath", CASES, ids=[c[0] for c in CASES])
def test_write_roundtrip(label, relpath, tmp_path):
    src = RES / relpath
    if not src.exists():
        pytest.skip(f"resource missing: {src}")
    img = tmp_path / src.name
    shutil.copy(src, img)

    controller = DiskController()
    assert controller.open_disk(str(img), disk_type="auto"), f"open failed: {label}"
    before = {item["name"] for item in controller.list_directory("/")}
    assert before, f"{label}: no existing files to guard"
    sample = sorted(before)[0]
    sample_before = controller.read_file("/" + sample)

    controller.write_file("/RT.TST", PAYLOAD)
    controller.flush()
    controller.close_disk()

    # Reopen via auto-detection - the written image must still be recognized.
    verifier = DiskController()
    assert verifier.open_disk(str(img), disk_type="auto"), f"reopen failed: {label}"
    after = {item["name"] for item in verifier.list_directory("/")}

    assert "RT.TST" in after, f"{label}: new file missing after reopen"
    readback = verifier.read_file("/RT.TST")
    # FAT returns the exact bytes; CP/M pads the final record with 0x1A, so the
    # payload is the prefix either way.
    assert readback is not None and readback[: len(PAYLOAD)] == PAYLOAD, (
        f"{label}: new file content wrong: {readback[:48]!r}"
    )

    # Every pre-existing file must survive the write unchanged.
    assert before <= after, f"{label}: lost files: {before - after}"
    assert verifier.read_file("/" + sample) == sample_before, (
        f"{label}: existing file {sample} corrupted by write"
    )
    verifier.close_disk()
