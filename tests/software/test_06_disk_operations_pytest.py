# tests/software/test_06_disk_operations_pytest.py
import pytest
import sys
from unittest.mock import MagicMock, call
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / 'src'))

# Import the correct classes
from fatfloppy.core.disk import Disk
from fatfloppy.core.drivers import IMGImageDriver
from fatfloppy.core.physical_format import PhysicalFormat, TrackFormat

@pytest.fixture(scope="function")
def disk_setup(tmp_path):
    # Define geometry parameters clearly
    cylinders = 2
    heads = 2
    sectors_per_track = 3
    bytes_per_sector = 128

    # Create TrackFormat and PhysicalFormat
    track_fmt = TrackFormat(
        track_start=0,
        track_end=cylinders - 1,
        head_start=0,
        head_end=heads - 1,
        sectors_per_track=sectors_per_track,
        encoding="MFM",
        rate=500, # Example rate
        gap3=42,  # Example gap
        interleave=1
    )
    phys_fmt = PhysicalFormat(
        cylinders=cylinders,
        heads=heads,
        rpm=300,
        heads_inverted=False,
        bytes_per_sector=bytes_per_sector,
        track_formats=[track_fmt]
    )

    # Calculate total size and create initial data
    total_sectors = phys_fmt.total_sectors
    total_bytes = total_sectors * bytes_per_sector
    initial_data = bytearray(total_bytes)

    # Populate initial data based on CHS
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
                         print(f"Warning: Calculated offset {offset} out of bounds for initial data (size {len(initial_data)}) for CHS={c},{h},{s} LBA={lba}")
                except ValueError as e:
                    print(f"Warning: Error calculating LBA/offset for CHS={c},{h},{s}: {e}")
                except IndexError as e:
                    print(f"Warning: Index error writing initial data for CHS={c},{h},{s}: {e}")


    img_path = tmp_path / "disk_ops.img"
    # Instantiate driver and disk
    driver = IMGImageDriver(file_path=str(img_path), image_data=bytes(initial_data))
    disk = Disk(driver)
    # Set geometry on the disk (this also sets it on the driver if method exists)
    disk.set_geometry(phys_fmt)

    # Yield the disk and the PhysicalFormat object
    yield disk, phys_fmt

# --- Tests ---

def test_01_read_sectors_single_track(disk_setup):
    disk, phys_fmt = disk_setup
    c, h, start_s, num_s = 0, 1, 1, 2
    expected_len = num_s * phys_fmt.bytes_per_sector
    # Sector values: C=0, H=1, S=1 -> 11; C=0, H=1, S=2 -> 12
    expected_data = bytes([11] * phys_fmt.bytes_per_sector) + bytes([12] * phys_fmt.bytes_per_sector)
    read_data = disk.read_sectors(c, h, start_s, num_s)
    assert len(read_data) == expected_len
    assert read_data == expected_data

def test_02_read_sectors_span_track(disk_setup):
    disk, phys_fmt = disk_setup
    c, h, start_s, num_s = 0, 0, 2, 3 # Reads (0,0,2), (0,0,3), (0,1,1)
    expected_len = num_s * phys_fmt.bytes_per_sector
    # Sector values: C=0, H=0, S=2 -> 2; C=0, H=0, S=3 -> 3; C=0, H=1, S=1 -> 11
    expected_data = bytes([2] * phys_fmt.bytes_per_sector) + bytes([3] * phys_fmt.bytes_per_sector) + bytes([11] * phys_fmt.bytes_per_sector)
    read_data = disk.read_sectors(c, h, start_s, num_s)
    assert len(read_data) == expected_len
    assert read_data == expected_data

def test_03_read_sectors_span_cylinder(disk_setup):
    disk, phys_fmt = disk_setup
    c, h, start_s, num_s = 0, 1, 3, 2 # Reads (0,1,3), (1,0,1)
    expected_len = num_s * phys_fmt.bytes_per_sector
    # Sector values: C=0, H=1, S=3 -> 13; C=1, H=0, S=1 -> 101
    expected_data = bytes([13] * phys_fmt.bytes_per_sector) + bytes([101] * phys_fmt.bytes_per_sector)
    read_data = disk.read_sectors(c, h, start_s, num_s)
    assert len(read_data) == expected_len
    assert read_data == expected_data

def test_04_write_sectors_single_track(disk_setup):
    disk, phys_fmt = disk_setup
    c, h, start_s, num_s = 1, 0, 1, 2 # Writes (1,0,1), (1,0,2)
    bytes_per_sector = phys_fmt.bytes_per_sector
    write_data = bytes([0xAA] * bytes_per_sector) + bytes([0xBB] * bytes_per_sector)
    disk.write_sector = MagicMock() # Mock the underlying single write
    disk.write_sectors(c, h, start_s, write_data)
    expected_calls = [
        call(1, 0, 1, bytes([0xAA] * bytes_per_sector)),
        call(1, 0, 2, bytes([0xBB] * bytes_per_sector)),
    ]
    disk.write_sector.assert_has_calls(expected_calls)
    assert disk.write_sector.call_count == 2

