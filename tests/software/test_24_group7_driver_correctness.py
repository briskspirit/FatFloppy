"""
Regression tests for Group 7: driver correctness fixes.

Covers:
  * mits_dsk.py:538/541 - data-track checksum byte [4] now computed
  * mits_dsk.py:488     - write_sector validates the cylinder
  * imd.py:587/822      - logical->ID mapping uses sorted IDs (interleave safe)
  * imd.py:554          - read_boot_sector_data reads the first logical sector
"""

import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from fatfloppy.core.drivers.imd import IMDImageDriver  # noqa: E402
from fatfloppy.core.drivers.mits_dsk import (  # noqa: E402
    MITS_PHYSICAL_SECTOR_SIZE,
    MITS_SECTORS_PER_TRACK,
    MITS_SYSTEM_TRACK_COUNT,
    MITS_TRACKS,
    MITSDSKDriver,
)


def _new_mits(tmp_path):
    driver = MITSDSKDriver(str(tmp_path / "disk.dsk"))
    driver.initialize_new_image(None)
    return driver


def test_mits_new_image_data_track_sectors_pass_checksum(tmp_path):
    """Freshly created data-track sectors must pass the driver's own checksum."""
    driver = _new_mits(tmp_path)
    track = MITS_SYSTEM_TRACK_COUNT + 2  # a data track
    for sector in range(MITS_SECTORS_PER_TRACK):
        offset = (track * MITS_SECTORS_PER_TRACK + sector) * MITS_PHYSICAL_SECTOR_SIZE
        phys = bytes(driver.image_data[offset : offset + MITS_PHYSICAL_SECTOR_SIZE])
        assert driver._validate_sector_checksum(phys, track), (
            f"data-track sector C:{track} S:{sector} failed checksum"
        )


def test_mits_written_data_sector_passes_checksum(tmp_path):
    """A written data-track sector must pass the checksum after flush."""
    driver = _new_mits(tmp_path)
    track = MITS_SYSTEM_TRACK_COUNT + 1
    payload = bytes((i % 256) for i in range(128))
    driver.write_sector(track, 0, 5, payload)
    driver.flush()

    reopened = MITSDSKDriver(str(tmp_path / "disk.dsk"))
    reopened._create_physical_format()
    offset = (track * MITS_SECTORS_PER_TRACK + 5) * MITS_PHYSICAL_SECTOR_SIZE
    phys = bytes(reopened.image_data[offset : offset + MITS_PHYSICAL_SECTOR_SIZE])
    assert reopened._validate_sector_checksum(phys, track)
    assert reopened.read_sector(track, 0, 5) == payload


def test_mits_write_sector_rejects_bad_cylinder(tmp_path):
    """write_sector must reject an out-of-range cylinder, not append a blob."""
    driver = _new_mits(tmp_path)
    with pytest.raises(ValueError):
        driver.write_sector(MITS_TRACKS, 0, 0, bytes(128))


def test_imd_read_boot_sector_returns_first_logical_sector(tmp_path):
    """read_boot_sector_data returns the first logical sector (sector id 1)."""
    path = tmp_path / "b.imd"
    header = b"IMD 1.18: test\x1a"
    # One track, 3 sectors (ids 1,2,3), each NORMAL with id-stamped content.
    track = struct.pack("<BBBBB", 5, 0, 0, 3, 0) + bytes([1, 2, 3])
    for sid in (1, 2, 3):
        track += bytes([1]) + bytes([sid] * 128)
    path.write_bytes(header + track)

    driver = IMDImageDriver(str(path))
    boot = driver.read_boot_sector_data()
    assert boot == driver.read_sector(0, 0, 0)
    assert boot == bytes([1] * 128)  # first logical sector = id 1


def test_imd_interleaved_map_reads_by_sorted_id(tmp_path):
    """A skewed numbering map must map logical index by ascending sector ID."""
    path = tmp_path / "skew.imd"
    header = b"IMD 1.18: test\x1a"
    # Numbering map in interleaved (rotational) order [1,3,2]; data stored in
    # that physical order but each record carries its own id-stamped content.
    sector_map = [1, 3, 2]
    track = struct.pack("<BBBBB", 5, 0, 0, 3, 0) + bytes(sector_map)
    for sid in sector_map:
        track += bytes([1]) + bytes([sid] * 128)
    path.write_bytes(header + track)

    driver = IMDImageDriver(str(path))
    # Logical 0 -> id 1, logical 1 -> id 2, logical 2 -> id 3 (ascending id).
    assert driver.read_sector(0, 0, 0) == bytes([1] * 128)
    assert driver.read_sector(0, 0, 1) == bytes([2] * 128)
    assert driver.read_sector(0, 0, 2) == bytes([3] * 128)
