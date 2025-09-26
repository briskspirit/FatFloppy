# tests/test_05_controller_physical_mocked_pytest.py
"""
Tests for the DiskController with a mocked physical Greaseweazle drive.

This test suite uses pytest and the unittest.mock library to simulate the
behavior of a physical floppy disk drive connected via a Greaseweazle USB
adapter. It allows for testing of the DiskController's interaction with the
GreaseweazleDriver without requiring actual hardware.

The tests cover various scenarios including:
- Opening a disk with auto-detection of the format.
- Opening a disk with an explicitly specified format.
- Reading and writing individual sectors.
- Handling read/write errors and cache invalidation.
- Verifying written data.
- Edge cases and error handling in the driver.
"""
import copy
import sys
import types
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Tuple
from unittest.mock import MagicMock, call, patch

import pytest

# Assuming greaseweazle might raise specific errors, e.g., USBError
try:
    from greaseweazle.usb import USBError
except ImportError:
    USBError = IOError  # Fallback for environments without greaseweazle installed

# Add the source directory to the Python path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from fatfloppy.core.controller import DiskController
from fatfloppy.core.disk import Disk
from fatfloppy.core.drivers import GreaseweazleDriver
from fatfloppy.core.filesystems.fat12fs import FATFilesystem, FATVolumeInfo
from fatfloppy.core.format_definitions import FLOPPY_FORMATS
from fatfloppy.core.format_profile import FormatProfile
from greaseweazle.codec import codec
from greaseweazle.codec.ibm import ibm

# --- Constants for floppy formats ---
FMT_144 = FLOPPY_FORMATS["ibm_3.5_1.44m"]
FMT_360 = FLOPPY_FORMATS["ibm_5.25_360k"]
FMT_720 = FLOPPY_FORMATS["ibm_3.5_720k"]


# --- Helper Functions ---