def test_05_write_sectors_span_track_cylinder(disk_setup):
    disk, phys_fmt = disk_setup
    c, h, start_s, num_s = 0, 1, 2, 3 # Writes (0,1,2), (0,1,3), (1,0,1)
    bytes_per_sector = phys_fmt.bytes_per_sector
    write_data = bytes([0x11] * bytes_per_sector) + bytes([0x22] * bytes_per_sector) + bytes([0x33] * bytes_per_sector)
    disk.write_sector = MagicMock() # Mock the underlying single write
    disk.write_sectors(c, h, start_s, write_data)
    expected_calls = [
        call(0, 1, 2, bytes([0x11] * bytes_per_sector)),
        call(0, 1, 3, bytes([0x22] * bytes_per_sector)),
        call(1, 0, 1, bytes([0x33] * bytes_per_sector)),
    ]
    disk.write_sector.assert_has_calls(expected_calls)
    assert disk.write_sector.call_count == 3

def test_06_write_sectors_padding(disk_setup):
    disk, phys_fmt = disk_setup
    c, h, start_s, num_s = 1, 1, 1, 2 # Writes (1,1,1), (1,1,2)
    bytes_per_sector = phys_fmt.bytes_per_sector
    partial_data_len = bytes_per_sector + 50 # Data spans into second sector
    write_data = bytes([0xCC] * partial_data_len)
    expected_s1_data = bytes([0xCC] * bytes_per_sector)
    expected_s2_data = bytes([0xCC] * 50) + bytes([0x00] * (bytes_per_sector - 50)) # Padded second sector
    disk.write_sector = MagicMock() # Mock the underlying single write
    disk.write_sectors(c, h, start_s, write_data)
    expected_calls = [
        call(1, 1, 1, expected_s1_data),
        call(1, 1, 2, expected_s2_data),
    ]
    disk.write_sector.assert_has_calls(expected_calls)
    assert disk.write_sector.call_count == 2

def test_07_disk_error_no_geometry_read(disk_setup):
    # Create a disk without setting geometry
    disk_no_geom = Disk(disk_setup[0].driver)
    assert disk_no_geom.geometry is None
    with pytest.raises(ValueError, match="Disk geometry not set"):
        disk_no_geom.read_sector(0, 0, 1)
    with pytest.raises(ValueError, match="Disk geometry not set"):
        disk_no_geom.read_sectors(0, 0, 1, 1)

def test_08_disk_error_no_geometry_write(disk_setup):
    # Create a disk without setting geometry
    disk_no_geom = Disk(disk_setup[0].driver)
    assert disk_no_geom.geometry is None
    with pytest.raises(ValueError, match="Disk geometry not set"):
        disk_no_geom.write_sector(0, 0, 1, b'\x00' * 128) # Use correct size
    with pytest.raises(ValueError, match="Disk geometry not set"):
        disk_no_geom.write_sectors(0, 0, 1, b'\x00' * 128)

def test_09_disk_error_invalid_address_read(disk_setup):
    disk, phys_fmt = disk_setup
    # Accessing via disk calls _validate_chs which raises ValueError
    with pytest.raises(ValueError, match=f"Invalid CHS: {phys_fmt.cylinders}, 0, 1"):
        disk.read_sector(phys_fmt.cylinders, 0, 1)
    with pytest.raises(ValueError, match=f"Invalid CHS: 0, {phys_fmt.heads}, 1"):
        disk.read_sector(0, phys_fmt.heads, 1)
    with pytest.raises(ValueError, match="Sector 0 out of range"):
        disk.read_sector(0, 0, 0)
    max_spt = phys_fmt.get_sectors_per_track(0,0)
    with pytest.raises(ValueError, match=f"Sector {max_spt + 1} out of range"):
        disk.read_sector(0, 0, max_spt + 1)

def test_10_disk_error_invalid_address_write(disk_setup):
    disk, phys_fmt = disk_setup
    data = b'\x00' * phys_fmt.bytes_per_sector
    # Accessing via disk calls _validate_chs which raises ValueError
    with pytest.raises(ValueError, match=f"Invalid CHS: {phys_fmt.cylinders}, 0, 1"):
        disk.write_sector(phys_fmt.cylinders, 0, 1, data)
    with pytest.raises(ValueError, match=f"Invalid CHS: 0, {phys_fmt.heads}, 1"):
        disk.write_sector(0, phys_fmt.heads, 1, data)
    with pytest.raises(ValueError, match="Sector 0 out of range"):
        disk.write_sector(0, 0, 0, data)
    max_spt = phys_fmt.get_sectors_per_track(0,0)
    with pytest.raises(ValueError, match=f"Sector {max_spt + 1} out of range"):
        disk.write_sector(0, 0, max_spt + 1, data)

def test_11_disk_error_invalid_write_size(disk_setup):
    disk, phys_fmt = disk_setup
    with pytest.raises(ValueError, match="Data size .* != sector size .*"):
        disk.write_sector(0, 0, 1, b'\x00' * (phys_fmt.bytes_per_sector - 1))
    with pytest.raises(ValueError, match="Data size .* != sector size .*"):
        disk.write_sector(0, 0, 1, b'\x00' * (phys_fmt.bytes_per_sector + 1))

# Removed tests 12 and 13 as they tested outdated concepts.
# set_geometry implicitly updates the driver format if the method exists.
