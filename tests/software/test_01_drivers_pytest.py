# tests/software/test_01_drivers_pytest.py
import pytest
import sys
import shutil
from pathlib import Path

# Ensure src is in path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / 'src'))

from fatfloppy.core.drivers import RawImageDriver, PhysicalFormat
from fatfloppy.core.format_definitions import FLOPPY_FORMATS
from fatfloppy.core.disk import Disk, DiskGeometry # Import Disk for geometry test

# --- Constants ---
RESOURCE_DIR = Path(__file__).parent.parent / 'resources'
EMPTY_IMG_SRC = RESOURCE_DIR / 'empty_formatted_144m.img'
FMT_144 = FLOPPY_FORMATS['ibm_3.5_1.44m']
FMT_720 = FLOPPY_FORMATS['ibm_3.5_720k']

# --- Helper ---
def _manual_lba_to_chs(lba: int, geom: DiskGeometry) -> tuple[int, int, int]:
    """Manually converts LBA to CHS based on geometry."""
    if geom.sectors_per_track <= 0 or geom.heads <= 0:
        raise ValueError("Invalid geometry for LBA->CHS conversion")
    if not (0 <= lba < geom.total_sectors):
         raise IndexError(f"LBA {lba} out of bounds for geometry (0-{geom.total_sectors-1})")

    sector = (lba % geom.sectors_per_track) + 1
    temp = lba // geom.sectors_per_track
    head = temp % geom.heads
    cylinder = temp // geom.sectors_per_track
    return cylinder, head, sector

# --- Fixture ---
@pytest.fixture(scope="function")
def driver_setup(tmp_path):
    """Sets up a RawImageDriver with a temporary image copy."""
    test_img_path = tmp_path / "test_driver_1.44mb.img"
    sector_size = FMT_144.geometry.sector_size
    total_bytes = FMT_144.geometry.total_bytes

    # Copy a fresh image for each test or create a zeroed file
    if EMPTY_IMG_SRC.exists():
        shutil.copy(EMPTY_IMG_SRC, test_img_path)
    else:
        pytest.skip(f"Resource file not found: {EMPTY_IMG_SRC}")
        # print(f"Warning: {EMPTY_IMG_SRC} not found. Creating zeroed file.")
        # with open(test_img_path, "wb") as f:
        #     f.write(b'\x00' * total_bytes)

    driver = RawImageDriver(str(test_img_path))
    # Important: Set physical format *before* using methods that rely on it
    driver.set_physical_format(FMT_144.physical_format)

    # Yield necessary objects for tests
    yield driver, test_img_path, sector_size, FMT_144.geometry

    # Teardown is handled automatically by tmp_path fixture
    print(f"\n[Fixture Teardown] Driver test image {test_img_path} cleanup.")


# --- Tests ---

def test_01_initialization_from_file(driver_setup):
    driver, test_img_path, _, geom = driver_setup
    assert test_img_path.exists()
    assert len(driver.image_data) > 0
    assert len(driver.image_data) == geom.total_bytes

def test_02_initialization_from_bytes():
    initial_data = b'\xAA' * 512 * 10 # 10 sectors worth
    driver_bytes = RawImageDriver("dummy_path_not_used.img", image_data=initial_data)
    driver_bytes.set_physical_format(FMT_144.physical_format) # Set format
    assert driver_bytes.image_data == bytearray(initial_data)
    assert driver_bytes.dirty is True # Should be dirty if initialized from data

def test_03_set_physical_format(driver_setup):
    driver, _, _, _ = driver_setup
    fmt_720_phys = FMT_720.physical_format
    fmt_720_geom = FMT_720.geometry
    driver.set_physical_format(fmt_720_phys)

    assert driver.physical_format is not None
    assert driver.physical_format.sectors_per_track == fmt_720_geom.sectors_per_track
    assert driver.physical_format.heads == fmt_720_geom.heads
    assert driver.physical_format.sector_size == fmt_720_geom.sector_size

def test_04_read_sector(driver_setup):
    driver, _, sector_size, _ = driver_setup
    # Read boot sector (assuming it's formatted)
    boot_sector = driver.read_sector(0, 0, 1)
    assert len(boot_sector) == sector_size
    # Check boot signature if image is properly formatted
    if EMPTY_IMG_SRC.exists():
         assert boot_sector[510:512] == b'\x55\xAA'

def test_05_write_sector_and_flush(driver_setup):
    driver, test_img_path, sector_size, _ = driver_setup
    test_data = b'TEST' + b'\xEE' * (sector_size - 4)
    cyl, head, sect = 5, 1, 3

    # Write data
    driver.write_sector(cyl, head, sect, test_data)
    assert driver.dirty is True

    # Verify data is in memory buffer
    offset = driver._calculate_sector_offset(cyl, head, sect, sector_size)
    assert driver.image_data[offset:offset+len(test_data)] == test_data

    # Flush to disk
    driver.flush()
    assert driver.dirty is False

    # Create a new driver instance to read from the flushed file
    driver2 = RawImageDriver(str(test_img_path))
    driver2.set_physical_format(FMT_144.physical_format) # Set format for driver2
    read_data = driver2.read_sector(cyl, head, sect)
    assert read_data == test_data

