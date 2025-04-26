# tests/software/test_01_drivers_pytest.py
import pytest
import sys
import shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / 'src'))

from fatfloppy.core.drivers import IMGImageDriver
from fatfloppy.core.format_definitions import FLOPPY_FORMATS
from fatfloppy.core.disk import Disk

RESOURCE_DIR = Path(__file__).parent.parent / 'resources'
EMPTY_IMG_SRC = RESOURCE_DIR / 'empty_formatted_144m.img'
FMT_144 = FLOPPY_FORMATS['ibm_3.5_1.44m']
FMT_720 = FLOPPY_FORMATS['ibm_3.5_720k']

@pytest.fixture(scope="function")
def driver_setup(tmp_path):
    test_img_path = tmp_path / "test_driver_1.44mb.img"
    test_format = FMT_144.physical_format # Keep reference to the full format object
    bytes_per_sector = test_format.bytes_per_sector
    if EMPTY_IMG_SRC.exists():
        shutil.copy(EMPTY_IMG_SRC, test_img_path)
    else:
        pytest.skip(f"Resource file not found: {EMPTY_IMG_SRC}")
    driver = IMGImageDriver(str(test_img_path))
    driver.set_physical_format(test_format)
    yield driver, test_img_path, bytes_per_sector, test_format # Yield the full format object
    print(f"\n[Fixture Teardown] Driver test image {test_img_path} cleanup.")

def test_01_initialization_from_file(driver_setup):
    driver, test_img_path, _, geom = driver_setup
    assert test_img_path.exists()
    assert len(driver.image_data) > 0
    expected_total_bytes = geom.total_sectors * geom.bytes_per_sector
    assert len(driver.image_data) == expected_total_bytes

def test_02_initialization_from_bytes():
    initial_data = b'\xAA' * 512 * 10
    driver_bytes = IMGImageDriver("dummy_path_not_used.img", image_data=initial_data)
    driver_bytes.set_physical_format(FMT_144.physical_format)
    assert driver_bytes.image_data == bytearray(initial_data)
    assert driver_bytes.dirty is True

def test_03_set_physical_format(driver_setup):
    driver, _, _, _ = driver_setup
    fmt_720_phys = FMT_720.physical_format
    driver.set_physical_format(fmt_720_phys)
    assert driver.physical_format is not None
    assert driver.physical_format.get_sectors_per_track(0, 0) == fmt_720_phys.get_sectors_per_track(0, 0)
    assert driver.physical_format.heads == fmt_720_phys.heads
    assert driver.physical_format.bytes_per_sector == fmt_720_phys.bytes_per_sector

def test_04_read_sector(driver_setup):
    driver, _, bytes_per_sector, _ = driver_setup
    boot_sector = driver.read_sector(0, 0, 1)
    assert len(boot_sector) == bytes_per_sector
    if EMPTY_IMG_SRC.exists():
        assert boot_sector[510:512] == b'\x55\xAA'

def test_05_write_sector_and_flush(driver_setup):
    driver, test_img_path, bytes_per_sector, geom = driver_setup # Get geom
    test_data = b'TEST' + b'\xEE' * (bytes_per_sector - 4)
    cyl, head, sect = 5, 1, 3
    driver.write_sector(cyl, head, sect, test_data)
    assert driver.dirty is True
    offset = geom.chs_to_lba(cyl, head, sect) * bytes_per_sector # Use geom method
    assert driver.image_data[offset:offset + len(test_data)] == test_data
    driver.flush()
    assert driver.dirty is False
    driver2 = IMGImageDriver(str(test_img_path))
    driver2.set_physical_format(geom) # Use the same geom
    read_data = driver2.read_sector(cyl, head, sect)
    assert read_data == test_data

def test_06_read_beyond_image_size(driver_setup):
    driver, _, bytes_per_sector, geom = driver_setup
    invalid_cyl = geom.cylinders # Cylinder index is 0-based, so this is out of bounds
    invalid_head, invalid_sect = 0, 1
    disk = Disk(driver)
    disk.set_geometry(geom)
    # --- FIX: Expect ValueError from validate_chs ---
    with pytest.raises(ValueError, match=f"Invalid CHS: {invalid_cyl}, {invalid_head}, {invalid_sect}"):
         disk.read_sector(invalid_cyl, invalid_head, invalid_sect)
    # --- End Fix ---

    last_valid_c, last_valid_h = geom.cylinders - 1, geom.heads - 1
    last_valid_s = geom.get_sectors_per_track(last_valid_c, last_valid_h)
    # Read last valid sector using the driver directly to avoid Disk validation
    last_sector_data = driver.read_sector(last_valid_c, last_valid_h, last_valid_s)
    assert len(last_sector_data) == bytes_per_sector

def test_07_write_within_bounds(driver_setup):
    driver, test_img_path, bytes_per_sector, geom = driver_setup
    test_data = b'LAST' * (bytes_per_sector // 4)
    assert len(test_data) == bytes_per_sector

    # Write to the last valid sector
    last_cyl, last_head = geom.cylinders - 1, geom.heads - 1
    last_sect = geom.get_sectors_per_track(last_cyl, last_head)
    driver.write_sector(last_cyl, last_head, last_sect, test_data)
    driver.flush()
    driver2 = IMGImageDriver(str(test_img_path))
    driver2.set_physical_format(geom) # Use the same geom
    read_data = driver2.read_sector(last_cyl, last_head, last_sect)
    assert read_data == test_data

    # Check exceptions raised by write_sector (IOError wrapping ValueError from driver layer)
    # The driver's write_sector catches the initial ValueError from validate_chs
    # and re-raises it as IOError. This differs from Disk.read_sector.
    invalid_cyl = geom.cylinders
    with pytest.raises(ValueError, match=f"Invalid sector access: Invalid CHS: {invalid_cyl}, 0, 1"):
        driver.write_sector(invalid_cyl, 0, 1, test_data)

    invalid_head = geom.heads
    with pytest.raises(ValueError, match=f"Invalid sector access: Invalid CHS: 0, {invalid_head}, 1"):
        driver.write_sector(0, invalid_head, 1, test_data)

    max_spt = geom.get_sectors_per_track(0, 0)
    invalid_sect = max_spt + 1
    with pytest.raises(ValueError, match=f"Invalid sector access: Sector {invalid_sect} out of range"):
        driver.write_sector(0, 0, invalid_sect, test_data)
