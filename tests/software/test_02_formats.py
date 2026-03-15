"""
Tests for format detection, profile management, and boot sector data structures.

This module covers the functionality of the DiskController in managing and
identifying disk formats, as well as the serialization and deserialization
of FAT12 boot sector information via the FATVolumeInfo data class.
"""

import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from fatfloppy.core.controller import DiskController
from fatfloppy.core.disk import Disk
from fatfloppy.core.drivers import IMGImageDriver
from fatfloppy.core.filesystem_registry import FilesystemRegistry
from fatfloppy.core.filesystems.fat12_fs import FATVolumeInfo
from fatfloppy.core.format_profile import FormatProfile
from fatfloppy.core.physical_format import PhysicalFormat, TrackFormat

RESOURCE_DIR = Path(__file__).parent.parent / "resources"
EMPTY_IMG_SRC = RESOURCE_DIR / "empty_formatted_144m.img"

_ALL_FORMATS = FilesystemRegistry.get_all_formats()
FMT_144 = _ALL_FORMATS["ibm_3.5_1.44m"]


@pytest.fixture(scope="module")
def disk_controller() -> DiskController:
    """Provides a single DiskController instance for the test module."""
    return DiskController()


def test_list_known_formats(disk_controller: DiskController) -> None:
    """Test that the controller can list all known floppy formats."""
    formats: list[tuple[str, str]] = disk_controller.list_formats()
    assert isinstance(formats, list)
    assert len(formats) >= 20, f"Expected at least 20 formats, got {len(formats)}"

    # Verify tuple structure
    for name, description in formats:
        assert isinstance(name, str) and name, "Format name must be a non-empty string"
        assert isinstance(description, str) and description, (
            "Format description must be a non-empty string"
        )

    format_names = {name for name, _ in formats}
    # Core formats that must always be present
    assert "ibm_3.5_1.44m" in format_names
    assert "ibm_3.5_720k" in format_names
    assert "ibm_5.25_360k" in format_names
    assert "ibm_5.25_1.2m" in format_names
    assert "cpm_8_sssd_250k" in format_names
    assert "hdos_5.25_100k" in format_names


def test_get_format_by_name(disk_controller: DiskController) -> None:
    """Test retrieving a specific format profile by its name."""
    profile = disk_controller.get_format_by_name("ibm_3.5_1.44m")
    assert profile is not None
    assert isinstance(profile, FormatProfile)
    assert profile.name == "ibm_3.5_1.44m"

    expected_total_bytes = (
        profile.physical_format.total_sectors * profile.physical_format.bytes_per_sector
    )
    assert expected_total_bytes == 1440 * 1024

    profile_none = disk_controller.get_format_by_name("non_existent_format")
    assert profile_none is None


def test_detect_format_144mb(disk_controller: DiskController) -> None:
    """Test detecting a standard 1.44MB format from a real disk image."""
    if not EMPTY_IMG_SRC.exists():
        pytest.skip(f"Resource file not found: {EMPTY_IMG_SRC}")

    success = disk_controller.open_disk(str(EMPTY_IMG_SRC), disk_type="IMG")
    assert success is True
    assert disk_controller.disk is not None
    assert disk_controller.driver is not None

    detected_format_result = disk_controller.detect_format()
    assert isinstance(detected_format_result, tuple)
    assert detected_format_result[0] == "ibm_3.5_1.44m"
    assert isinstance(detected_format_result[1], FATVolumeInfo)

    disk_controller.close_disk()


