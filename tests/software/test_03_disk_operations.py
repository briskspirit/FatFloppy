"""
Tests for the core Disk class operations using a mock IMGImageDriver.

This test suite focuses on verifying the correctness of sector-level read and
write operations on a Disk object. It uses a pytest fixture to create a
temporary in-memory disk image with a defined geometry and predictable data
pattern.
"""

import sys
from collections.abc import Generator
from pathlib import Path
from unittest.mock import MagicMock, call

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from fatfloppy.core.disk import Disk
from fatfloppy.core.drivers import IMGImageDriver
from fatfloppy.core.physical_format import PhysicalFormat, TrackFormat

CYLINDERS = 2
HEADS = 2
SECTORS_PER_TRACK = 3
BYTES_PER_SECTOR = 128


@pytest.fixture(scope="function")
def disk_setup(tmp_path: Path) -> Generator[tuple[Disk, PhysicalFormat], None, None]:
    """
    Sets up an in-memory disk with a defined geometry and test data.

    Args:
        tmp_path: The pytest temporary path fixture.

    Yields:
        Tuple containing the initialized Disk object and its PhysicalFormat.
    """
    track_fmt = TrackFormat(
        track_start=0,
        track_end=CYLINDERS - 1,
        head_start=0,
        head_end=HEADS - 1,
        sectors_per_track=SECTORS_PER_TRACK,
        encoding="MFM",
        rate=500,
        gap3_bytes=42,
        interleave=1,
    )
    phys_fmt = PhysicalFormat(
        cylinders=CYLINDERS,
        heads=HEADS,
        rpm=300,
        heads_inverted=False,
        bytes_per_sector=BYTES_PER_SECTOR,
        track_formats=[track_fmt],
    )

    total_sectors = phys_fmt.total_sectors
    total_bytes = total_sectors * BYTES_PER_SECTOR
    initial_data = bytearray(total_bytes)

    for c in range(phys_fmt.cylinders):
        for h in range(phys_fmt.heads):
            spt = phys_fmt.get_sectors_per_track(c, h)
            for s in range(1, spt + 1):
                try:
                    lba = phys_fmt.chs_to_lba(c, h, s)
                    offset = lba * BYTES_PER_SECTOR
                    sector_val = c * 100 + h * 10 + s
                    sector_byte = sector_val % 256
                    sector_data = bytes([sector_byte] * BYTES_PER_SECTOR)
                    if offset + BYTES_PER_SECTOR <= len(initial_data):
                        initial_data[offset : offset + BYTES_PER_SECTOR] = sector_data
                except (ValueError, IndexError):
                    pass

    img_path = tmp_path / "disk_ops.img"
    driver = IMGImageDriver(file_path=str(img_path), image_data=bytes(initial_data))
    disk = Disk(driver)
    disk.set_geometry(phys_fmt)

    yield disk, phys_fmt


def test_read_sectors_single_track(disk_setup: tuple[Disk, PhysicalFormat]) -> None:
    """Tests reading multiple sectors that are all on the same track."""
    disk, phys_fmt = disk_setup
    c, h, start_s, num_s = 0, 1, 1, 2
    expected_len = num_s * phys_fmt.bytes_per_sector
    expected_data = bytes([11] * phys_fmt.bytes_per_sector) + bytes(
        [12] * phys_fmt.bytes_per_sector
    )

    read_data = disk.read_sectors(c, h, start_s, num_s)

    assert len(read_data) == expected_len
    assert read_data == expected_data


def test_read_sectors_span_track(disk_setup: tuple[Disk, PhysicalFormat]) -> None:
    """Tests reading multiple sectors that span across a head/track boundary."""
    disk, phys_fmt = disk_setup
    c, h, start_s, num_s = 0, 0, 2, 3
    expected_len = num_s * phys_fmt.bytes_per_sector
    expected_data = (
        bytes([2] * phys_fmt.bytes_per_sector)
        + bytes([3] * phys_fmt.bytes_per_sector)
        + bytes([11] * phys_fmt.bytes_per_sector)
    )

    read_data = disk.read_sectors(c, h, start_s, num_s)

    assert len(read_data) == expected_len
    assert read_data == expected_data


def test_read_sectors_span_cylinder(disk_setup: tuple[Disk, PhysicalFormat]) -> None:
    """Tests reading multiple sectors that span across a cylinder boundary."""
    disk, phys_fmt = disk_setup
    c, h, start_s, num_s = 0, 1, 3, 2
    expected_len = num_s * phys_fmt.bytes_per_sector
    expected_data = bytes([13] * phys_fmt.bytes_per_sector) + bytes(
        [101] * phys_fmt.bytes_per_sector
    )

    read_data = disk.read_sectors(c, h, start_s, num_s)

    assert len(read_data) == expected_len
    assert read_data == expected_data


def test_write_sectors_single_track(disk_setup: tuple[Disk, PhysicalFormat]) -> None:
    """Tests writing multiple sectors that are all on the same track."""
    disk, phys_fmt = disk_setup
    c, h, start_s = 1, 0, 1
    bytes_per_sector = phys_fmt.bytes_per_sector
    write_data = bytes([0xAA] * bytes_per_sector) + bytes([0xBB] * bytes_per_sector)
    disk.write_sector = MagicMock()

    disk.write_sectors(c, h, start_s, write_data)

    expected_calls = [
        call(1, 0, 1, bytes([0xAA] * bytes_per_sector)),
        call(1, 0, 2, bytes([0xBB] * bytes_per_sector)),
    ]
    disk.write_sector.assert_has_calls(expected_calls)
    assert disk.write_sector.call_count == 2


