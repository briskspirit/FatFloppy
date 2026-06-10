"""
Regression tests for Group 6: filesystem correctness fixes.

Covers:
  * cpm_fs.py:1289     - non-ASCII filename crashes write
  * cpm_fs.py:1120     - empty (zero-byte) file support
  * cpm_formats.py:55  - DPB_525_SSSD dsm off-by-one (block past end of disk)
  * cpm_formats.py:302 - cpm_5.25_100k interleave=4 ignored (stale xlat table)
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from fatfloppy.core.controller import DiskController  # noqa: E402
from fatfloppy.core.filesystem_registry import FilesystemRegistry  # noqa: E402


def _format(tmp_path, profile_name, name="d.img"):
    ctrl = DiskController()
    profile = ctrl.get_format_by_name(profile_name)
    img = tmp_path / name
    img.write_bytes(b"\x00" * profile.physical_format.total_bytes)
    assert ctrl.open_disk(
        str(img), disk_type="IMG", format_info={"format_name": profile_name}
    )
    assert ctrl.format_disk_media(profile_name)
    return ctrl


def test_cpm_non_ascii_filename_rejected(tmp_path):
    ctrl = _format(tmp_path, "cpm_8_sssd_250k")
    ctrl.write_file("/KEEP.TXT", b"keep me")
    with pytest.raises((ValueError, OSError)):
        ctrl.write_file("/NAMÉ.TXT", b"junk")
    assert ctrl.read_file("/KEEP.TXT") == b"keep me"
    ctrl.close_disk()


def test_cpm_empty_file_roundtrips(tmp_path):
    ctrl = _format(tmp_path, "cpm_8_sssd_250k")
    ctrl.write_file("/EMPTY.TXT", b"")
    listing = [item["name"] for item in ctrl.list_directory("/")]
    assert "EMPTY.TXT" in listing
    assert ctrl.read_file("/EMPTY.TXT") == b""
    ctrl.close_disk()


def test_cpm_525_interleave_translation_table_is_not_sequential():
    fmt = FilesystemRegistry.get_all_formats()["cpm_5.25_100k"]
    tf = fmt.physical_format.track_formats[0]
    assert tf.interleave == 4
    sequential = list(range(tf.id_start, tf.id_start + tf.sectors_per_track))
    assert tf.sector_translation_table != sequential, (
        "interleave=4 must rebuild the translation table, not keep the sequential one"
    )


def test_cpm_525_can_fill_disk_without_crashing(tmp_path):
    """Filling the 100K CP/M disk must not allocate a block past the disk end."""
    ctrl = _format(tmp_path, "cpm_5.25_100k")
    free_bytes, _ = ctrl.get_free_space()
    assert free_bytes > 0
    payload = bytes((i % 251) for i in range(free_bytes))
    ctrl.write_file("/FULL.BIN", payload)  # must not raise IndexError/crash
    assert ctrl.read_file("/FULL.BIN") == payload
    ctrl.close_disk()
