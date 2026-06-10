"""
Bug-proof regression tests for Group 3: filesystem write ordering + HDOS RGT.

Covers:
  * hdos_fs.py:439  - format never reserves the RGT sector's group; the first
                      file written after formatting overwrites the RGT (critical)
  * hdos_fs.py:1108 - GRT free-chain walk has no cycle/bounds guard
  * hdos_fs.py:1223 - write_file does no filename validation before deleting
  * fat12_fs.py:1082- write_file deletes the old file before checking it fits
  * cpm_fs.py:1108  - write_file deletes the old file before checking it fits
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from fatfloppy.core.controller import DiskController  # noqa: E402
from fatfloppy.core.filesystems.hdos_fs import HDOS_RGT_SECTOR_LBA  # noqa: E402


def _format_disk(tmp_path, profile_name, name="disk.img"):
    """Creates, opens, and formats a blank image; returns the controller."""
    controller = DiskController()
    profile = controller.get_format_by_name(profile_name)
    assert profile is not None
    img = tmp_path / name
    img.write_bytes(b"\x00" * profile.physical_format.total_bytes)
    assert controller.open_disk(
        str(img), disk_type="IMG", format_info={"format_name": profile_name}
    )
    assert controller.format_disk_media(profile_name)
    return controller


# --------------------------------------------------------------------------- #
# HDOS RGT reservation (critical)
# --------------------------------------------------------------------------- #


def test_hdos_first_write_does_not_clobber_rgt(tmp_path):
    """Writing a file on a freshly formatted HDOS disk must not overwrite the RGT."""
    controller = _format_disk(tmp_path, "hdos_5.25_100k")
    fs = controller.filesystem

    rgt_after_format = bytes(fs._read_lba(HDOS_RGT_SECTOR_LBA))

    controller.write_file("/TEST.TXT", b"hello world, this is a file" * 20)

    rgt_after_write = bytes(controller.filesystem._read_lba(HDOS_RGT_SECTOR_LBA))
    assert rgt_after_write == rgt_after_format, "the RGT sector was overwritten"

    # And the file is still readable (sanity).
    assert controller.read_file("/TEST.TXT") == b"hello world, this is a file" * 20
    controller.close_disk()


def test_hdos_corrupt_free_chain_raises(tmp_path):
    """A cyclic GRT free chain must raise, not loop or write duplicate groups."""
    controller = _format_disk(tmp_path, "hdos_5.25_100k")
    fs = controller.filesystem

    # Corrupt the on-disk GRT into a self-cycle at the free-chain head.
    grt = bytearray(fs._read_lba(fs.label.grt_start_block))
    head = grt[0]
    assert head != 0
    grt[head] = head  # head points to itself -> cycle
    fs._write_lba(fs.label.grt_start_block, bytes(grt))
    controller.disk.flush()
    fs._grt = None
    fs._init_completed = False

    with pytest.raises(OSError, match="Corrupt GRT free chain"):
        controller.write_file("/X.TXT", b"\x00" * 600)  # needs >1 group
    controller.close_disk()


def test_hdos_empty_name_rejected(tmp_path):
    """An empty filename must be rejected, not silently create an invisible entry."""
    controller = _format_disk(tmp_path, "hdos_5.25_100k")

    # Pre-fix this "succeeded" and wrote an entry with an empty name that is
    # invisible in listings and leaks its allocated group.
    with pytest.raises(ValueError):
        controller.write_file("/.TXT", b"data that would be orphaned")

    assert controller.list_directory("/") == []
    controller.close_disk()


def test_hdos_non_ascii_name_rejected(tmp_path):
    """A non-ASCII name must be rejected without destroying an existing file."""
    controller = _format_disk(tmp_path, "hdos_5.25_100k")
    controller.write_file("/KEEP.TXT", b"original contents")

    with pytest.raises(ValueError):
        controller.write_file("/NAMÉ.TXT", b"junk")

    assert controller.read_file("/KEEP.TXT") == b"original contents"
    controller.close_disk()


# --------------------------------------------------------------------------- #
# FAT12 / CP/M validate-before-delete
# --------------------------------------------------------------------------- #


def test_fat12_failed_overwrite_preserves_original(tmp_path):
    """Overwriting a file with data too large to fit must keep the original."""
    controller = _format_disk(tmp_path, "ibm_5.25_160k")
    original = b"KEEP THIS DATA SAFE\n" * 3
    controller.write_file("/KEEP.TXT", original)

    _, total_bytes = controller.get_free_space()
    too_big = bytes(total_bytes * 2)  # cannot fit even after freeing KEEP

    with pytest.raises(OSError):
        controller.write_file("/KEEP.TXT", too_big)

    assert controller.read_file("/KEEP.TXT") == original
    controller.close_disk()


def test_cpm_failed_overwrite_preserves_original(tmp_path):
    """Overwriting a CP/M file with data too large must keep the original."""
    controller = _format_disk(tmp_path, "cpm_8_sssd_250k")
    original = b"IMPORTANT CP/M FILE\n" * 4
    controller.write_file("/KEEP.TXT", original)

    _, total_bytes = controller.get_free_space()
    too_big = bytes(total_bytes * 2)

    with pytest.raises(OSError):
        controller.write_file("/KEEP.TXT", too_big)

    assert controller.read_file("/KEEP.TXT") == original
    controller.close_disk()


def test_fat12_same_size_overwrite_still_works(tmp_path):
    """A normal in-place overwrite must keep working after the fit check."""
    controller = _format_disk(tmp_path, "ibm_5.25_160k")
    controller.write_file("/A.TXT", b"first version")
    controller.write_file("/A.TXT", b"second version, replaces the first")
    assert controller.read_file("/A.TXT") == b"second version, replaces the first"
    controller.close_disk()
