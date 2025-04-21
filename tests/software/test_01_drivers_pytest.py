# tests/software/test_01_drivers_pytest.py
import pytest
import sys
import shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / 'src'))

from fatfloppy.core.drivers import RawImageDriver, PhysicalFormat
from fatfloppy.core.format_definitions import FLOPPY_FORMATS
from fatfloppy.core.disk import Disk, DiskGeometry

RESOURCE_DIR = Path(__file__).parent.parent / 'resources'
EMPTY_IMG_SRC = RESOURCE_DIR / 'empty_formatted_144m.img'
FMT_144 = FLOPPY_FORMATS['ibm_3.5_1.44m']
FMT_720 = FLOPPY_FORMATS['ibm_3.5_720k']

def _manual_lba_to_chs(lba: int, geom: DiskGeometry) -> tuple[int, int, int]:
    if geom.sectors_per_track <= 0 or geom.heads <= 0:
        raise ValueError("Invalid geometry for LBA->CHS conversion")
    if not (0 <= lba < geom.total_sectors):
        raise IndexError(f"LBA {lba} out of bounds for geometry (0-{geom.total_sectors-1})")
    sector = (lba % geom.sectors_per_track) + 1
    temp = lba // geom.sectors_per_track
    head = temp % geom.heads
    cylinder = temp // geom.sectors_per_track
    return cylinder, head, sector

@pytest.fixture(scope="function")
def driver_setup(tmp_path):
    test_img_path = tmp_path / "test_driver_1.44mb.img"
    sector_size = FMT_144.geometry.sector_size
    if EMPTY_IMG_SRC.exists():
        shutil.copy(EMPTY_IMG_SRC, test_img_path)
    else:
        pytest.skip(f"Resource file not found: {EMPTY_IMG_SRC}")
    driver = RawImageDriver(str(test_img_path))
    driver.set_physical_format(FMT_144.physical_format)
    yield driver, test_img_path, sector_size, FMT_144.geometry
    print(f"\n[Fixture Teardown] Driver test image {test_img_path} cleanup.")

def test_01_initialization_from_file(driver_setup):
    driver, test_img_path, _, geom = driver_setup
    assert test_img_path.exists()
    assert len(driver.image_data) > 0
    assert len(driver.image_data) == geom.total_bytes

def test_02_initialization_from_bytes():
    initial_data = b'\xAA' * 512 * 10
    driver_bytes = RawImageDriver("dummy_path_not_used.img", image_data=initial_data)
    driver_bytes.set_physical_format(FMT_144.physical_format)
    assert driver_bytes.image_data == bytearray(initial_data)
    assert driver_bytes.dirty is True

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
    boot_sector = driver.read_sector(0, 0, 1)
    assert len(boot_sector) == sector_size
    if EMPTY_IMG_SRC.exists():
        assert boot_sector[510:512] == b'\x55\xAA'

def test_05_write_sector_and_flush(driver_setup):
    driver, test_img_path, sector_size, _ = driver_setup
    test_data = b'TEST' + b'\xEE' * (sector_size - 4)
    cyl, head, sect = 5, 1, 3
    driver.write_sector(cyl, head, sect, test_data)
    assert driver.dirty is True
    offset = driver._calculate_sector_offset(cyl, head, sect, sector_size)
    assert driver.image_data[offset:offset + len(test_data)] == test_data
    driver.flush()
    assert driver.dirty is False
    driver2 = RawImageDriver(str(test_img_path))
    driver2.set_physical_format(FMT_144.physical_format)
    read_data = driver2.read_sector(cyl, head, sect)
    assert read_data == test_data

def test_06_read_beyond_image_size(driver_setup):
    driver, _, sector_size, geom = driver_setup
    invalid_cyl = geom.cylinders
    invalid_head, invalid_sect = 0, 1
    disk = Disk(driver)
    disk.set_geometry(geom)
    with pytest.raises(ValueError, match="Invalid sector address"):
        disk.read_sector(invalid_cyl, invalid_head, invalid_sect)
    last_valid_c, last_valid_h, last_valid_s = geom.cylinders - 1, geom.heads - 1, geom.sectors_per_track
    last_sector_data = driver.read_sector(last_valid_c, last_valid_h, last_valid_s)
    assert len(last_sector_data) == sector_size

def test_07_write_within_bounds(driver_setup):
    driver, test_img_path, sector_size, geom = driver_setup
    test_data = b'LAST' * (sector_size // 4)
    assert len(test_data) == sector_size
    last_cyl, last_head, last_sect = geom.cylinders - 1, geom.heads - 1, geom.sectors_per_track
    driver.write_sector(last_cyl, last_head, last_sect, test_data)
    driver.flush()
    driver2 = RawImageDriver(str(test_img_path))
    driver2.set_physical_format(FMT_144.physical_format)
    read_data = driver2.read_sector(last_cyl, last_head, last_sect)
    assert read_data == test_data

    invalid_cyl = geom.cylinders
    with pytest.raises(IOError, match="Cannot write sector .* out of bounds"):
        driver.write_sector(invalid_cyl, 0, 1, test_data)

    invalid_head = geom.heads + 1
    with pytest.raises(IOError, match="Invalid sector access"):
        driver.write_sector(0, invalid_head, 1, test_data)

    invalid_sect = geom.sectors_per_track + 25
    with pytest.raises(IOError, match="Invalid sector access"):
        driver.write_sector(0, 0, invalid_sect, test_data)