def create_mock_flux(ticks: int = 20000000) -> MagicMock:
    """
    Creates a mock flux object simulating raw track data from a Greaseweazle.

    Args:
        ticks: The number of ticks per revolution to simulate.

    Returns:
        A MagicMock object configured to resemble a greaseweazle.Flux object.
    """
    mock = MagicMock()
    mock.index_list = [0, ticks, ticks * 2]
    mock.ticks_per_rev = ticks
    mock.list = [ticks // 2] * 2
    mock.ticks_to_index = ticks
    return mock


def create_mock_track_data(
    cyl: int, head: int, fmt: Optional[FormatProfile], sectors_present: Optional[List[int]] = None
) -> MagicMock:
    """
    Creates a mock track data object containing simulated sector data.

    Args:
        cyl: The cylinder number of the track.
        head: The head number of the track.
        fmt: The FormatProfile defining the track's geometry.
        sectors_present: A list of sector numbers that should exist on the track.
                         If None, a full track is created.

    Returns:
        A MagicMock object configured to resemble a greaseweazle track data object.
    """
    mock_track = MagicMock()
    bps = 512
    spt = 18

    if fmt and fmt.physical_format:
        bps = fmt.physical_format.bytes_per_sector
        try:
            tf = fmt.physical_format.get_track_format(cyl, head)
            spt = tf.sectors_per_track
        except ValueError:
            pass  # Keep default if track format is not found

    if sectors_present is None:
        sectors_present = list(range(1, spt + 1))

    mock_sectors = []
    for s_num in sectors_present:
        mock_sector = MagicMock()
        mock_sector.idam = MagicMock()
        mock_sector.idam.r = s_num
        mock_sector.idam.crc = 0
        mock_sector.dam = MagicMock()
        mock_sector.dam.data = bytearray(b"\x00" * bps)
        mock_sector.dam.crc = 0
        mock_sector.crc = 0
        mock_sectors.append(mock_sector)

    mock_dat = MagicMock()
    mock_dat.sectors = mock_sectors
    mock_dat.track = MagicMock()
    mock_dat.track.mode = "IBM MFM"
    mock_dat.track.clock = 1.0 / (500 * 2000)  # Standard 500kbps clock rate
    return mock_dat


# --- Fixtures ---

@pytest.fixture(scope="function")
def mocked_controller(request: pytest.FixtureRequest) -> Generator[Tuple[DiskController, Dict[str, MagicMock]], None, None]:
    """
    Pytest fixture to provide a DiskController with a mocked Greaseweazle backend.

    This fixture patches all necessary Greaseweazle and USB functions to simulate
    a physical drive. It yields the controller instance and a dictionary of
    the mocks for further customization within tests.

    Args:
        request: The pytest request object, used for logging.

    Yields:
        A tuple containing:
        - An instance of DiskController.
        - A dictionary of the created mock objects for inspection and modification.
    """
    print(f"\n--- [Fixture Setup] Mocking GW Controller for test: {request.node.name} ---")
    mock_custom_diskdef_instance = MagicMock(spec=codec.DiskDef)
    mock_custom_diskdef_instance.name = "fixture_mock_diskdef"

    mock_ibm_track = MagicMock(spec=ibm.IBMTrack)
    mock_ibm_track.sectors = [
        MagicMock(idam=MagicMock(r=i, crc=0), dam=MagicMock(data=bytearray(512), crc=0), crc=0) for i in range(1, 19)
    ]
    mock_ibm_track.master_track.return_value.flux_for_writeout.return_value = create_mock_flux()

    mock_track_def = MagicMock(spec=ibm.IBMTrack_FixedDef)
    mock_track_def.mk_track.return_value = mock_ibm_track
    mock_custom_diskdef_instance.track_map = MagicMock()
    mock_custom_diskdef_instance.track_map.get.return_value = mock_track_def

    mock_get_diskdef_instance = MagicMock(spec=codec.DiskDef)
    mock_get_diskdef_instance.name = "mock_get_diskdef_name"
    mock_get_diskdef_instance.track_map = MagicMock()
    mock_get_diskdef_instance.track_map.get.return_value = mock_track_def

    patch_paths = {
        "create_diskdef": "fatfloppy.core.drivers.greaseweazle.create_greaseweazle_diskdef",
        "usb_open": "greaseweazle.tools.util.usb_open",
        "drive": "greaseweazle.tools.util.Drive",
        "with_drive": "greaseweazle.tools.util.with_drive_selected",
        "get_diskdef": "greaseweazle.codec.codec.get_diskdef",
        "read_retry": "fatfloppy.core.drivers.greaseweazle.read.read_with_retry",
    }

    with patch(patch_paths["create_diskdef"], return_value=mock_custom_diskdef_instance) as mock_create_gw_diskdef, \
         patch(patch_paths["usb_open"]) as mock_usb_open, \
         patch(patch_paths["drive"]) as mock_drive, \
         patch(patch_paths["with_drive"]) as mock_with_drive_selected, \
         patch(patch_paths["get_diskdef"], return_value=mock_get_diskdef_instance) as mock_get_diskdef, \
         patch(patch_paths["read_retry"]) as mock_read_with_retry:

        # Configure mock USB device
        mock_usb = MagicMock()
        mock_usb.sample_freq = 96000000.0
        mock_usb.read_track.return_value = create_mock_flux()
        mock_usb.write_track = MagicMock()
        mock_usb.seek.return_value = None
        mock_usb.update_track = MagicMock()
        mock_usb_open.return_value = mock_usb

        # Configure mock Drive context
        mock_drive_instance = MagicMock()
        mock_drive.return_value = mock_drive_instance
        mock_with_drive_selected.side_effect = lambda func, *args, **kwargs: func()
        mock_read_with_retry.return_value = (create_mock_flux(), create_mock_track_data(0, 0, FMT_144))

        controller = DiskController()
        mocks_bundle = {
            "mock_usb_open": mock_usb_open,
            "mock_usb": mock_usb,
            "mock_drive": mock_drive,
            "mock_drive_instance": mock_drive_instance,
            "mock_with_drive_selected": mock_with_drive_selected,
            "mock_get_diskdef": mock_get_diskdef,
            "mock_read_with_retry": mock_read_with_retry,
            "mock_create_gw_diskdef": mock_create_gw_diskdef,
            "mock_custom_diskdef_instance": mock_custom_diskdef_instance,
        }

        yield controller, mocks_bundle

    print(f"--- [Fixture Teardown] Mock GW Controller test: {request.node.name} ---")


def open_disk_for_rw_tests(
    controller: DiskController, mocks_bundle: Dict[str, MagicMock], test_format: FormatProfile = FMT_144
) -> None:
    """
    Helper function to open a disk in the controller for read/write tests.

    Sets up the necessary mocks and calls controller.open_disk to prepare the
    controller with a simulated disk of the specified format.

    Args:
        controller: The DiskController instance to use.
        mocks_bundle: The dictionary of mocks from the fixture.
        test_format: The FormatProfile to simulate.
    """
    # Arrange
    mock_usb = mocks_bundle["mock_usb"]
    mock_with_drive_selected = mocks_bundle["mock_with_drive_selected"]
    mock_custom_diskdef_instance = mocks_bundle["mock_custom_diskdef_instance"]

    phys_fmt = test_format.physical_format
    track_fmt = phys_fmt.track_formats[0]
    format_info_dict = {
        "format_name": test_format.name,
        "cylinders": phys_fmt.cylinders,
        "heads": phys_fmt.heads,
        "sectors_per_track": track_fmt.sectors_per_track,
        "bytes_per_sector": phys_fmt.bytes_per_sector,
        "encoding": track_fmt.encoding,
        "rate": track_fmt.rate,
        "rpm": phys_fmt.rpm,
        "gap3_bytes": track_fmt.gap3_bytes,
        "interleave": track_fmt.interleave,
    }

    mock_fs = MagicMock(spec=FATFilesystem)
    mock_fs.get_validity_score.return_value = 100
    mock_fat_bs = MagicMock(spec=FATVolumeInfo)
    bsd = test_format.filesystem_config or FATVolumeInfo()
    for attr, value in bsd.__dict__.items():
        if attr not in ["logger"]:
            setattr(mock_fat_bs, attr, value)

    mock_fat_bs.is_valid.return_value = True
    mock_fat_bs.sectors_per_track = track_fmt.sectors_per_track
    mock_fat_bs.num_heads = phys_fmt.heads
    mock_fat_bs.total_sectors = phys_fmt.total_sectors
    mock_fs.boot_sector = mock_fat_bs
    mock_fs.get_specific_config.return_value = mock_fat_bs
    mock_fs.get_allocated_units.return_value = []
    mock_fs.get_free_space.return_value = (phys_fmt.total_bytes, phys_fmt.total_bytes)

    mock_usb.read_track.return_value = create_mock_flux()
    mock_boot_sector_bytes = bsd.to_bytes()

    # Act
    with patch("fatfloppy.core.disk.Disk.read_sector", return_value=mock_boot_sector_bytes), \
         patch("fatfloppy.core.controller.create_filesystem", return_value=mock_fs):
        mock_with_drive_selected.side_effect = lambda func, *args, **kwargs: func()
        success = controller.open_disk(None, "physical", "A", "3.5", format_info=format_info_dict)

    # Assert
    assert success is True, f"open_disk failed in helper for format {test_format.name}"
    assert controller.disk is not None
    assert controller.driver is not None
    assert controller.driver.initialized is True
    assert controller.driver.fmt_cls is mock_custom_diskdef_instance
    assert hasattr(controller.driver.fmt_cls, "name"), "Mock fmt_cls missing name after setup"
    assert controller.driver.fmt_cls.name == "fixture_mock_diskdef"
    assert controller.filesystem == mock_fs


# --- Tests ---

def test_01_open_physical_drive_A_35_auto_detect_mocked(
    mocked_controller: Tuple[DiskController, Dict[str, MagicMock]]
) -> None:
    """
    Tests opening a 3.5" physical drive A with format auto-detection.
    """
    # Arrange
    controller, mocks_bundle = mocked_controller
    mock_usb = mocks_bundle["mock_usb"]
    mock_usb_open = mocks_bundle["mock_usb_open"]
    mock_drive = mocks_bundle["mock_drive"]
    mock_read_with_retry = mocks_bundle["mock_read_with_retry"]
    mock_with_drive_selected = mocks_bundle["mock_with_drive_selected"]

    drive_letter = "A"
    drive_size = "3.5"
    expected_format = FMT_144

    mock_usb.read_track.return_value = create_mock_flux()
    measure_rpm_called = MagicMock()

    def with_drive_side_effect(func, *args, **kwargs):
        if func.__name__ == "measure_rpm":
            measure_rpm_called()
        func()

    mock_with_drive_selected.side_effect = with_drive_side_effect

    def mock_read_retry_auto_detect(usb, args, track_iter):
        cyl, head = 0, 0
        try:
            track_info = str(args.tracks)
            c_part = track_info.split(":")[0]
            h_part = track_info.split(":")[1]
            cyl = int(c_part.split("=")[1])
            head = int(h_part.split("=")[1])
        except (IndexError, ValueError, AttributeError):
            pass
        return (create_mock_flux(), create_mock_track_data(cyl, head, expected_format))

    mock_read_with_retry.side_effect = mock_read_retry_auto_detect

    mock_fs = MagicMock(spec=FATFilesystem)
    mock_fs.get_validity_score.return_value = 100
    bsd = expected_format.filesystem_config
    mock_fat_bs = MagicMock(spec=FATVolumeInfo)
    for attr, value in bsd.__dict__.items():
        if attr != "logger":
            setattr(mock_fat_bs, attr, value)
    mock_fat_bs.is_valid.return_value = True
    mock_fat_bs.sectors_per_track = expected_format.physical_format.track_formats[0].sectors_per_track
    mock_fat_bs.num_heads = expected_format.physical_format.heads
    mock_fat_bs.total_sectors = expected_format.physical_format.total_sectors
    mock_fs.boot_sector = mock_fat_bs
    mock_fs.get_specific_config.return_value = mock_fat_bs
    mock_boot_sector_bytes = bsd.to_bytes()

    # Act
    with patch("fatfloppy.core.disk.Disk.read_sector", return_value=mock_boot_sector_bytes), \
         patch("fatfloppy.core.controller.create_filesystem", return_value=mock_fs):
        success = controller.open_disk(source=None, disk_type="physical", drive_letter=drive_letter, drive_size=drive_size)

    # Assert
    assert success is True
    assert isinstance(controller.driver, GreaseweazleDriver)
    mock_usb_open.assert_called_with(None)
    mock_drive.assert_called()
    measure_rpm_called.assert_called_once()
    assert controller.driver.initialized is True
    assert controller.disk is not None
    assert controller.disk.physical_format is not None
    geom = controller.disk.physical_format
    assert geom.get_sectors_per_track(0, 0) == mock_fat_bs.sectors_per_track
    assert geom.heads == mock_fat_bs.num_heads
    assert geom.cylinders == expected_format.physical_format.cylinders
    assert controller.filesystem == mock_fs
    pf = controller.driver.physical_format
    assert pf is not None
    assert pf.get_sectors_per_track(0, 0) == expected_format.physical_format.get_sectors_per_track(0, 0)
    assert pf.heads == expected_format.physical_format.heads


def test_02_open_physical_with_explicit_format_mocked(
    mocked_controller: Tuple[DiskController, Dict[str, MagicMock]]
) -> None:
    """
    Tests opening a physical drive with an explicitly provided format.
    """
    # Arrange
    controller, mocks_bundle = mocked_controller
    mock_usb = mocks_bundle["mock_usb"]
    mock_with_drive_selected = mocks_bundle["mock_with_drive_selected"]
    mock_custom_diskdef_instance = mocks_bundle["mock_custom_diskdef_instance"]
    expected_format = FMT_144

    phys_fmt = expected_format.physical_format
    track_fmt = phys_fmt.track_formats[0]
    format_info = {
        "format_name": expected_format.name,
        "cylinders": phys_fmt.cylinders,
        "heads": phys_fmt.heads,
        "sectors_per_track": track_fmt.sectors_per_track,
        "bytes_per_sector": phys_fmt.bytes_per_sector,
        "encoding": track_fmt.encoding,
        "rate": track_fmt.rate,
        "rpm": phys_fmt.rpm,
        "gap3_bytes": track_fmt.gap3_bytes,
        "interleave": track_fmt.interleave,
    }

    mock_fs = MagicMock(spec=FATFilesystem)
    mock_fs.get_validity_score.return_value = 100
    mock_fat_bs = MagicMock(spec=FATVolumeInfo)
    bsd = expected_format.filesystem_config
    for attr, value in bsd.__dict__.items():
        if attr != "logger":
            setattr(mock_fat_bs, attr, value)
    mock_fat_bs.is_valid.return_value = True
    mock_fat_bs.sectors_per_track = track_fmt.sectors_per_track
    mock_fat_bs.num_heads = phys_fmt.heads
    mock_fat_bs.total_sectors = phys_fmt.total_sectors
    mock_fs.boot_sector = mock_fat_bs

    mock_usb.read_track.return_value = create_mock_flux()

    # Act
    with patch("fatfloppy.core.controller.create_filesystem", return_value=mock_fs):
        mock_with_drive_selected.side_effect = lambda func, *args, **kwargs: func()
        success = controller.open_disk(None, "physical", "A", "3.5", format_info)

    # Assert
    assert success
    assert controller.filesystem == mock_fs
    assert controller.disk.physical_format is not None
    geom = controller.disk.physical_format
    assert geom.get_sectors_per_track(0, 0) == mock_fat_bs.sectors_per_track
    assert geom.get_sectors_per_track(0, 0) == expected_format.physical_format.get_sectors_per_track(0, 0)
    assert geom.heads == mock_fat_bs.num_heads
    assert controller.driver.physical_format is not None
    assert controller.driver.physical_format.get_sectors_per_track(0, 0) == expected_format.physical_format.get_sectors_per_track(0, 0)
    assert controller.driver.fmt_cls is mock_custom_diskdef_instance


def test_03_physical_read_sector_success_mocked(
    mocked_controller: Tuple[DiskController, Dict[str, MagicMock]]
) -> None:
    """
    Tests successful reading of a single sector from the physical drive.
    """
    # Arrange
    controller, mocks_bundle = mocked_controller
    mock_read_with_retry = mocks_bundle["mock_read_with_retry"]
    test_format = FMT_144
    open_disk_for_rw_tests(controller, mocks_bundle, test_format=test_format)
    bps = test_format.physical_format.bytes_per_sector
    mock_track_data = create_mock_track_data(0, 0, test_format)
    mock_read_with_retry.return_value = (create_mock_flux(), mock_track_data)

    # Act
    data = controller.disk.read_sector(0, 0, 1)

    # Assert
    assert data == b"\x00" * bps


def test_04_physical_read_sector_not_found_mocked(
    mocked_controller: Tuple[DiskController, Dict[str, MagicMock]]
) -> None:
    """
    Tests reading a sector that is not found on the track, expecting zeroed data.
    """
    # Arrange
    controller, mocks_bundle = mocked_controller
    mock_read_with_retry = mocks_bundle["mock_read_with_retry"]
    test_format = FMT_144
    open_disk_for_rw_tests(controller, mocks_bundle, test_format=test_format)
    bps = test_format.physical_format.bytes_per_sector

    # Create a mock track data object with no sectors
    mock_dat_empty = MagicMock()
    mock_dat_empty.sectors = []
    mock_dat_empty.track = MagicMock()
    mock_dat_empty.track.mode = "IBM MFM"
    mock_dat_empty.track.clock = 1.0 / (500 * 2000)
    mock_read_with_retry.return_value = (create_mock_flux(), mock_dat_empty)

    # Act
    data = controller.disk.read_sector(0, 0, 1)

    # Assert
    assert data == b"\x00" * bps


def test_06_physical_write_sector_and_flush_success_mocked(
    mocked_controller: Tuple[DiskController, Dict[str, MagicMock]]
) -> None:
    """
    Tests that writing a sector and flushing caches results in a track write call.
    """
    # Arrange
    controller, mocks_bundle = mocked_controller
    mock_usb = mocks_bundle["mock_usb"]
    mock_read_with_retry = mocks_bundle["mock_read_with_retry"]
    test_format = FMT_144
    open_disk_for_rw_tests(controller, mocks_bundle, test_format=test_format)
    bps = test_format.physical_format.bytes_per_sector
    data = bytearray(bps)
    # Simulate a partial track read first
    mock_track_data = create_mock_track_data(0, 0, test_format, sectors_present=list(range(2, 19)))
    mock_read_with_retry.return_value = (create_mock_flux(), mock_track_data)

    # Act
    controller.disk.write_sector(0, 0, 1, data)
    controller.driver.flush()

    # Assert
    mock_usb.write_track.assert_called()


def test_07_physical_flush_write_error_mocked(
    mocked_controller: Tuple[DiskController, Dict[str, MagicMock]]
) -> None:
    """
    Tests that a USBError during a flush is caught and handled gracefully.
    """
    # Arrange
    controller, mocks_bundle = mocked_controller
    mock_usb = mocks_bundle["mock_usb"]
    mock_read_with_retry = mocks_bundle["mock_read_with_retry"]
    test_format = FMT_144
    open_disk_for_rw_tests(controller, mocks_bundle, test_format=test_format)
    bps = test_format.physical_format.bytes_per_sector
    # Simulate reading a partial track
    mock_track_data = create_mock_track_data(0, 0, test_format, sectors_present=list(range(2, 19)))
    mock_read_with_retry.return_value = (create_mock_flux(), mock_track_data)
    mock_usb.write_track.side_effect = USBError("Simulated Write error")

    # Act
    controller.disk.write_sector(0, 0, 1, bytearray(bps))
    controller.driver.flush()  # The error should be caught within flush

    # Assert
    mock_usb.write_track.assert_called_once()


def test_09_physical_flush_partial_track_reads_first_mocked(
    mocked_controller: Tuple[DiskController, Dict[str, MagicMock]]
) -> None:
    """
    Tests that flushing a partial track write triggers a read of the existing
    track data first.
    """
    # Arrange
    controller, mocks_bundle = mocked_controller
    mock_read_with_retry = mocks_bundle["mock_read_with_retry"]
    test_format = FMT_144
    spt = test_format.physical_format.get_sectors_per_track(0, 0)
    open_disk_for_rw_tests(controller, mocks_bundle, test_format=test_format)
    bps = test_format.physical_format.bytes_per_sector

    # Simulate reading a track with sector 1 missing
    mock_track_data_partial = create_mock_track_data(0, 0, test_format, sectors_present=list(range(2, spt + 1)))
    mock_read_with_retry.return_value = (create_mock_flux(), mock_track_data_partial)

    # Act
    controller.disk.write_sector(0, 0, 1, bytearray(bps))
    mock_read_with_retry.reset_mock()
    mock_read_with_retry.return_value = (create_mock_flux(), mock_track_data_partial)
    controller.driver.flush()

    # Assert
    if spt > 1:
        found_read_call = False
        for call_args in mock_read_with_retry.call_args_list:
            args, _ = call_args
            # Check if read_with_retry was called for the correct track
            if len(args) > 1 and isinstance(args[1], types.SimpleNamespace):
                track_set_str = str(args[1].tracks)
                if track_set_str == "c=0:h=0":
                    found_read_call = True
                    break
        assert found_read_call, "read_with_retry should be called for C=0,H=0 during flush"


def test_10_gw_cache_invalidation_mocked(
    mocked_controller: Tuple[DiskController, Dict[str, MagicMock]]
) -> None:
    """
    Tests that after a write and flush, clearing the driver's cache forces a
    re-read from the (mocked) disk using the last known good format.
    """
    # Arrange
    controller, mocks_bundle = mocked_controller
    mock_read_with_retry = mocks_bundle["mock_read_with_retry"]
    mock_usb = mocks_bundle["mock_usb"]
    test_format = FMT_144
    spt = test_format.physical_format.get_sectors_per_track(0, 0)
    bps = test_format.physical_format.bytes_per_sector

    open_disk_for_rw_tests(controller, mocks_bundle, test_format=test_format)
    controller.driver.verify_writes = True
    assert controller.driver.fmt_cls is not None
    assert hasattr(controller.driver.fmt_cls, "name"), "Mock fmt_cls missing name"

    initial_track_data = create_mock_track_data(0, 0, test_format, sectors_present=list(range(2, spt + 1)))
    verify_track_data = create_mock_track_data(0, 0, test_format, sectors_present=list(range(1, spt + 1)))
    verify_track_data.sectors[0].dam.data = bytearray([0xAA] * bps)
    final_read_data = create_mock_track_data(0, 0, test_format)

    mock_read_with_retry.reset_mock()
    flush_reads_list = []

    def flush_read_side_effect(*args: Any, **kwargs: Any) -> Tuple[MagicMock, MagicMock]:
        nonlocal flush_reads_list
        call_num = len(flush_reads_list) + 1
        if call_num == 1 and spt > 1:
            flush_reads_list.append("pre-read")
            return (create_mock_flux(), initial_track_data)
        elif (call_num == 1 and spt == 1) or (call_num == 2 and spt > 1):
            flush_reads_list.append("verify-read")
            return (create_mock_flux(), verify_track_data)
        pytest.fail(f"Unexpected flush read call {call_num}")

    mock_read_with_retry.side_effect = flush_read_side_effect

    # Act 1: Write data and flush
    test_data = bytearray([0xAA] * bps)
    controller.disk.write_sector(0, 0, 1, test_data)
    controller.driver.flush()

    # Assert 1: Verify flush behavior
    mock_usb.write_track.assert_called()
    expected_flush_reads = 1 if spt == 1 else 2
    assert len(flush_reads_list) == expected_flush_reads
    assert mock_read_with_retry.call_count == expected_flush_reads

    # Act 2: Invalidate cache and re-read
    controller.driver.track_data.clear()
    controller.driver.sector_cache.clear()
    controller.driver.last_successful_format = None
    mock_read_with_retry.reset_mock()

    final_read_call_count = 0

    def final_read_mock_wrapper(cyl: int, head: int, fmt_tuple: Tuple[str, Any]) -> Optional[Dict[int, bytes]]:
        nonlocal final_read_call_count
        final_read_call_count += 1
        fmt_name_attempted = fmt_tuple[0]
        if fmt_name_attempted == "custom":
            # Simulate a successful read with the expected format
            return {1: bytes(final_read_data.sectors[0].dam.data)}
        pytest.fail(f"Unexpected format '{fmt_name_attempted}' tried during final read")
        return None

    # Act 3
    with patch.object(controller.driver, "_read_track_with_format", side_effect=final_read_mock_wrapper) as mock_final_read:
        controller.disk.read_sector(0, 0, 1)

        # Assert 3
        mock_final_read.assert_called_once_with(0, 0, ("custom", None))
        assert final_read_call_count == 1, "Mock wrapper for final read was called more than once"


def test_11_gw_write_verify_success_mocked(
    mocked_controller: Tuple[DiskController, Dict[str, MagicMock]]
) -> None:
    """
    Tests that write verification, when enabled, correctly reads back the track
    and confirms the written data.
    """
    # Arrange
    controller, mocks_bundle = mocked_controller
    mock_read_with_retry = mocks_bundle["mock_read_with_retry"]
    test_format = FMT_144
    spt = test_format.physical_format.get_sectors_per_track(0, 0)
    bps = test_format.physical_format.bytes_per_sector
    open_disk_for_rw_tests(controller, mocks_bundle, test_format=test_format)
    controller.driver.verify_writes = True

    data = bytearray([0xCC] * bps)
    mock_dat_verify = create_mock_track_data(0, 0, test_format)
    for sector_obj in mock_dat_verify.sectors:
        if sector_obj.idam.r == 1:
            sector_obj.dam.data = data
            break

    mock_read_with_retry.reset_mock()
    flush_reads_list = []

    def flush_read_side_effect(*args: Any, **kwargs: Any) -> Tuple[MagicMock, MagicMock]:
        nonlocal flush_reads_list
        call_num = len(flush_reads_list) + 1
        if call_num == 1 and spt > 1:
            flush_reads_list.append("pre-read")
            return (create_mock_flux(), create_mock_track_data(0, 0, test_format, sectors_present=list(range(2, spt + 1))))
        elif (call_num == 1 and spt == 1) or (call_num == 2 and spt > 1):
            flush_reads_list.append("verify-read")
            return (create_mock_flux(), mock_dat_verify)
        pytest.fail(f"Unexpected flush read call {call_num}")

    mock_read_with_retry.side_effect = flush_read_side_effect

    # Act
    controller.disk.write_sector(0, 0, 1, data)
    controller.driver.flush()

    # Assert
    expected_calls = 1 if spt == 1 else 2
    assert mock_read_with_retry.call_count == expected_calls, f"Expected {expected_calls} read calls, got {mock_read_with_retry.call_count}"
    assert "verify-read" in flush_reads_list


def test_12_gw_initialize_rpm_fail(
    mocked_controller: Tuple[DiskController, Dict[str, MagicMock]]
) -> None:
    """
    Tests that if RPM measurement fails during initialization, the driver
    falls back to a default value.
    """
    # Arrange
    controller, mocks_bundle = mocked_controller
    mock_usb = mocks_bundle["mock_usb"]
    mock_with_drive_selected = mocks_bundle["mock_with_drive_selected"]
    rpm_error = USBError("RPM measurement failed")

    def rpm_fail_side_effect(func, *args, **kwargs):
        if func.__name__ == "measure_rpm":
            raise rpm_error
        func()

    mock_with_drive_selected.side_effect = rpm_fail_side_effect

    # Act
    with patch("fatfloppy.core.controller.create_filesystem", return_value=None):
        format_info_dict = {"format_name": FMT_144.name}
        controller.open_disk(None, "physical", "A", "3.5", format_info=format_info_dict)

    # Assert
    assert controller.driver is not None
    assert controller.driver.initialized is True
    # Check for fallback value (300 RPM -> 0.2s/rev * sample_freq)
    assert controller.driver.drive_ticks_per_rev == 0.2 * mock_usb.sample_freq


def test_13_gw_convert_to_flux_no_format(
    mocked_controller: Tuple[DiskController, Dict[str, MagicMock]]
) -> None:
    """
    Tests that attempting to convert data to flux for writing without a format
    defined raises a ValueError.
    """
    # Arrange
    _, mocks_bundle = mocked_controller
    driver = GreaseweazleDriver()
    driver.usb = mocks_bundle["mock_usb"]
    driver.initialized = True

    assert driver.physical_format is None
    assert driver.fmt_cls is None

    # Act & Assert
    with pytest.raises(ValueError, match="No format defined for writing"):
        driver._convert_to_flux(0, 0)


def test_14_gw_convert_to_flux_no_trackdef(
    mocked_controller: Tuple[DiskController, Dict[str, MagicMock]]
) -> None:
    """
    Tests that converting to flux fails if the format has no track definition
    for the specified cylinder/head.
    """
    # Arrange
    controller, mocks_bundle = mocked_controller
    test_format = FMT_144
    open_disk_for_rw_tests(controller, mocks_bundle, test_format=test_format)

    # Ensure fmt_cls is a mock and its track_map is configured to fail
    if not isinstance(controller.driver.fmt_cls, MagicMock):
        controller.driver.fmt_cls = MagicMock(spec=codec.DiskDef)
    if not hasattr(controller.driver.fmt_cls, "track_map"):
        controller.driver.fmt_cls.track_map = MagicMock()

    controller.driver.fmt_cls.track_map.get.return_value = None

    # Act & Assert
    with pytest.raises(ValueError, match="No track definition for C:1 H:1"):
        controller.driver._convert_to_flux(1, 1)


def test_15_gw_read_track_format_codec_error(
    mocked_controller: Tuple[DiskController, Dict[str, MagicMock]]
) -> None:
    """
    Tests that a KeyError from the codec during format lookup is handled
    gracefully and returns None.
    """
    # Arrange
    controller, mocks_bundle = mocked_controller
    mock_get_diskdef = mocks_bundle["mock_get_diskdef"]
    test_format = FMT_144
    open_disk_for_rw_tests(controller, mocks_bundle, test_format=test_format)

    mock_get_diskdef.side_effect = KeyError("Format 'ibm.mfm' not found")

    # Force driver to use a standard format instead of the custom one
    controller.driver.fmt_cls = None
    controller.driver.using_custom_diskdef = False
    controller.driver.last_successful_format = None

    # Act
    result = controller.driver._read_track_with_format(0, 0, ("ibm.mfm", 500))

    # Assert
    assert result is None
    mock_get_diskdef.assert_called_with("ibm.mfm")


def test_16_gw_update_physical_format_incomplete_data(
    mocked_controller: Tuple[DiskController, Dict[str, MagicMock]]
) -> None:
    """
    Tests that the physical format is not updated if the track data object
    is missing required attributes.
    """
    # Arrange
    controller, mocks_bundle = mocked_controller
    open_disk_for_rw_tests(controller, mocks_bundle, test_format=FMT_144)
    original_format = copy.deepcopy(controller.driver.physical_format)

    # Case 1: Track data object has no 'track' attribute
    mock_dat_no_track = MagicMock()
    if hasattr(mock_dat_no_track, "track"):
        del mock_dat_no_track.track

    # Act & Assert 1
    controller.driver._update_physical_format(mock_dat_no_track, 9)
    assert controller.driver.physical_format == original_format

    # Case 2: Track data object has no 'clock' attribute
    mock_dat_no_clock = MagicMock()
    mock_dat_no_clock.track = MagicMock()
    mock_dat_no_clock.track.mode = "IBM MFM"
    if hasattr(mock_dat_no_clock.track, "clock"):
        del mock_dat_no_clock.track.clock

    # Act & Assert 2
    controller.driver._update_physical_format(mock_dat_no_clock, 9)
    assert controller.driver.physical_format == original_format
