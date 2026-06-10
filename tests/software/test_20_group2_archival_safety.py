"""
Bug-proof regression tests for Group 2: archival-image safety.

Covers the audit's critical/high findings for saving disk images:
  * imd.py:347/350 - flush drops cyl/head/size maps it declares in the header
  * imd.py:371     - flush rewrites every sector type byte (loses deleted/error/
                     unavailable marks)
  * imd.py:291     - flush rebuilds from physical_format, dropping tracks on a
                     geometry override
  * imd.py:424     - _sector_size_cache not invalidated on reformat
  * h17.py:798/937 - new-image header is unreadable by the driver's own parser
  * h17.py:829     - off-by-8 sector-data offsets in new images
  * img.py:175 etc - in-place flush has no atomic write / crash safety
"""

import os
import struct
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from fatfloppy.core.drivers.h17 import H17ImageDriver  # noqa: E402
from fatfloppy.core.drivers.imd import IMDImageDriver  # noqa: E402
from fatfloppy.core.drivers.img import IMGImageDriver  # noqa: E402
from fatfloppy.core.filesystem_registry import FilesystemRegistry  # noqa: E402
from fatfloppy.core.physical_format import PhysicalFormat, TrackFormat  # noqa: E402
from fatfloppy.core.utils.atomic_io import atomic_write  # noqa: E402

_ALL_FORMATS = FilesystemRegistry.get_all_formats()
FMT_720 = _ALL_FORMATS["ibm_3.5_720k"]  # 512 bps

# IMD sector data types
IMD_NORMAL = 1
IMD_COMPRESSED = 2
IMD_NORMAL_DEL = 3
IMD_UNAVAILABLE = 0
IMD_DELETED_TYPES = {3, 4, 7, 8}

MODE_MFM_250 = 5  # IMD mode for 250kbps MFM


def _make_imd(tracks: list[dict]) -> bytes:
    """Builds an IMD byte image from explicit per-track descriptors.

    Each track dict: cyl, head, size_code, sector_map, optional cyl_map/head_map/
    size_map lists, and sectors = list of (type_byte, content_bytes).
    """
    data = bytearray(b"IMD 1.18: test\x1a")
    for t in tracks:
        head_flags = t["head"] & 1
        if t.get("cyl_map"):
            head_flags |= 0x80
        if t.get("head_map"):
            head_flags |= 0x40
        spt = len(t["sector_map"])
        data += struct.pack(
            "<BBBBB", MODE_MFM_250, t["cyl"], head_flags, spt, t["size_code"]
        )
        data += struct.pack(f"<{spt}B", *t["sector_map"])
        if t.get("cyl_map"):
            data += struct.pack(f"<{spt}B", *t["cyl_map"])
        if t.get("head_map"):
            data += struct.pack(f"<{spt}B", *t["head_map"])
        if t.get("size_map"):
            data += struct.pack(f"<{spt}H", *t["size_map"])
        for type_byte, content in t["sectors"]:
            data.append(type_byte)
            data += content
    return bytes(data)


# --------------------------------------------------------------------------- #
# IMD flush: optional maps must survive a save (imd.py:347/350)
# --------------------------------------------------------------------------- #


def test_imd_flush_preserves_cylinder_and_head_maps(tmp_path):
    """Saving an IMD with cylinder/head maps must keep them, not corrupt the file."""
    path = tmp_path / "maps.imd"
    sector_map = [1, 2, 3]
    track = {
        "cyl": 0,
        "head": 0,
        "size_code": 0,  # 128
        "sector_map": sector_map,
        "cyl_map": [7, 7, 7],  # nonstandard physical cylinder ids
        "head_map": [1, 1, 1],
        "sectors": [(IMD_NORMAL, bytes([s]) * 128) for s in sector_map],
    }
    path.write_bytes(_make_imd([track]))

    driver = IMDImageDriver(str(path))
    driver.write_sector(0, 0, 0, bytes([0xAA]) * 128)  # touch logical sector 0
    driver.flush()

    # Re-open: must parse cleanly and still carry the maps.
    reopened = IMDImageDriver(str(path))
    ti = reopened.tracks[(0, 0)]
    assert ti.has_cyl_map is True
    assert ti.has_head_map is True
    assert ti.sector_cyl_map == {1: 7, 2: 7, 3: 7}
    assert ti.sector_head_map == {1: 1, 2: 1, 3: 1}


