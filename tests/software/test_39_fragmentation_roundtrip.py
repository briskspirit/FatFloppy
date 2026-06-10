"""
Delete/rewrite fragmentation stress for the formats added in this work.

The write tests cover simple append. These churn the filesystem: write many
multi-block files, delete alternating ones to fragment the free space, then
write new files that must reuse the scattered free blocks/clusters - repeated
over several rounds. After a reopen every surviving file must read back intact
and every deleted file must be gone, proving the FAT cluster and CP/M block
allocators (across the new geometries, skews and interleaves) stay consistent
under fragmentation. A "disk/directory full" condition is a legitimate stop, not
a failure - the disk must simply remain readable.
"""

import hashlib
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from fatfloppy.core.controller import DiskController  # noqa: E402

RES = Path(__file__).parent.parent / "resources"

# Multi-block payload so files span several clusters/blocks and fragmentation
# forces non-contiguous allocation.
PAYLOAD_SIZE = 1500
RESERVE = 2048
SAFETY_CAP = 40
ROUNDS = 3


def _payload(seed: str) -> bytes:
    block = hashlib.sha256(seed.encode()).digest()
    return (block * ((PAYLOAD_SIZE // len(block)) + 1))[:PAYLOAD_SIZE]


def _write_until_full(ctrl: DiskController, prefix: str, expected: dict) -> None:
    """Write files until the disk (data or directory) is full or a cap is hit."""
    for i in range(SAFETY_CAP):
        free, _ = ctrl.get_free_space()
        if free < PAYLOAD_SIZE + RESERVE:
            break
        name = f"{prefix}{i:03d}.DAT"
        payload = _payload(name)
        try:
            ctrl.write_file("/" + name, payload)
        except OSError:
            break  # directory or disk full - a legitimate boundary
        expected[name] = payload


def _churn(ctrl: DiskController) -> dict:
    """Fill, then repeatedly delete-alternating and refill. Returns survivors."""
    expected: dict = {}
    _write_until_full(ctrl, "A", expected)
    for r in range(ROUNDS):
        for name in sorted(expected)[::2]:
            ctrl.delete_item("/" + name)
            del expected[name]
        _write_until_full(ctrl, f"B{r}", expected)
    return expected


def _open_fresh(profile: str, driver: str, ext: str, tmp_path: Path):
    img = tmp_path / f"frag{ext}"
    ctrl = DiskController()
    assert ctrl.format_disk_media(
        format_name=profile, volume_label="FRAG", file_path=str(img), disk_type=driver
    ), f"create/format failed: {profile}"
    return ctrl, img, set()  # no pre-existing files


def _open_real(relpath: str, tmp_path: Path):
    src = RES / relpath
    if not src.exists():
        pytest.skip(f"resource missing: {src}")
    img = tmp_path / src.name
    shutil.copy(src, img)
    ctrl = DiskController()
    assert ctrl.open_disk(str(img), disk_type="auto"), f"open failed: {relpath}"
    preexisting = {item["name"] for item in ctrl.list_directory("/")}
    return ctrl, img, preexisting


# (id, fresh-profile-or-None, driver/ext or real relpath)
FRESH_CASES = [
    ("dec_rainbow_fresh", "dec_rainbow_400k", "IMD", ".imd"),
    ("cpm_525_100k_fresh", "cpm_5.25_100k", "IMG", ".img"),
    ("mits_8inch_fresh", "cpm_8_mits_dsk_308k", "MITS_DSK", ".dsk"),
    ("mits_mini_fresh", "cpm_5.25_mits_mini_70k", "MITS_DSK", ".dsk"),
]

REAL_CASES = [
    ("dos86_real", "dos86/86dos_110_scp.imd"),
    ("nobpb_fat12_real", "nobpb_fat12/pcdos100_160k.img"),
    ("h17_cpm_real", "cpm_h17/cpm22_h17_disk_i.h17disk"),
    ("nodpb_cpm_real", "cpm_extra/kaypro2_cpm22.imd"),
    ("zero_slot_cpm_real", "cpm_zero_slots/northstar_zsys_disk3.img"),
]


def _verify(img: Path, expected: dict, preexisting: set, fresh: bool):
    verifier = DiskController()
    assert verifier.open_disk(str(img), disk_type="auto"), "reopen after churn failed"
    after = {item["name"] for item in verifier.list_directory("/")}
    try:
        # Every survivor reads back with the right content.
        for name, payload in expected.items():
            assert name in after, f"survivor {name} missing after reopen"
            data = verifier.read_file("/" + name)
            assert data is not None and data[: len(payload)] == payload, (
                f"{name} content corrupted after churn"
            )
        # Pre-existing files (real images) must be untouched by the churn.
        for name in preexisting:
            assert name in after, f"pre-existing {name} lost"
        # On a fresh disk nothing else may linger (no stale directory entries).
        if fresh:
            assert after == set(expected), (
                f"stale/extra entries after churn: {after ^ set(expected)}"
            )
    finally:
        verifier.close_disk()


@pytest.mark.parametrize(
    "label,profile,driver,ext", FRESH_CASES, ids=[c[0] for c in FRESH_CASES]
)
def test_fragmentation_fresh(label, profile, driver, ext, tmp_path):
    ctrl, img, preexisting = _open_fresh(profile, driver, ext, tmp_path)
    expected = _churn(ctrl)
    assert len(expected) >= 4, f"{label}: churn wrote too few files ({len(expected)})"
    ctrl.flush()
    ctrl.close_disk()
    _verify(img, expected, preexisting, fresh=True)


@pytest.mark.parametrize("label,relpath", REAL_CASES, ids=[c[0] for c in REAL_CASES])
def test_fragmentation_real(label, relpath, tmp_path):
    ctrl, img, preexisting = _open_real(relpath, tmp_path)
    expected = _churn(ctrl)
    assert len(expected) >= 1, f"{label}: churn wrote no files"
    ctrl.flush()
    ctrl.close_disk()
    _verify(img, expected, preexisting, fresh=False)
