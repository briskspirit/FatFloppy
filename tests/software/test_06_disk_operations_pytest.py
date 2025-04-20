# tests/software/test_06_disk_operations_pytest.py
import pytest
import sys
from unittest.mock import MagicMock, call
from pathlib import Path

# Ensure src is in path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / 'src'))

from fatfloppy.core.disk import Disk, DiskGeometry
from fatfloppy.core.drivers import RawImageDriver, PhysicalFormat

# --- Fixture ---
@pytest.fixture(scope="function")
def disk_setup(tmp_path):
    """Sets up Disk with RawImageDriver and patterned data."""
    geom = DiskGeometry(cylinders=2, heads=2, sectors_per_track=3, sector_size=128)
    total_bytes = geom.total_bytes
    initial_data = bytearray(total_bytes)
    offset = 0
    for c in range(geom.cylinders):
        for h in range(geom.heads):
            for s in range(1, geom.sectors_per_track + 1):
                sector_val = c * 100 + h * 10 + s
                sector_byte = sector_val % 256
                sector_data = bytes([sector_byte] * geom.sector_size)
                if offset + geom.sector_size <= len(initial_data):
                    initial_data[offset:offset+geom.sector_size] = sector_data
                offset += geom.sector_size
    initial_data = initial_data[:total_bytes]

    img_path = tmp_path / "disk_ops.img"

    driver = RawImageDriver(file_path=str(img_path), image_data=bytes(initial_data))
    disk = Disk(driver)
    disk.set_geometry(geom)
    pf = PhysicalFormat(
        encoding="MFM", rate=500, rpm=300,
        sectors_per_track=geom.sectors_per_track,
        heads=geom.heads,
        sector_size=geom.sector_size
    )
    driver.set_physical_format(pf)

    yield disk, geom # Yield disk and geometry

    # Cleanup handled by tmp_path

# --- Tests ---

def test_01_read_sectors_single_track(disk_setup):
    disk, geom = disk_setup
    c, h, start_s = 0, 1, 1; num_s = 2
    expected_len = num_s * geom.sector_size
    expected_data = bytes([11] * geom.sector_size) + bytes([12] * geom.sector_size)
    read_data = disk.read_sectors(c, h, start_s, num_s)
    assert len(read_data) == expected_len
    assert read_data == bytes(expected_data)

def test_02_read_sectors_span_track(disk_setup):
    disk, geom = disk_setup
    c, h, start_s = 0, 0, 2; num_s = 3
    expected_len = num_s * geom.sector_size
    expected_data = bytes([2] * geom.sector_size) + \
                    bytes([3] * geom.sector_size) + \
                    bytes([11] * geom.sector_size)
    read_data = disk.read_sectors(c, h, start_s, num_s)
    assert len(read_data) == expected_len
    assert read_data == bytes(expected_data)

def test_03_read_sectors_span_cylinder(disk_setup):
    disk, geom = disk_setup
    c, h, start_s = 0, 1, 3; num_s = 2
    expected_len = num_s * geom.sector_size
    expected_data = bytes([13] * geom.sector_size) + \
                    bytes([101] * geom.sector_size)
    read_data = disk.read_sectors(c, h, start_s, num_s)
    assert len(read_data) == expected_len
    assert read_data == bytes(expected_data)

def test_04_write_sectors_single_track(disk_setup):
    disk, geom = disk_setup
    c, h, start_s = 1, 0, 1; num_s = 2
    sector_size = geom.sector_size
    write_data = bytes([0xAA] * sector_size) + bytes([0xBB] * sector_size)

    disk.write_sector = MagicMock() # Mock the single sector write method
    disk.write_sectors(c, h, start_s, write_data)

    expected_calls = [
        call(1, 0, 1, bytes([0xAA] * sector_size)),
        call(1, 0, 2, bytes([0xBB] * sector_size)),
    ]
    disk.write_sector.assert_has_calls(expected_calls)
    assert disk.write_sector.call_count == 2

def test_05_write_sectors_span_track_cylinder(disk_setup):
    disk, geom = disk_setup
    c, h, start_s = 0, 1, 2; num_s = 3
    sector_size = geom.sector_size
    write_data = bytes([0x11] * sector_size) + \
                 bytes([0x22] * sector_size) + \
                 bytes([0x33] * sector_size)

    disk.write_sector = MagicMock()
    disk.write_sectors(c, h, start_s, write_data)

    expected_calls = [
        call(0, 1, 2, bytes([0x11] * sector_size)),
        call(0, 1, 3, bytes([0x22] * sector_size)),
        call(1, 0, 1, bytes([0x33] * sector_size)),
    ]
    disk.write_sector.assert_has_calls(expected_calls)
    assert disk.write_sector.call_count == 3

