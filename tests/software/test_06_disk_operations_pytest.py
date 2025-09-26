# tests/software/test_06_disk_operations_pytest.py
"""
Tests for the core Disk class operations using a mock IMGImageDriver.

This test suite focuses on verifying the correctness of sector-level read and
write operations on a Disk object. It uses a pytest fixture to create a
temporary in-memory disk image with a defined geometry and predictable data
pattern.

The tests cover:
- Reading single and multiple sectors within a track.
- Reading sectors that span across track and cylinder boundaries.
- Writing single and multiple sectors.
- Verifying correct data padding when writing partial sector data.
- Handling of error conditions, such as operations without a defined
  disk geometry and attempts to access invalid CHS addresses.
"""

import sys
from pathlib import Path
from typing import Generator, Tuple
from unittest.mock import MagicMock, call

import pytest

# Add the source directory to the Python path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from fatfloppy.core.disk import Disk
from fatfloppy.core.drivers import IMGImageDriver
from fatfloppy.core.physical_format import PhysicalFormat, TrackFormat


@pytest.fixture(scope="function")
def disk_setup(tmp_path: Path) -> Generator[Tuple[Disk, PhysicalFormat], None, None]:
    """
    Sets up an in-memory disk with a defined geometry and test data.

    This fixture creates a PhysicalFormat object, calculates the total disk size,
    and populates a bytearray with a predictable pattern based on the CHS
    address of each sector. It then initializes a Disk object with an
    IMGImageDriver using this data.

    Args:
        tmp_path: The pytest temporary path fixture.

    Yields:
        A tuple containing the initialized Disk object and its PhysicalFormat.
    """
    # Define geometry parameters clearly
    cylinders: int = 2
    heads: int = 2
    sectors_per_track: int = 3
    bytes_per_sector: int = 128

    # Create TrackFormat and PhysicalFormat
    track_fmt = TrackFormat(
        track_start=0,
        track_end=cylinders - 1,
        head_start=0,
        head_end=heads - 1,
        sectors_per_track=sectors_per_track,
        encoding="MFM",
        rate=500,
        gap3_bytes=42,
        interleave=1,
    )
    phys_fmt = PhysicalFormat(
        cylinders=cylinders,
        heads=heads,
        rpm=300,
        heads_inverted=False,
        bytes_per_sector=bytes_per_sector,
        track_formats=[track_fmt],
    )

    # Calculate total size and create initial data
    total_sectors: int = phys_fmt.total_sectors
    total_bytes: int = total_sectors * bytes_per_sector
    initial_data = bytearray(total_bytes)

    # Populate initial data with a pattern based on CHS address
    for c in range(phys_fmt.cylinders):
        for h in range(phys_fmt.heads):
            spt = phys_fmt.get_sectors_per_track(c, h)
            for s in range(1, spt + 1):
                try:
                    lba = phys_fmt.chs_to_lba(c, h, s)
                    offset = lba * bytes_per_sector
                    sector_val = c * 100 + h * 10 + s
                    sector_byte = sector_val % 256
                    sector_data = bytes([sector_byte] * bytes_per_sector)
                    if offset + bytes_per_sector <= len(initial_data):
                        initial_data[offset : offset + bytes_per_sector] = sector_data
                    else:
                        print(
                            f"Warning: Calculated offset {offset} out of bounds for "
                            f"initial data (size {len(initial_data)}) for "
                            f"CHS={c},{h},{s} LBA={lba}"
                        )
                except (ValueError, IndexError) as e:
                    print(f"Warning: Error preparing initial data for CHS={c},{h},{s}: {e}")

    img_path: Path = tmp_path / "disk_ops.img"
    driver = IMGImageDriver(file_path=str(img_path), image_data=bytes(initial_data))
    disk = Disk(driver)
    disk.set_geometry(phys_fmt)

    yield disk, phys_fmt


def test_01_read_sectors_single_track(disk_setup: Tuple[Disk, PhysicalFormat]) -> None:
    """
    Tests reading multiple sectors that are all on the same track.
    """
    # Arrange
    disk, phys_fmt = disk_setup
    c, h, start_s, num_s = 0, 1, 1, 2
    expected_len = num_s * phys_fmt.bytes_per_sector
    # Sector values: C=0, H=1, S=1 -> 11; C=0, H=1, S=2 -> 12
    expected_data = bytes([11] * phys_fmt.bytes_per_sector) + bytes([12] * phys_fmt.bytes_per_sector)

    # Act
    read_data = disk.read_sectors(c, h, start_s, num_s)

    # Assert
    assert len(read_data) == expected_len
    assert read_data == expected_data