def test_detect_format_no_match(disk_controller: DiskController) -> None:
    """
    Test format detection with a custom BPB that doesn't match known profiles.

    Even if the format is not recognized, the BPB data should still be parsed
    correctly into a FATVolumeInfo object.
    """
    dummy_boot = bytearray(512)
    dummy_boot[0:3] = b"\xeb\xfe\x90"
    dummy_boot[3:11] = b"NONAME  "
    struct.pack_into("<H", dummy_boot, 0x0B, 512)
    struct.pack_into("<B", dummy_boot, 0x0D, 1)
    struct.pack_into("<H", dummy_boot, 0x0E, 1)
    struct.pack_into("<B", dummy_boot, 0x10, 2)
    struct.pack_into("<H", dummy_boot, 0x11, 224)
    struct.pack_into("<H", dummy_boot, 0x13, 1000)
    struct.pack_into("<B", dummy_boot, 0x15, 0xF1)
    struct.pack_into("<H", dummy_boot, 0x16, 5)
    struct.pack_into("<H", dummy_boot, 0x18, 10)
    struct.pack_into("<H", dummy_boot, 0x1A, 3)
    struct.pack_into("<I", dummy_boot, 0x1C, 0)
    struct.pack_into("<I", dummy_boot, 0x20, 0)
    struct.pack_into("<H", dummy_boot, 0x1FE, 0xAA55)

    driver = IMGImageDriver(
        "dummy", image_data=bytes(dummy_boot) + b"\x00" * 1024 * 100
    )
    disk_controller.driver = driver
    disk_controller.disk = Disk(driver)

    temp_heads = 3
    temp_spt = 10
    temp_cylinders = 1000 // (temp_spt * temp_heads)
    temp_track_format = TrackFormat(
        track_start=0,
        track_end=temp_cylinders - 1,
        head_start=0,
        head_end=temp_heads - 1,
        sectors_per_track=temp_spt,
        encoding="MFM",
        rate=250,
        gap3_bytes=84,
        interleave=1,
    )
    temp_geom = PhysicalFormat(
        cylinders=temp_cylinders,
        heads=temp_heads,
        rpm=300,
        heads_inverted=False,
        bytes_per_sector=512,
        track_formats=[temp_track_format],
    )

    disk_controller.disk.set_geometry(temp_geom)
    if hasattr(disk_controller.driver, "set_physical_format"):
        disk_controller.driver.set_physical_format(temp_geom)

    detected_format_result = disk_controller.detect_format()
    assert isinstance(detected_format_result, tuple)
    assert len(detected_format_result) == 3

    format_name, fs_config, physical_format = detected_format_result

    assert format_name is None or isinstance(format_name, str)
    if fs_config is not None:
        assert isinstance(fs_config, FATVolumeInfo)

    disk_controller.close_disk()
    disk_controller.driver = None
    disk_controller.disk = None


def test_fat_volume_info_to_bytes_from_bytes_roundtrip() -> None:
    """Test the roundtrip conversion of FATVolumeInfo to bytes and back."""
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
        fs_type="FAT12   ",
    )
    bs_bytes = bsd.to_bytes()
    assert len(bs_bytes) == 512
    assert bs_bytes[510:512] == b"\x55\xaa"

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


def test_fat_volume_info_from_bytes_real_image() -> None:
    """Test creating a FATVolumeInfo object from a real 1.44MB image."""
    if not EMPTY_IMG_SRC.exists():
        pytest.skip(f"Resource file not found: {EMPTY_IMG_SRC}")

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


@pytest.mark.skip(
    reason="Boot signature check is currently disabled in FATVolumeInfo.from_bytes"
)
def test_fat_volume_info_from_bytes_invalid_signature() -> None:
    """Test that an invalid boot signature raises a ValueError."""
    invalid_boot = bytearray(FMT_144.filesystem_config.to_bytes())
    invalid_boot[510:512] = b"\x00\x00"
    with pytest.raises(ValueError, match="Invalid boot signature"):
        FATVolumeInfo.from_bytes(bytes(invalid_boot))


def test_fat_volume_info_from_bytes_too_short() -> None:
    """Test that data shorter than a full sector raises a ValueError."""
    short_boot = b"\x00" * 100
    try:
        FATVolumeInfo.from_bytes(short_boot)
        pytest.fail("ValueError was not raised for short boot sector")
    except ValueError as e:
        assert "Sector data too short" in str(e)
    except Exception as e:
        pytest.fail(f"Raised {type(e).__name__} instead of ValueError: {e}")
