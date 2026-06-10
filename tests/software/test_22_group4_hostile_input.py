"""
Bug-proof regression tests for Group 4: hostile-input robustness.

These parsers consume untrusted images downloaded from the internet. Covers:
  * h17.py:1188/1311 - infinite loop on a crafted draft Track sub-block
  * h17.py:1265/1284 - sync-less sectors aliasing onto file offset 0
  * fat12_fs.py:1516 - invalid cluster numbers entering a cluster chain
  * hdos_fs.py:1025  - unclamped last_sector_index reading past a group
  * imd.py:639       - validate_for_opening rejecting valid long-comment files
  * file_manager.py:698 - path traversal when extracting untrusted names
"""

import signal
import struct
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from fatfloppy.core.controller import DiskController  # noqa: E402
from fatfloppy.core.drivers.h17 import (  # noqa: E402
    DRAFT_SUBBLOCK_SECTOR,
    DRAFT_SUBBLOCK_TRACK,
    H17DiskFormat,
    H17ImageDriver,
)
from fatfloppy.core.drivers.imd import IMDImageDriver  # noqa: E402
from fatfloppy.gui.managers.file_manager import (  # noqa: E402
    _safe_local_join,
    _sanitize_local_name,
)


@contextmanager
def time_limit(seconds):
    """Raises TimeoutError if the wrapped block runs longer than `seconds`."""

    def handler(_signum, _frame):
        raise TimeoutError("operation timed out")

    old = signal.signal(signal.SIGALRM, handler)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


# --------------------------------------------------------------------------- #
# H17 draft parser must not hang (h17.py:1188/1311)
# --------------------------------------------------------------------------- #


def test_h17_draft_parser_does_not_hang_on_bad_track_length(tmp_path):
    """A crafted Track sub-block with a tiny length must not spin forever."""
    driver = H17ImageDriver(str(tmp_path / "x.h17"))
    driver.disk_format = H17DiskFormat(sides=1, tracks=40, read_only=False)

    # Track sub-block with track_length=5 (1..9 makes sector_length negative),
    # followed by a sector sub-block header.
    data = (
        bytes([DRAFT_SUBBLOCK_TRACK, 0, 0])
        + struct.pack(">H", 5)
        + bytes([DRAFT_SUBBLOCK_SECTOR, 0, 0, 0, 0, 0])
        + b"\x00" * 20
    )

    with time_limit(5):
        driver._parse_draft_data_block(data, 0)  # must return, not hang


# --------------------------------------------------------------------------- #
# H17 unrecoverable sectors must not alias onto the header (h17.py:1265/1284)
# --------------------------------------------------------------------------- #


def test_h17_unrecoverable_sector_reads_zero_and_rejects_write(tmp_path):
    """A sector with no data location reads as zeros and refuses writes."""
    driver = H17ImageDriver(str(tmp_path / "y.h17"))
    driver.format_h17(sides=1, tracks=40, scheme="hdos")

    meta = driver.sector_metadata[(0, 0, 0)]
    meta.offset_to_data = -1  # simulate a damaged, unlocatable sector

    assert driver.read_sector(0, 0, 0) == bytes(256)
    with pytest.raises(OSError):
        driver.write_sector(0, 0, 0, bytes(256))


# --------------------------------------------------------------------------- #
# Path-traversal sanitisation (file_manager.py:698)
# --------------------------------------------------------------------------- #


def test_sanitize_local_name_strips_paths_and_traversal():
    assert _sanitize_local_name("a/b/c.txt") == "c.txt"
    assert _sanitize_local_name("..\\..\\evil.txt") == "evil.txt"
    assert _sanitize_local_name("..") == "_unnamed_"
    assert _sanitize_local_name("") == "_unnamed_"
    assert _sanitize_local_name("NORMAL.TXT") == "NORMAL.TXT"


def test_safe_local_join_stays_within_base(tmp_path):
    base = str(tmp_path)
    result = Path(_safe_local_join(base, "../../../../etc/passwd"))
    assert result.parent == Path(base)
    assert result.name == "passwd"


# --------------------------------------------------------------------------- #
# IMD validate accepts long comments (imd.py:639)
# --------------------------------------------------------------------------- #


def test_imd_validate_accepts_long_comment(tmp_path):
    """A valid IMD whose comment exceeds 128 bytes must validate as openable."""
    path = tmp_path / "long.imd"
    header = b"IMD 1.18: " + b"X" * 300 + b"\x1a"
    # One track: mode 5, cyl 0, head 0, 1 sector, size code 0 (128), map [1],
    # one compressed sector.
    track = struct.pack("<BBBBB", 5, 0, 0, 1, 0) + bytes([1]) + bytes([2, 0xE5])
    path.write_bytes(header + track)

    driver = IMDImageDriver(str(path))
    ok, err = driver.validate_for_opening(str(path))
    assert ok is True, f"valid long-comment IMD rejected: {err}"


# --------------------------------------------------------------------------- #
# FAT12 cluster chain rejects unaddressable clusters (fat12_fs.py:1516)
# --------------------------------------------------------------------------- #


def test_fat12_cluster_chain_truncates_on_invalid_entry(tmp_path):
    """A FAT entry pointing to a reserved/out-of-range cluster must not crash."""
    controller = DiskController()
    profile_name = "ibm_5.25_160k"
    profile = controller.get_format_by_name(profile_name)
    img = tmp_path / "f.img"
    img.write_bytes(b"\x00" * profile.physical_format.total_bytes)
    assert controller.open_disk(
        str(img), disk_type="IMG", format_info={"format_name": profile_name}
    )
    assert controller.format_disk_media(profile_name)

    fs = controller.filesystem
    fs._load_fat_cache()
    # Point cluster 2's FAT entry at the reserved cluster 1 (unaddressable).
    fs._set_fat_entry_cached(2, 1)

    chain = fs._get_cluster_chain(2)
    assert 1 not in chain  # invalid cluster must never enter the chain
    controller.close_disk()


# --------------------------------------------------------------------------- #
# HDOS clamps a corrupt last_sector_index (hdos_fs.py:1025)
# --------------------------------------------------------------------------- #


def test_hdos_read_clamps_corrupt_last_sector_index(tmp_path):
    """A corrupt last_sector_index must not read past the file's last group."""
    controller = DiskController()
    profile_name = "hdos_5.25_100k"
    profile = controller.get_format_by_name(profile_name)
    img = tmp_path / "h.h8d"
    img.write_bytes(b"\x00" * profile.physical_format.total_bytes)
    assert controller.open_disk(
        str(img), disk_type="IMG", format_info={"format_name": profile_name}
    )
    assert controller.format_disk_media(profile_name)
    controller.write_file("/F.TXT", b"data" * 100)

    fs = controller.filesystem
    entries = fs.list_directory("/")
    assert entries
    # Corrupt the last_sector_index far beyond sectors-per-group and read.
    fs._init_completed = False
    target = fs._read_directory_chain()[0]
    target.last_sector_index = 200  # spg is 2; this is wildly out of range

    # read_file must not raise / read off the end; size stays bounded.
    size = fs._calculate_file_size(target)
    assert size <= (target.last_group) * 256 * 2 + 256 * 2
    controller.close_disk()
