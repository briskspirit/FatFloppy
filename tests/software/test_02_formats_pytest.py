# tests/software/test_02_formats_pytest.py
import pytest
import sys
import re
import struct
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / 'src'))

from fatfloppy.core.formats import FATVolumeInfo, FormatProfile
from fatfloppy.core.disk import Disk
from fatfloppy.core.drivers import RawImageDriver
from fatfloppy.core.format_definitions import FLOPPY_FORMATS
from fatfloppy.core.controller import DiskController
from fatfloppy.core.drivers import PhysicalFormat

RESOURCE_DIR = Path(__file__).parent.parent / 'resources'
EMPTY_IMG_SRC = RESOURCE_DIR / 'empty_formatted_144m.img'
FMT_144 = FLOPPY_FORMATS['ibm_3.5_1.44m']

@pytest.fixture(scope="module")
def disk_controller():
    return DiskController()

def test_01_list_known_formats(disk_controller):
    formats = disk_controller.list_formats()
    assert isinstance(formats, list)
    assert len(formats) > 0
    assert isinstance(formats[0], tuple)
    assert len(formats[0]) == 2

def test_02_get_format_by_name(disk_controller):
    profile = disk_controller.get_format_by_name("ibm_3.5_1.44m")
    assert profile is not None
    assert isinstance(profile, FormatProfile)
    assert profile.name == "ibm_3.5_1.44m"
    assert profile.physical_format.total_bytes == 1440 * 1024
    profile_none = disk_controller.get_format_by_name("non_existent_format")
    assert profile_none is None

def test_03_detect_format_144mb(disk_controller):
    if not EMPTY_IMG_SRC.exists():
        pytest.skip(f"{EMPTY_IMG_SRC} not found.")
    success = disk_controller.open_disk(str(EMPTY_IMG_SRC), disk_type="image")
    assert success is True
    assert disk_controller.disk is not None
    assert disk_controller.driver is not None
    detected_format_result = disk_controller.detect_format()
    assert isinstance(detected_format_result, tuple), "detect_format should return a tuple"
    assert detected_format_result[0] == "ibm_3.5_1.44m"
    assert isinstance(detected_format_result[1], FATVolumeInfo), "Second element should be FATVolumeInfo"
    disk_controller.close_disk()

def test_04_detect_format_no_match(disk_controller):
    dummy_boot = bytearray(512)
    dummy_boot[0:3] = b'\xEB\xFE\x90'
    dummy_boot[3:11] = b'NONAME  '
    struct.pack_into('<H', dummy_boot, 0x0B, 512)
    struct.pack_into('<B', dummy_boot, 0x0D, 1)
    struct.pack_into('<H', dummy_boot, 0x0E, 1)
    struct.pack_into('<B', dummy_boot, 0x10, 2)
    struct.pack_into('<H', dummy_boot, 0x11, 224)
    struct.pack_into('<H', dummy_boot, 0x13, 1000)
    struct.pack_into('<B', dummy_boot, 0x15, 0xF1)
    struct.pack_into('<H', dummy_boot, 0x16, 5)
    struct.pack_into('<H', dummy_boot, 0x18, 10)
    struct.pack_into('<H', dummy_boot, 0x1A, 3)
    struct.pack_into('<H', dummy_boot, 0x1FE, 0xAA55)
    driver = RawImageDriver("dummy", image_data=bytes(dummy_boot) + b'\x00' * 1024 * 100)
    disk_controller.driver = driver
    disk_controller.disk = Disk(driver)
    temp_geom = PhysicalFormat(
        encoding="MFM", rate=250, rpm=300, cylinders=80, heads=2,
        sectors_per_track=18, bytes_per_sector=512
    )
    disk_controller.disk.set_geometry(temp_geom)
    if hasattr(disk_controller.driver, "set_physical_format"):
         disk_controller.driver.set_physical_format(temp_geom)

    detected_format_result = disk_controller.detect_format()
    assert isinstance(detected_format_result, tuple), "detect_format should return a tuple"
    assert detected_format_result[0] is None, "Should not detect a standard format name"
    assert isinstance(detected_format_result[1], FATVolumeInfo), "Should still parse BPB data even if no known format matches"
    disk_controller.close_disk()
    disk_controller.driver = None
    disk_controller.disk = None