def test_06_write_sectors_padding(disk_setup):
    disk, geom = disk_setup
    c, h, start_s = 1, 1, 1; num_s = 2
    sector_size = geom.sector_size
    partial_data_len = sector_size + 50
    write_data = bytes([0xCC] * partial_data_len)
    expected_s1_data = bytes([0xCC] * sector_size)
    expected_s2_data = bytes([0xCC] * 50) + bytes([0x00] * (sector_size - 50))

    disk.write_sector = MagicMock()
    disk.write_sectors(c, h, start_s, write_data)

    expected_calls = [
        call(1, 1, 1, expected_s1_data),
        call(1, 1, 2, expected_s2_data),
    ]
    disk.write_sector.assert_has_calls(expected_calls)
    assert disk.write_sector.call_count == 2

def test_07_disk_error_no_geometry_read(disk_setup):
    _, geom = disk_setup
    # Create disk without geometry for this test
    disk_no_geom = Disk(disk_setup[0].driver) # Use the driver from the fixture
    with pytest.raises(ValueError, match="Disk geometry not set"):
        disk_no_geom.read_sector(0, 0, 1)
    with pytest.raises(ValueError, match="Disk geometry not set"):
        disk_no_geom.read_sectors(0, 0, 1, 1)

def test_08_disk_error_no_geometry_write(disk_setup):
    disk_no_geom = Disk(disk_setup[0].driver)
    with pytest.raises(ValueError, match="Disk geometry not set"):
        disk_no_geom.write_sector(0, 0, 1, b'\x00'*128)
    with pytest.raises(ValueError, match="Disk geometry not set"):
        disk_no_geom.write_sectors(0, 0, 1, b'\x00'*128)

def test_09_disk_error_invalid_address_read(disk_setup):
    disk, geom = disk_setup
    with pytest.raises(ValueError, match="Invalid sector address"):
        disk.read_sector(geom.cylinders, 0, 1) # Invalid Cylinder
    with pytest.raises(ValueError, match="Invalid sector address"):
        disk.read_sector(0, geom.heads, 1) # Invalid Head
    with pytest.raises(ValueError, match="Invalid sector address"):
        disk.read_sector(0, 0, 0) # Invalid Sector (too low)
    with pytest.raises(ValueError, match="Invalid sector address"):
        disk.read_sector(0, 0, geom.sectors_per_track + 1) # Invalid Sector (too high)

def test_10_disk_error_invalid_address_write(disk_setup):
    disk, geom = disk_setup
    data = b'\x00'*geom.sector_size
    with pytest.raises(ValueError, match="Invalid sector address"):
        disk.write_sector(geom.cylinders, 0, 1, data) # Invalid Cylinder
    with pytest.raises(ValueError, match="Invalid sector address"):
        disk.write_sector(0, geom.heads, 1, data) # Invalid Head
    with pytest.raises(ValueError, match="Invalid sector address"):
        disk.write_sector(0, 0, 0, data) # Invalid Sector (too low)
    with pytest.raises(ValueError, match="Invalid sector address"):
        disk.write_sector(0, 0, geom.sectors_per_track + 1, data) # Invalid Sector (too high)

def test_11_disk_error_invalid_write_size(disk_setup):
    disk, geom = disk_setup
    with pytest.raises(ValueError, match="Data size .* does not match geometry sector size"):
        disk.write_sector(0, 0, 1, b'\x00'*(geom.sector_size - 1))
    with pytest.raises(ValueError, match="Data size .* does not match geometry sector size"):
        disk.write_sector(0, 0, 1, b'\x00'*(geom.sector_size + 1))

def test_12_disk_set_geometry_updates_driver_format(disk_setup):
    disk, geom = disk_setup
    driver = disk.driver
    assert driver.physical_format.sectors_per_track == 3
    assert driver.physical_format.heads == 2
    assert driver.physical_format.sector_size == 128

    new_geom = DiskGeometry(cylinders=10, heads=1, sectors_per_track=5, sector_size=256)
    disk.set_geometry(new_geom)

    assert disk.geometry == new_geom
    assert driver.physical_format is not None
    assert driver.physical_format.sectors_per_track == 5
    assert driver.physical_format.heads == 1
    assert driver.physical_format.sector_size == 256
    assert driver.physical_format.encoding == "MFM" # Check non-geom field preserved

def test_13_disk_set_geometry_creates_driver_format(disk_setup):
    # Create driver without format for this specific test
    driver_no_fmt = RawImageDriver("dummy_path", image_data=disk_setup[0].driver.image_data)
    disk_no_fmt = Disk(driver_no_fmt)
    assert driver_no_fmt.physical_format is None

    geom_to_set = DiskGeometry(cylinders=5, heads=1, sectors_per_track=8, sector_size=512)
    disk_no_fmt.set_geometry(geom_to_set)

    assert driver_no_fmt.physical_format is not None
    assert driver_no_fmt.physical_format.sectors_per_track == 8
    assert driver_no_fmt.physical_format.heads == 1
    assert driver_no_fmt.physical_format.sector_size == 512
    assert driver_no_fmt.physical_format.encoding == "MFM"
    assert driver_no_fmt.physical_format.rate == 500
    assert driver_no_fmt.physical_format.rpm == 300
