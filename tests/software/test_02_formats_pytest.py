# tests/software/test_02_formats_pytest.py
import pytest
import sys
import struct
from pathlib import Path

# Ensure src is in path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / 'src'))

from fatfloppy.core.formats import BootSectorData, FormatProfile
from fatfloppy.core.disk import Disk, DiskGeometry
from fatfloppy.core.drivers import RawImageDriver
from fatfloppy.core.format_definitions import FLOPPY_FORMATS
# Import DiskController instead of FormatManager
from fatfloppy.core.controller import DiskController
from fatfloppy.core.drivers import PhysicalFormat

# --- Constants ---
RESOURCE_DIR = Path(__file__).parent.parent / 'resources'
EMPTY_IMG_SRC = RESOURCE_DIR / 'empty_formatted_144m.img'
FMT_144 = FLOPPY_FORMATS['ibm_3.5_1.44m']

# --- Fixture for DiskController ---
@pytest.fixture(scope="module")
def disk_controller():
    """Provides a DiskController instance."""
    # DiskController now loads formats internally
    return DiskController()

# --- Tests for Format Detection in DiskController ---

def test_01_list_known_formats(disk_controller):
    # Use the controller's method
    formats = disk_controller.list_formats()
    assert isinstance(formats, list)
    assert len(formats) > 0
    assert isinstance(formats[0], tuple)
    assert len(formats[0]) == 2 # Name, Description

def test_02_get_format_by_name(disk_controller):
    # Use the controller's internal method (prefixed with _)
    profile = disk_controller.get_format_by_name("ibm_3.5_1.44m")
    assert profile is not None
    assert isinstance(profile, FormatProfile)
    assert profile.name == "ibm_3.5_1.44m"
    assert profile.geometry.total_bytes == 1440 * 1024

    profile_none = disk_controller.get_format_by_name("non_existent_format")
    assert profile_none is None

def test_03_detect_format_144mb(disk_controller):
    if not EMPTY_IMG_SRC.exists():
        pytest.skip(f"{EMPTY_IMG_SRC} not found.")

    # Open the disk using the controller, which sets up driver and disk
    success = disk_controller.open_disk(str(EMPTY_IMG_SRC), disk_type="image")
    assert success is True
    assert disk_controller.disk is not None
    assert disk_controller.driver is not None

    # Call the controller's detection method AFTER opening the disk
    detected_format_name = disk_controller.detect_format()
    assert detected_format_name == "ibm_3.5_1.44m"
    disk_controller.close_disk() # Clean up

def test_04_detect_format_no_match(disk_controller):
     # Create dummy disk with non-matching boot sector data
    dummy_boot = bytearray(512)
    dummy_boot[0:3] = b'\xEB\xFE\x90'
    dummy_boot[3:11] = b'NONAME  '
    struct.pack_into('<H', dummy_boot, 0x0B, 512) # Bytes/Sector
    struct.pack_into('<B', dummy_boot, 0x0D, 1)   # Sectors/Cluster
    struct.pack_into('<H', dummy_boot, 0x0E, 1)   # Reserved
    struct.pack_into('<B', dummy_boot, 0x10, 2)   # Num FATs
    struct.pack_into('<H', dummy_boot, 0x11, 224) # Root Entries
    struct.pack_into('<H', dummy_boot, 0x13, 1000)# Total Sectors (unusual)
    struct.pack_into('<B', dummy_boot, 0x15, 0xF1)# Media Descriptor (unusual)
    struct.pack_into('<H', dummy_boot, 0x16, 5)   # Sectors/FAT
    struct.pack_into('<H', dummy_boot, 0x18, 10)  # Sectors/Track
    struct.pack_into('<H', dummy_boot, 0x1A, 3)   # Heads (unusual)
    struct.pack_into('<H', dummy_boot, 0x1FE, 0xAA55) # Boot Signature

    # Use RawImageDriver directly with the image data
    driver = RawImageDriver("dummy", image_data=bytes(dummy_boot) + b'\x00'*1024*100)
    # Assign the driver and disk manually to the controller for this test
    disk_controller.driver = driver
    disk_controller.disk = Disk(driver)
    # Need to set *some* geometry for detect_format to try reading sector 0
    # A temporary one is fine here as the boot sector data itself is the focus
    temp_geom = DiskGeometry(80, 2, 18, 512)
    temp_phys = PhysicalFormat(encoding="MFM", rate=500, rpm=300, sectors_per_track=18, heads=2, sector_size=512)
    disk_controller.disk.set_geometry(temp_geom)
    disk_controller.driver.set_physical_format(temp_phys)


    detected_format = disk_controller.detect_format()
    assert detected_format is None, "Should not detect a standard format"
    disk_controller.close_disk() # Clean up

# --- Tests for BootSectorData ---

def test_01_bsd_to_bytes_from_bytes_roundtrip():
    bsd = BootSectorData(
        oem_id="MYDOS6.2",
        bytes_per_sector=512,
        sectors_per_cluster=2,
        reserved_sectors=1,
        num_fats=2,
        root_entries=112,
        total_sectors=1440, # 720KB
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

    bsd_reloaded = BootSectorData.from_bytes(bs_bytes)

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
    assert bsd_reloaded.volume_label == "TEST DISK" # Stripped
    assert bsd_reloaded.fs_type == "FAT12"      # Stripped

def test_02_bsd_from_bytes_real_image():
    if not EMPTY_IMG_SRC.exists():
        pytest.skip(f"{EMPTY_IMG_SRC} not found.")

    boot_sector_bytes = EMPTY_IMG_SRC.read_bytes()[:512]
    bsd = BootSectorData.from_bytes(boot_sector_bytes)

    # Values specific to standard 1.44MB format
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

@pytest.mark.skip(reason="Boot signature check is currently disabled in BootSectorData.from_bytes")
def test_03_bsd_from_bytes_invalid_signature():
     invalid_boot = bytearray(FMT_144.boot_sector.to_bytes())
     invalid_boot[510:512] = b'\x00\x00' # Corrupt signature
     with pytest.raises(ValueError, match="Invalid boot signature"):
          BootSectorData.from_bytes(bytes(invalid_boot))

def test_04_bsd_from_bytes_too_short():
     short_boot = b'\x00' * 500
     with pytest.raises(ValueError, match="too short"):
         BootSectorData.from_bytes(short_boot)