def test_01_bsd_to_bytes_from_bytes_roundtrip():
    bsd = FATVolumeInfo(
        oem_id="MYDOS6.2",
        bytes_per_sector=512,
        sectors_per_cluster=2,
        reserved_sectors=1,
        num_fats=2,
        root_entries=112,
        total_sectors=1440,
        media_descriptor=0xF9,
        sectors_per_fat=3,
        sectors_per_track=9,
        num_heads=2,
        hidden_sectors=0,
        volume_serial=0x12345678,
        volume_label="TEST DISK  ",
        fs_type="FAT12   "
    )
    bs_bytes = bsd.to_bytes()
    assert len(bs_bytes) == 512
    assert bs_bytes[510:512] == b'\x55\xAA'
    bsd_reloaded = FATVolumeInfo.from_bytes(bs_bytes)
    assert bsd_reloaded.oem_id == "MYDOS6.2"
    assert bsd_reloaded.bytes_per_sector == 512
    assert bsd_reloaded.sectors_per_cluster == 2
    assert bsd_reloaded.reserved_sectors == 1
    assert bsd_reloaded.num_fats == 2
    assert bsd_reloaded.root_entries == 112
    assert bsd_reloaded.total_sectors == 1440
    assert bsd_reloaded.media_descriptor == 0xF9
    assert bsd_reloaded.sectors_per_fat == 3
    assert bsd_reloaded.sectors_per_track == 9
    assert bsd_reloaded.num_heads == 2
    assert bsd_reloaded.hidden_sectors == 0
    assert bsd_reloaded.volume_serial == 0x12345678
    assert bsd_reloaded.volume_label.strip() == "TEST DISK"
    assert bsd_reloaded.fs_type.strip() == "FAT12"

def test_02_bsd_from_bytes_real_image():
    if not EMPTY_IMG_SRC.exists():
        pytest.skip(f"{EMPTY_IMG_SRC} not found.")
    boot_sector_bytes = EMPTY_IMG_SRC.read_bytes()[:512]
    bsd = FATVolumeInfo.from_bytes(boot_sector_bytes)
    assert bsd.bytes_per_sector == 512
    assert bsd.sectors_per_cluster == 1
    assert bsd.reserved_sectors == 1
    assert bsd.num_fats == 2
    assert bsd.root_entries == 224
    assert bsd.total_sectors == 2880
    assert bsd.media_descriptor == 0xF0
    assert bsd.sectors_per_fat == 9
    assert bsd.sectors_per_track == 18
    assert bsd.num_heads == 2
    assert bsd.fs_type.startswith("FAT12")

@pytest.mark.skip(reason="Boot signature check is currently disabled in FATVolumeInfo.from_bytes")
def test_03_bsd_from_bytes_invalid_signature():
    invalid_boot = bytearray(FMT_144.boot_sector.to_bytes())
    invalid_boot[510:512] = b'\x00\x00'
    with pytest.raises(ValueError, match="Invalid boot signature"):
        FATVolumeInfo.from_bytes(bytes(invalid_boot))

def test_04_bsd_from_bytes_too_short():
    short_boot = b'\x00' * 100
    try:
        FATVolumeInfo.from_bytes(short_boot)
        pytest.fail("ValueError was not raised for short boot sector")
    except ValueError as e:
        assert "Boot sector is too short" in str(e), \
            f"Expected 'Boot sector is too short' in exception message, but got: {str(e)}"
    except Exception as e:
        pytest.fail(f"Raised {type(e).__name__} instead of ValueError: {e}")