def test_imd_flush_preserves_variable_size_map(tmp_path):
    """Saving a variable-sector-size IMD must keep its 16-bit size map."""
    path = tmp_path / "sizes.imd"
    sector_map = [1, 2, 3]
    size_map = [128, 256, 512]
    track = {
        "cyl": 0,
        "head": 0,
        "size_code": 0xFF,  # variable
        "sector_map": sector_map,
        "size_map": size_map,
        "sectors": [
            (IMD_NORMAL, bytes([1]) * 128),
            (IMD_NORMAL, bytes([2]) * 256),
            (IMD_NORMAL, bytes([3]) * 512),
        ],
    }
    path.write_bytes(_make_imd([track]))

    driver = IMDImageDriver(str(path))
    driver.write_sector(0, 0, 0, bytes([0xAA]) * 128)  # logical 0 -> sector id 1 (128B)
    driver.flush()

    reopened = IMDImageDriver(str(path))
    ti = reopened.tracks[(0, 0)]
    assert ti.sector_size_code == 0xFF
    assert ti.sector_size_map == {1: 128, 2: 256, 3: 512}


# --------------------------------------------------------------------------- #
# IMD flush: original sector type bytes must survive (imd.py:371)
# --------------------------------------------------------------------------- #


def test_imd_flush_preserves_unmodified_sector_types(tmp_path):
    """Untouched deleted-DAM and unavailable sectors keep their type on save."""
    path = tmp_path / "types.imd"
    sector_map = [1, 2, 3]
    track = {
        "cyl": 0,
        "head": 0,
        "size_code": 0,  # 128
        "sector_map": sector_map,
        "sectors": [
            (IMD_NORMAL, bytes([0x11]) * 128),
            (IMD_NORMAL_DEL, bytes([0x22]) * 128),  # deleted data address mark
            (IMD_UNAVAILABLE, b""),  # sector physically not present
        ],
    }
    path.write_bytes(_make_imd([track]))

    driver = IMDImageDriver(str(path))
    driver.write_sector(0, 0, 0, bytes([0xAB]) * 128)  # modify only sector id 1
    driver.flush()

    reopened = IMDImageDriver(str(path))
    info = reopened.tracks[(0, 0)].sector_data_info
    assert info[2][1] in IMD_DELETED_TYPES, "deleted-DAM mark must be preserved"
    assert info[3][1] == IMD_UNAVAILABLE, "unavailable sector must stay unavailable"


# --------------------------------------------------------------------------- #
# IMD flush: geometry override must not drop tracks (imd.py:291)
# --------------------------------------------------------------------------- #


def test_imd_flush_keeps_tracks_outside_overridden_geometry(tmp_path):
    """A geometry override with fewer cylinders must not delete tracks on save."""
    path = tmp_path / "twocyl.imd"
    tracks = [
        {
            "cyl": c,
            "head": 0,
            "size_code": 0,
            "sector_map": [1, 2],
            "sectors": [(IMD_NORMAL, bytes([c]) * 128) for _ in range(2)],
        }
        for c in range(2)
    ]
    path.write_bytes(_make_imd(tracks))

    driver = IMDImageDriver(str(path))
    assert driver.physical_format.cylinders == 2

    override = PhysicalFormat(
        cylinders=1,
        heads=1,
        rpm=300,
        heads_inverted=False,
        bytes_per_sector=128,
        track_formats=[TrackFormat(0, 0, 0, 0, 2, "MFM", 250, bytes_per_sector=128)],
    )
    driver.set_physical_format(override)
    driver.write_sector(0, 0, 0, bytes([0xAB]) * 128)
    driver.flush()

    reopened = IMDImageDriver(str(path))
    assert reopened.physical_format.cylinders == 2, "cylinder 1 must not be dropped"


