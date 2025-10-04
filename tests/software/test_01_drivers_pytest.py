# tests/software/test_01_drivers_pytest.py
"""Tests for the IMGImageDriver class."""

import copy
import shutil
import sys
from pathlib import Path
from typing import Generator, Tuple
from unittest.mock import mock_open, patch

import pytest

# Add the source directory to the Python path for local imports.
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from fatfloppy.core.disk import Disk
from fatfloppy.core.drivers import IMGImageDriver
from fatfloppy.core.filesystem_registry import FilesystemRegistry
from fatfloppy.core.physical_format import PhysicalFormat

# --- Constants and Type Aliases ---

RESOURCE_DIR = Path(__file__).parent.parent / "resources"
EMPTY_IMG_SRC = RESOURCE_DIR / "empty_formatted_144m.img"

# Get format profiles from registry
_ALL_FORMATS = FilesystemRegistry.get_all_formats()
FMT_144 = _ALL_FORMATS["ibm_3.5_1.44m"]
FMT_720 = _ALL_FORMATS["ibm_3.5_720k"]

# Type alias for the fixture's yielded tuple for cleaner type hinting.
DriverSetupFixture = Tuple[IMGImageDriver, Path, int, PhysicalFormat]


# --- Fixtures ---

@pytest.fixture(scope="function")
def driver_setup(tmp_path: Path) -> Generator[DriverSetupFixture, None, None]:
    """
    Set up an IMGImageDriver instance for testing.

    This fixture copies a pre-formatted 1.44MB disk image to a temporary
    location, initializes an IMGImageDriver with it, and sets its physical
    format. It yields the driver, image path, bytes per sector, and the
    physical format object.

    Args:
        tmp_path: The pytest temporary path fixture.

    Yields:
        A tuple containing:
            - The initialized IMGImageDriver instance.
            - The path to the temporary disk image.
            - The bytes per sector for the format.
            - The PhysicalFormat object.
    """
    test_img_path = tmp_path / "test_driver_1.44mb.img"
    test_format_profile = FMT_144
    test_format = test_format_profile.physical_format
    bytes_per_sector = test_format.bytes_per_sector

    if not EMPTY_IMG_SRC.exists():
        pytest.skip(f"Resource file not found: {EMPTY_IMG_SRC}")

    shutil.copy(EMPTY_IMG_SRC, test_img_path)
    driver = IMGImageDriver(str(test_img_path))
    driver.set_physical_format(test_format)

    yield driver, test_img_path, bytes_per_sector, test_format

    print(f"\n[Fixture Teardown] Driver test image {test_img_path} cleanup.")


# --- Test Cases ---

def test_01_initialization_from_file(driver_setup: DriverSetupFixture) -> None:
    """Test that the driver initializes correctly from a file path."""
    driver, test_img_path, _, physical_format = driver_setup
    assert test_img_path.exists()
    assert len(driver.image_data) > 0

    expected_total_bytes = (
        physical_format.total_sectors * physical_format.bytes_per_sector
    )
    assert len(driver.image_data) == expected_total_bytes


def test_02_initialization_from_bytes() -> None:
    """Test that the driver initializes correctly from a bytes object."""
    initial_data = b"\xAA" * 512 * 10
    driver_bytes = IMGImageDriver("dummy_path_not_used.img", image_data=initial_data)
    driver_bytes.set_physical_format(FMT_144.physical_format)
    assert driver_bytes.image_data == bytearray(initial_data)
    assert driver_bytes.dirty is True


def test_03_set_physical_format(driver_setup: DriverSetupFixture) -> None:
    """Test the ability to set and change the physical format of the driver."""
    driver, _, _, _ = driver_setup
    fmt_720_phys = FMT_720.physical_format
    driver.set_physical_format(fmt_720_phys)

    assert driver.physical_format is not None
    assert (
        driver.physical_format.get_sectors_per_track(0, 0)
        == fmt_720_phys.get_sectors_per_track(0, 0)
    )
    assert driver.physical_format.heads == fmt_720_phys.heads
    assert driver.physical_format.bytes_per_sector == fmt_720_phys.bytes_per_sector


def test_04_read_sector(driver_setup: DriverSetupFixture) -> None:
    """Test reading a single sector from the disk image."""
    driver, _, bytes_per_sector, _ = driver_setup
    boot_sector = driver.read_sector(0, 0, 1)
    assert len(boot_sector) == bytes_per_sector
    assert boot_sector[510:512] == b"\x55\xAA"


def test_05_write_sector_and_flush(driver_setup: DriverSetupFixture) -> None:
    """Test writing data to a sector and flushing changes to the file."""
    driver, test_img_path, bytes_per_sector, physical_format = driver_setup
    test_data = b"TEST" + b"\xEE" * (bytes_per_sector - 4)
    cyl, head, sect = 5, 1, 3

    driver.write_sector(cyl, head, sect, test_data)
    assert driver.dirty is True

    offset = physical_format.chs_to_lba(cyl, head, sect) * bytes_per_sector
    assert driver.image_data[offset : offset + len(test_data)] == test_data

    driver.flush()
    assert driver.dirty is False

    # Verify by reading back in a new driver instance
    driver2 = IMGImageDriver(str(test_img_path))
    driver2.set_physical_format(physical_format)
    read_data = driver2.read_sector(cyl, head, sect)
    assert read_data == test_data