def test_06_read_beyond_image_size(driver_setup):
    driver, _, sector_size, geom = driver_setup
    # Use geometry to find a sector just outside the image
    invalid_cyl = geom.cylinders # Invalid cylinder for Disk class check
    invalid_head, invalid_sect = 0, 1

    # Test Disk class behavior (should raise ValueError due to geometry check)
    disk = Disk(driver)
    disk.set_geometry(geom) # Set geometry on Disk object
    with pytest.raises(ValueError, match="Invalid sector address"):
         disk.read_sector(invalid_cyl, invalid_head, invalid_sect)

    # Test driver's behavior directly (returns zeros for reads past end)
    last_valid_c, last_valid_h, last_valid_s = geom.cylinders - 1, geom.heads - 1, geom.sectors_per_track
    last_sector_offset = driver._calculate_sector_offset(
        last_valid_c, last_valid_h, last_valid_s, sector_size
    )
    # Read the last valid sector to confirm offset calculation is reasonable
    last_sector_data = driver.read_sector(last_valid_c, last_valid_h, last_valid_s)
    assert len(last_sector_data) == sector_size

    # Now try reading past this offset directly using read_bytes_direct
    past_end_offset = len(driver.image_data)
    read_data = driver.read_bytes_direct(past_end_offset, sector_size)
    assert read_data == b'\x00' * sector_size, "Direct read past end should return zeros"

    # Also test read_sector for a valid CHS that falls outside the image buffer
    lba_past_end = len(driver.image_data) // sector_size
    if lba_past_end < geom.total_sectors: # Make sure the LBA is within geometry bounds
         cyl_past, head_past, sect_past = _manual_lba_to_chs(lba_past_end, geom)
         read_data_past = driver.read_sector(cyl_past, head_past, sect_past)
         assert read_data_past == b'\x00' * sector_size, "read_sector past image end should return zeros"

@pytest.mark.skip(reason="TODO: bad logic in the code, we shouldn't allow extending the image!!")
def test_07_write_extends_image(driver_setup):
    driver, test_img_path, sector_size, geom = driver_setup
    initial_size = len(driver.image_data)
    # Use data slightly larger than one sector to guarantee extension needs
    test_data = b'EXTEND' * (sector_size // 6 + 1)
    assert len(test_data) > sector_size

    # Calculate offset of the last valid sector to write the first part
    cyl, head, sect = geom.cylinders -1, geom.heads -1, geom.sectors_per_track
    offset = driver._calculate_sector_offset(cyl, head, sect, sector_size)

    # Write first part to the last sector
    first_chunk = test_data[:sector_size]
    driver.write_sector(cyl, head, sect, first_chunk)

    # Calculate CHS for the *next* logical sector (which needs extending)
    last_lba = (geom.total_sectors - 1)
    next_lba = last_lba + 1

    if next_lba >= geom.total_sectors:
         pytest.skip("Calculated next LBA is outside disk geometry, cannot test extension this way.")

    next_cyl, next_head, next_sect = _manual_lba_to_chs(next_lba, geom)

    # Write the second part
    second_chunk = test_data[sector_size:]
    driver.write_sector(next_cyl, next_head, next_sect, second_chunk) # write_sector pads if needed

    # Calculate the final expected image size based on the last write operation
    required_offset = (next_lba * sector_size) + sector_size
    assert len(driver.image_data) >= required_offset, "Image data should be extended"
    assert driver.dirty is True

    # Flush and verify size on disk
    driver.flush()
    assert driver.dirty is False
    assert test_img_path.stat().st_size >= required_offset

    # Verify data can be read back from both sectors
    driver2 = RawImageDriver(str(test_img_path))
    driver2.set_physical_format(FMT_144.physical_format)
    read_data1 = driver2.read_sector(cyl, head, sect)
    read_data2 = driver2.read_sector(next_cyl, next_head, next_sect)

    combined_read = read_data1 + read_data2[:len(second_chunk)]
    assert combined_read == test_data

def test_08_read_bytes_direct(driver_setup):
    driver, _, _, _ = driver_setup
    # Read boot sector signature directly
    data = driver.read_bytes_direct(510, 2)
    if EMPTY_IMG_SRC.exists():
         assert data == b'\x55\xAA'

    # Read past end of file
    read_data = driver.read_bytes_direct(len(driver.image_data) - 10, 20)
    assert len(read_data) == 20
    expected_start = driver.image_data[-10:]
    expected_padding = b'\x00' * 10
    assert read_data == expected_start + expected_padding