# --------------------------------------------------------------------------- #
# IMD: sector size cache must be invalidated on reformat (imd.py:424)
# --------------------------------------------------------------------------- #


def test_imd_reformat_to_new_sector_size_accepts_correct_writes(tmp_path):
    """Reformatting to a new sector size must validate writes against the new size."""
    path = tmp_path / "resize.imd"
    track = {
        "cyl": 0,
        "head": 0,
        "size_code": 0,  # 128 bps originally
        "sector_map": [1, 2],
        "sectors": [(IMD_NORMAL, bytes([s]) * 128) for s in (1, 2)],
    }
    path.write_bytes(_make_imd([track]))

    driver = IMDImageDriver(str(path))
    driver.format_imd(FMT_720)  # 512 bps

    # A correctly sized 512-byte write must be accepted, not rejected against 128.
    driver.write_sector(0, 0, 0, bytes(512))


# --------------------------------------------------------------------------- #
# H17 new-image round-trip (h17.py:798/937/829)
# --------------------------------------------------------------------------- #


def test_h17_new_image_roundtrip(tmp_path):
    """A freshly created H17 image must be reopenable and preserve written data."""
    path = tmp_path / "new.h17"
    driver = H17ImageDriver(str(path))
    driver.format_h17(sides=1, tracks=40, scheme="hdos")

    payload = bytes([0x5A]) * 256
    driver.write_sector(0, 0, 0, payload)
    driver.flush()

    reopened = H17ImageDriver(str(path))
    assert reopened.physical_format is not None
    assert reopened.read_sector(0, 0, 0) == payload


# --------------------------------------------------------------------------- #
# Atomic/crash-safe flush (img.py:175 and the shared helper)
# --------------------------------------------------------------------------- #


def _small_pf() -> PhysicalFormat:
    return PhysicalFormat(
        cylinders=1,
        heads=1,
        rpm=300,
        heads_inverted=False,
        bytes_per_sector=128,
        track_formats=[TrackFormat(0, 0, 0, 0, 8, "MFM", 250, bytes_per_sector=128)],
    )


def test_atomic_write_preserves_original_on_failure(tmp_path):
    """A failed atomic_write must leave the original file intact and no temp behind."""
    path = tmp_path / "data.bin"
    path.write_bytes(b"ORIGINAL")

    with (
        patch("pathlib.Path.replace", side_effect=OSError("boom")),
        pytest.raises(OSError),
    ):
        atomic_write(str(path), b"NEWDATA")

    assert path.read_bytes() == b"ORIGINAL"
    leftovers = [p for p in tmp_path.iterdir() if p.name != "data.bin"]
    assert leftovers == [], f"temp files left behind: {leftovers}"

    atomic_write(str(path), b"NEWDATA")
    assert path.read_bytes() == b"NEWDATA"


def test_img_flush_does_not_destroy_file_on_write_failure(tmp_path):
    """A crash during IMG flush must not corrupt the existing image on disk."""
    path = tmp_path / "disk.img"
    driver = IMGImageDriver(str(path))
    driver.initialize_new_image(_small_pf())
    driver.flush()
    original = path.read_bytes()

    driver.write_sector(0, 0, 0, bytes([0xAB]) * 128)
    with (
        patch("pathlib.Path.replace", side_effect=OSError("boom")),
        pytest.raises(OSError),
    ):
        driver.flush()

    assert path.read_bytes() == original, "original image must survive a failed flush"
    assert not any(p.suffix == ".tmp" for p in tmp_path.iterdir()), (
        "no temp file should be left behind"
    )


def test_os_fsync_is_called_on_atomic_write(tmp_path):
    """atomic_write must fsync before replacing so 'flushed' means durable."""
    path = tmp_path / "f.bin"
    real_fsync = os.fsync
    called = []

    def tracking_fsync(fd):
        called.append(fd)
        return real_fsync(fd)

    with patch("fatfloppy.core.utils.atomic_io.os.fsync", side_effect=tracking_fsync):
        atomic_write(str(path), b"durable")

    assert called, "os.fsync must be called before the atomic replace"
    assert path.read_bytes() == b"durable"