def test_06_read_beyond_image_size(driver_setup: DriverSetupFixture) -> None:
    """Test that reading beyond the defined geometry raises an error."""
    driver, _, bytes_per_sector, physical_format = driver_setup
    invalid_cyl = physical_format.cylinders
    invalid_head, invalid_sect = 0, 1

    # The Disk object performs validation before calling the driver
    disk = Disk(driver)
    disk.set_geometry(physical_format)
    with pytest.raises(
        ValueError, match=f"Invalid CHS: {invalid_cyl}, {invalid_head}, {invalid_sect}"
    ):
        disk.read_sector(invalid_cyl, invalid_head, invalid_sect)

    # Directly reading the last valid sector from the driver should succeed
    last_valid_c = physical_format.cylinders - 1
    last_valid_h = physical_format.heads - 1
    last_valid_s = physical_format.get_sectors_per_track(last_valid_c, last_valid_h)
    last_sector_data = driver.read_sector(last_valid_c, last_valid_h, last_valid_s)
    assert len(last_sector_data) == bytes_per_sector


def test_07_write_within_bounds(driver_setup: DriverSetupFixture) -> None:
    """Test writing to the last sector and handling out-of-bounds writes."""
    driver, test_img_path, bytes_per_sector, physical_format = driver_setup
    test_data = b"LAST" * (bytes_per_sector // 4)
    assert len(test_data) == bytes_per_sector

    # Write to the very last valid sector
    last_cyl = physical_format.cylinders - 1
    last_head = physical_format.heads - 1
    last_sect = physical_format.get_sectors_per_track(last_cyl, last_head)
    driver.write_sector(last_cyl, last_head, last_sect, test_data)
    driver.flush()

    # Verify the write
    driver2 = IMGImageDriver(str(test_img_path))
    driver2.set_physical_format(physical_format)
    read_data = driver2.read_sector(last_cyl, last_head, last_sect)
    assert read_data == test_data

    # Test exceptions for out-of-bounds writes
    invalid_cyl = physical_format.cylinders
    with pytest.raises(ValueError, match=f"No TrackFormat for cylinder {invalid_cyl}, head 0"):
        driver.write_sector(invalid_cyl, 0, 1, test_data)

    invalid_head = physical_format.heads
    with pytest.raises(ValueError, match=f"No TrackFormat for cylinder 0, head {invalid_head}"):
        driver.write_sector(0, invalid_head, 1, test_data)

    max_spt = physical_format.get_sectors_per_track(0, 0)
    invalid_sect = max_spt + 1
    with pytest.raises(ValueError, match=f"Invalid sector access: Sector {invalid_sect} out of range"):
        driver.write_sector(0, 0, invalid_sect, test_data)


def test_08_img_write_invalid_sector_size(driver_setup: DriverSetupFixture) -> None:
    """Test that writing with an invalid sector size (0) raises an error."""
    driver, _, _, original_format = driver_setup
    
    # Create a modified format with an invalid bytes_per_sector value
    invalid_track_formats = copy.deepcopy(original_format.track_formats)
    for tf in invalid_track_formats:
        tf.bytes_per_sector = 0

    invalid_geom = PhysicalFormat(
        cylinders=original_format.cylinders,
        heads=original_format.heads,
        rpm=original_format.rpm,
        heads_inverted=original_format.heads_inverted,
        bytes_per_sector=0,  # Make consistent with track formats
        track_formats=invalid_track_formats,
    )
    driver.set_physical_format(invalid_geom)

    # Writing with a sector size of 0 should raise a ValueError.
    # We pass empty bytes to satisfy the data size check (len(b'') == 0).
    with pytest.raises(ValueError, match="Invalid sector size: 0"):
        driver.write_sector(0, 0, 1, b"")

    # Restore original format to ensure fixture cleanup is not affected
    driver.set_physical_format(original_format)


def test_09_img_flush_io_error(driver_setup: DriverSetupFixture) -> None:
    """Test that an IOError during flush is handled correctly."""
    driver, _, bytes_per_sector, _ = driver_setup

    # Make the driver's data dirty to enable flushing
    driver.write_sector(0, 0, 1, b"\xAA" * bytes_per_sector)
    assert driver.dirty

    # Mock 'open' to raise an IOError upon trying to write to the file
    with patch("builtins.open", mock_open()) as mocked_file:
        mocked_file.side_effect = IOError("Permission denied")
        with pytest.raises(IOError, match="Flush failed: Permission denied"):
            driver.flush()

    # The dirty flag should remain set after a failed flush
    assert driver.dirty


def test_10_img_init_read_error(tmp_path: Path) -> None:
    """Test that an IOError during file read on init is handled correctly."""
    test_img_path = tmp_path / "unreadable.img"
    test_img_path.touch()  # Create an empty file

    # Mock 'open' to raise an IOError when the driver tries to read the file
    with patch("builtins.open", mock_open()) as mocked_file:
        mocked_file.side_effect = IOError("Cannot read file")
        with pytest.raises(IOError, match="Cannot read file"):
            IMGImageDriver(str(test_img_path))