def test_02_read_sectors_span_track(disk_setup: Tuple[Disk, PhysicalFormat]) -> None:
    """
    Tests reading multiple sectors that span across a head/track boundary.
    """
    # Arrange
    disk, phys_fmt = disk_setup
    c, h, start_s, num_s = 0, 0, 2, 3  # Reads (0,0,2), (0,0,3), then (0,1,1)
    expected_len = num_s * phys_fmt.bytes_per_sector
    # Sector values: C=0, H=0, S=2 -> 2; C=0, H=0, S=3 -> 3; C=0, H=1, S=1 -> 11
    expected_data = (
        bytes([2] * phys_fmt.bytes_per_sector)
        + bytes([3] * phys_fmt.bytes_per_sector)
        + bytes([11] * phys_fmt.bytes_per_sector)
    )

    # Act
    read_data = disk.read_sectors(c, h, start_s, num_s)

    # Assert
    assert len(read_data) == expected_len
    assert read_data == expected_data


def test_03_read_sectors_span_cylinder(disk_setup: Tuple[Disk, PhysicalFormat]) -> None:
    """
    Tests reading multiple sectors that span across a cylinder boundary.
    """
    # Arrange
    disk, phys_fmt = disk_setup
    c, h, start_s, num_s = 0, 1, 3, 2  # Reads (0,1,3), then (1,0,1)
    expected_len = num_s * phys_fmt.bytes_per_sector
    # Sector values: C=0, H=1, S=3 -> 13; C=1, H=0, S=1 -> 101
    expected_data = bytes([13] * phys_fmt.bytes_per_sector) + bytes([101] * phys_fmt.bytes_per_sector)

    # Act
    read_data = disk.read_sectors(c, h, start_s, num_s)

    # Assert
    assert len(read_data) == expected_len
    assert read_data == expected_data


def test_04_write_sectors_single_track(disk_setup: Tuple[Disk, PhysicalFormat]) -> None:
    """
    Tests writing multiple sectors that are all on the same track.
    """
    # Arrange
    disk, phys_fmt = disk_setup
    c, h, start_s = 1, 0, 1  # Writes to (1,0,1) and (1,0,2)
    bytes_per_sector = phys_fmt.bytes_per_sector
    write_data = bytes([0xAA] * bytes_per_sector) + bytes([0xBB] * bytes_per_sector)
    disk.write_sector = MagicMock()  # Mock the underlying single write method

    # Act
    disk.write_sectors(c, h, start_s, write_data)

    # Assert
    expected_calls = [
        call(1, 0, 1, bytes([0xAA] * bytes_per_sector)),
        call(1, 0, 2, bytes([0xBB] * bytes_per_sector)),
    ]
    disk.write_sector.assert_has_calls(expected_calls)
    assert disk.write_sector.call_count == 2


def test_05_write_sectors_span_track_cylinder(disk_setup: Tuple[Disk, PhysicalFormat]) -> None:
    """
    Tests writing multiple sectors that span across track and cylinder boundaries.
    """
    # Arrange
    disk, phys_fmt = disk_setup
    c, h, start_s = 0, 1, 2  # Writes to (0,1,2), (0,1,3), and (1,0,1)
    bytes_per_sector = phys_fmt.bytes_per_sector
    write_data = (
        bytes([0x11] * bytes_per_sector)
        + bytes([0x22] * bytes_per_sector)
        + bytes([0x33] * bytes_per_sector)
    )
    disk.write_sector = MagicMock()  # Mock the underlying single write method

    # Act
    disk.write_sectors(c, h, start_s, write_data)

    # Assert
    expected_calls = [
        call(0, 1, 2, bytes([0x11] * bytes_per_sector)),
        call(0, 1, 3, bytes([0x22] * bytes_per_sector)),
        call(1, 0, 1, bytes([0x33] * bytes_per_sector)),
    ]
    disk.write_sector.assert_has_calls(expected_calls)
    assert disk.write_sector.call_count == 3


def test_06_write_sectors_padding(disk_setup: Tuple[Disk, PhysicalFormat]) -> None:
    """
    Tests that writing data smaller than a full number of sectors correctly
    pads the final sector with zeros.
    """
    # Arrange
    disk, phys_fmt = disk_setup
    c, h, start_s = 1, 1, 1  # Writes to (1,1,1), (1,1,2)
    bytes_per_sector = phys_fmt.bytes_per_sector
    partial_data_len = bytes_per_sector + 50  # Data spans into the second sector
    write_data = bytes([0xCC] * partial_data_len)

    expected_s1_data = bytes([0xCC] * bytes_per_sector)
    expected_s2_data = bytes([0xCC] * 50) + bytes([0x00] * (bytes_per_sector - 50))
    disk.write_sector = MagicMock()  # Mock the underlying single write method

    # Act
    disk.write_sectors(c, h, start_s, write_data)

    # Assert
    expected_calls = [
        call(1, 1, 1, expected_s1_data),
        call(1, 1, 2, expected_s2_data),
    ]
    disk.write_sector.assert_has_calls(expected_calls)
    assert disk.write_sector.call_count == 2