def test_write_sectors_span_track_cylinder(
    disk_setup: tuple[Disk, PhysicalFormat],
) -> None:
    """Tests writing multiple sectors spanning track and cylinder boundaries."""
    disk, phys_fmt = disk_setup
    c, h, start_s = 0, 1, 2
    bytes_per_sector = phys_fmt.bytes_per_sector
    write_data = (
        bytes([0x11] * bytes_per_sector)
        + bytes([0x22] * bytes_per_sector)
        + bytes([0x33] * bytes_per_sector)
    )
    disk.write_sector = MagicMock()

    disk.write_sectors(c, h, start_s, write_data)

    expected_calls = [
        call(0, 1, 2, bytes([0x11] * bytes_per_sector)),
        call(0, 1, 3, bytes([0x22] * bytes_per_sector)),
        call(1, 0, 1, bytes([0x33] * bytes_per_sector)),
    ]
    disk.write_sector.assert_has_calls(expected_calls)
    assert disk.write_sector.call_count == 3


def test_write_sectors_padding(disk_setup: tuple[Disk, PhysicalFormat]) -> None:
    """
    Tests that writing data smaller than a full number of sectors correctly
    pads the final sector with zeros.
    """
    disk, phys_fmt = disk_setup
    c, h, start_s = 1, 1, 1
    bytes_per_sector = phys_fmt.bytes_per_sector
    partial_data_len = bytes_per_sector + 50
    write_data = bytes([0xCC] * partial_data_len)

    expected_s1_data = bytes([0xCC] * bytes_per_sector)
    expected_s2_data = bytes([0xCC] * 50) + bytes([0x00] * (bytes_per_sector - 50))
    disk.write_sector = MagicMock()

    disk.write_sectors(c, h, start_s, write_data)

    expected_calls = [
        call(1, 1, 1, expected_s1_data),
        call(1, 1, 2, expected_s2_data),
    ]
    disk.write_sector.assert_has_calls(expected_calls)
    assert disk.write_sector.call_count == 2


def test_error_no_geometry_read(disk_setup: tuple[Disk, PhysicalFormat]) -> None:
    """
    Tests that read operations fail with a ValueError if disk geometry is not set.
    """
    disk_no_geom = Disk(disk_setup[0].driver)
    assert disk_no_geom.physical_format is None

    with pytest.raises(ValueError, match="Disk geometry not set"):
        disk_no_geom.read_sector(0, 0, 1)

    with pytest.raises(ValueError, match="Disk geometry not set"):
        disk_no_geom.read_sectors(0, 0, 1, 1)


def test_error_no_geometry_write(disk_setup: tuple[Disk, PhysicalFormat]) -> None:
    """
    Tests that write operations fail with a ValueError if disk geometry is not set.
    """
    disk_no_geom = Disk(disk_setup[0].driver)
    assert disk_no_geom.physical_format is None
    bytes_per_sector = disk_setup[1].bytes_per_sector
    data = b"\x00" * bytes_per_sector

    with pytest.raises(ValueError, match="Disk geometry not set"):
        disk_no_geom.write_sector(0, 0, 1, data)

    with pytest.raises(ValueError, match="Disk geometry not set"):
        disk_no_geom.write_sectors(0, 0, 1, data)


def test_error_invalid_address_read(disk_setup: tuple[Disk, PhysicalFormat]) -> None:
    """
    Tests that read operations fail with ValueError for out-of-bounds CHS addresses.
    """
    disk, phys_fmt = disk_setup

    with pytest.raises(ValueError, match=f"Invalid CHS: {phys_fmt.cylinders}, 0, 1"):
        disk.read_sector(phys_fmt.cylinders, 0, 1)

    with pytest.raises(ValueError, match=f"Invalid CHS: 0, {phys_fmt.heads}, 1"):
        disk.read_sector(0, phys_fmt.heads, 1)

    with pytest.raises(ValueError, match="Sector 0 out of range"):
        disk.read_sector(0, 0, 0)

    max_spt = phys_fmt.get_sectors_per_track(0, 0)
    with pytest.raises(ValueError, match=f"Sector {max_spt + 1} out of range"):
        disk.read_sector(0, 0, max_spt + 1)


def test_error_invalid_address_write(disk_setup: tuple[Disk, PhysicalFormat]) -> None:
    """
    Tests that write operations fail with ValueError for out-of-bounds CHS addresses.
    """
    disk, phys_fmt = disk_setup
    data = b"\x00" * phys_fmt.bytes_per_sector

    with pytest.raises(ValueError, match=f"Invalid CHS: {phys_fmt.cylinders}, 0, 1"):
        disk.write_sector(phys_fmt.cylinders, 0, 1, data)

    with pytest.raises(ValueError, match=f"Invalid CHS: 0, {phys_fmt.heads}, 1"):
        disk.write_sector(0, phys_fmt.heads, 1, data)

    with pytest.raises(ValueError, match="Sector 0 out of range"):
        disk.write_sector(0, 0, 0, data)

    max_spt = phys_fmt.get_sectors_per_track(0, 0)
    with pytest.raises(ValueError, match=f"Sector {max_spt + 1} out of range"):
        disk.write_sector(0, 0, max_spt + 1, data)


def test_error_invalid_write_size(disk_setup: tuple[Disk, PhysicalFormat]) -> None:
    """
    Tests that write_sector fails if the data size does not match the sector size.
    """
    disk, phys_fmt = disk_setup
    error_match = "Data size .* != sector size .*"

    with pytest.raises(ValueError, match=error_match):
        disk.write_sector(0, 0, 1, b"\x00" * (phys_fmt.bytes_per_sector - 1))

    with pytest.raises(ValueError, match=error_match):
        disk.write_sector(0, 0, 1, b"\x00" * (phys_fmt.bytes_per_sector + 1))