def test_07_disk_error_no_geometry_read(disk_setup: Tuple[Disk, PhysicalFormat]) -> None:
    """
    Tests that read operations fail with a ValueError if disk geometry is not set.
    """
    # Arrange
    disk_no_geom = Disk(disk_setup[0].driver)
    assert disk_no_geom.physical_format is None

    # Act & Assert
    with pytest.raises(ValueError, match="Disk geometry not set"):
        disk_no_geom.read_sector(0, 0, 1)

    with pytest.raises(ValueError, match="Disk geometry not set"):
        disk_no_geom.read_sectors(0, 0, 1, 1)


def test_08_disk_error_no_geometry_write(disk_setup: Tuple[Disk, PhysicalFormat]) -> None:
    """
    Tests that write operations fail with a ValueError if disk geometry is not set.
    """
    # Arrange
    disk_no_geom = Disk(disk_setup[0].driver)
    assert disk_no_geom.physical_format is None
    bytes_per_sector = disk_setup[1].bytes_per_sector
    data = b"\x00" * bytes_per_sector

    # Act & Assert
    with pytest.raises(ValueError, match="Disk geometry not set"):
        disk_no_geom.write_sector(0, 0, 1, data)

    with pytest.raises(ValueError, match="Disk geometry not set"):
        disk_no_geom.write_sectors(0, 0, 1, data)


def test_09_disk_error_invalid_address_read(disk_setup: Tuple[Disk, PhysicalFormat]) -> None:
    """
    Tests that read operations fail with ValueError for out-of-bounds CHS addresses.
    """
    # Arrange
    disk, phys_fmt = disk_setup

    # Act & Assert for invalid cylinder
    with pytest.raises(ValueError, match=f"Invalid CHS: {phys_fmt.cylinders}, 0, 1"):
        disk.read_sector(phys_fmt.cylinders, 0, 1)

    # Act & Assert for invalid head
    with pytest.raises(ValueError, match=f"Invalid CHS: 0, {phys_fmt.heads}, 1"):
        disk.read_sector(0, phys_fmt.heads, 1)

    # Act & Assert for invalid sector (zero)
    with pytest.raises(ValueError, match="Sector 0 out of range"):
        disk.read_sector(0, 0, 0)

    # Act & Assert for invalid sector (too high)
    max_spt = phys_fmt.get_sectors_per_track(0, 0)
    with pytest.raises(ValueError, match=f"Sector {max_spt + 1} out of range"):
        disk.read_sector(0, 0, max_spt + 1)


def test_10_disk_error_invalid_address_write(disk_setup: Tuple[Disk, PhysicalFormat]) -> None:
    """
    Tests that write operations fail with ValueError for out-of-bounds CHS addresses.
    """
    # Arrange
    disk, phys_fmt = disk_setup
    data = b"\x00" * phys_fmt.bytes_per_sector

    # Act & Assert for invalid cylinder
    with pytest.raises(ValueError, match=f"Invalid CHS: {phys_fmt.cylinders}, 0, 1"):
        disk.write_sector(phys_fmt.cylinders, 0, 1, data)

    # Act & Assert for invalid head
    with pytest.raises(ValueError, match=f"Invalid CHS: 0, {phys_fmt.heads}, 1"):
        disk.write_sector(0, phys_fmt.heads, 1, data)

    # Act & Assert for invalid sector (zero)
    with pytest.raises(ValueError, match="Sector 0 out of range"):
        disk.write_sector(0, 0, 0, data)

    # Act & Assert for invalid sector (too high)
    max_spt = phys_fmt.get_sectors_per_track(0, 0)
    with pytest.raises(ValueError, match=f"Sector {max_spt + 1} out of range"):
        disk.write_sector(0, 0, max_spt + 1, data)


def test_11_disk_error_invalid_write_size(disk_setup: Tuple[Disk, PhysicalFormat]) -> None:
    """
    Tests that write_sector fails if the data size does not match the sector size.
    """
    # Arrange
    disk, phys_fmt = disk_setup
    error_match = "Data size .* != sector size .*"

    # Act & Assert for data smaller than sector size
    with pytest.raises(ValueError, match=error_match):
        disk.write_sector(0, 0, 1, b"\x00" * (phys_fmt.bytes_per_sector - 1))

    # Act & Assert for data larger than sector size
    with pytest.raises(ValueError, match=error_match):
        disk.write_sector(0, 0, 1, b"\x00" * (phys_fmt.bytes_per_sector + 1))
