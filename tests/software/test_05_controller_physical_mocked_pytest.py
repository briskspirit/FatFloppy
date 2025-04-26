# tests/test_05_controller_physical_mocked_pytest.py
import pytest
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent.parent / 'src'))

from fatfloppy.core.controller import DiskController
from fatfloppy.core.drivers import GreaseweazleDriver
from fatfloppy.core.filesystem import FATFilesystem, FATBootSector
from fatfloppy.core.formats import FATVolumeInfo, FormatProfile
from fatfloppy.core.format_definitions import FLOPPY_FORMATS
# Import Disk to patch its method correctly
from fatfloppy.core.disk import Disk

FMT_144 = FLOPPY_FORMATS['ibm_3.5_1.44m']
FMT_360 = FLOPPY_FORMATS['ibm_5.25_360k']
FMT_720 = FLOPPY_FORMATS['ibm_3.5_720k']

def create_mock_flux():
    return MagicMock()

def create_mock_track_data(cyl, head, fmt: FormatProfile):
    mock_track = MagicMock()
    bps = 512 # Default
    if fmt and fmt.physical_format:
        bps = fmt.physical_format.bytes_per_sector
    # Simulate a single sector for simplicity in mock, adjust if needed
    mock_track.sectors = {1: MagicMock(data=b'\x00' * bps)}
    mock_track.track = MagicMock() # Add nested track mock
    # Mock attributes that might be checked in _update_physical_format
    mock_track.track.mode = "IBM MFM" # Example
    mock_track.track.clock = 1.0 / (500 * 2000) # Example for 500kbps MFM
    mock_track.idam = MagicMock()
    mock_track.idam.r = 1
    mock_track.dam = MagicMock()
    mock_track.dam.data = b'\x00' * bps
    return mock_track

@pytest.fixture(scope="function")
def mocked_controller():
    with patch('greaseweazle.tools.util.usb_open') as mock_usb_open, \
         patch('greaseweazle.tools.util.Drive') as mock_drive, \
         patch('greaseweazle.tools.util.with_drive_selected') as mock_with_drive_selected, \
         patch('greaseweazle.codec.codec.get_diskdef') as mock_get_diskdef, \
         patch('fatfloppy.core.drivers.greaseweazle.read.read_with_retry') as mock_read_with_retry:

        mock_usb = MagicMock()
        mock_usb.sample_freq = 96000000.0 # Example value
        # Simulate reading index pulses for RPM calculation
        mock_flux = MagicMock()
        mock_flux.index_list = [0, 20000000, 40000000] # Example for ~300 RPM at 96MHz sample
        mock_flux.ticks_per_rev = 20000000 # Example
        mock_usb.read_track.return_value = mock_flux
        mock_usb.write_track = MagicMock()
        mock_usb.seek.return_value = None
        mock_usb.update_track = MagicMock()

        mock_usb_open.return_value = mock_usb
        mock_drive.return_value = MagicMock()
        mock_with_drive_selected.side_effect = lambda func, *args, **kwargs: func() # Simple passthrough
        mock_get_diskdef.return_value = MagicMock()
        # Default return value for read_with_retry, specific tests can override
        mock_read_with_retry.return_value = (create_mock_flux(), create_mock_track_data(0, 0, None))

        controller = DiskController()
        yield controller, {
            'mock_usb_open': mock_usb_open,
            'mock_usb': mock_usb,
            'mock_drive': mock_drive,
            'mock_with_drive_selected': mock_with_drive_selected,
            'mock_get_diskdef': mock_get_diskdef,
            'mock_read_with_retry': mock_read_with_retry
        }

def open_disk_for_rw_tests(controller: DiskController, mocks_bundle: dict, test_format: FormatProfile = FMT_144):
    mock_usb = mocks_bundle['mock_usb']
    mock_with_drive_selected = mocks_bundle['mock_with_drive_selected']

    # Extract info from physical_format and track_formats
    phys_fmt = test_format.physical_format
    # Assuming uniform format for simplicity in test setup
    track_fmt = phys_fmt.track_formats[0]
    format_info_dict = {
        "cylinders": phys_fmt.cylinders,
        "heads": phys_fmt.heads,
        "sectors_per_track": track_fmt.sectors_per_track, # From TrackFormat
        "bytes_per_sector": phys_fmt.bytes_per_sector,
        "encoding": track_fmt.encoding, # From TrackFormat
        "rate": track_fmt.rate, # From TrackFormat
        "rpm": phys_fmt.rpm,
        "gap3": track_fmt.gap3, # From TrackFormat
        "interleave": track_fmt.interleave # From TrackFormat
    }

    mock_fs = MagicMock(spec=FATFilesystem)
    mock_fs.is_valid.return_value = True
    mock_fat_bs = MagicMock(spec=FATBootSector)
    bsd = test_format.boot_sector or FATVolumeInfo()
    for attr, value in bsd.__dict__.items():
        setattr(mock_fat_bs, attr, value)
    mock_fat_bs.is_valid.return_value = True
    mock_fat_bs.calculate_fat_type.return_value = "FAT12"
    mock_fat_bs.sectors_per_track = track_fmt.sectors_per_track
    mock_fat_bs.num_heads = phys_fmt.heads
    mock_fat_bs.total_sectors = phys_fmt.total_sectors
    mock_fs.boot_sector = mock_fat_bs

    # Ensure read_track mock returns something reasonable for RPM calc
    mock_flux_rpm = MagicMock()
    mock_flux_rpm.index_list = [0, 20000000, 40000000] # Simulate ~300 RPM
    mock_flux_rpm.ticks_per_rev = 20000000
    mock_usb.read_track.return_value = mock_flux_rpm

    with patch('fatfloppy.core.controller.create_filesystem', return_value=mock_fs):
        mock_with_drive_selected.side_effect = lambda func, *args, **kwargs: func()
        success = controller.open_disk(None, "physical", 'A', "3.5", format_info=format_info_dict)
        assert success is True, f"open_disk failed in helper for format {test_format.name}"
        assert controller.disk is not None, "Disk object not created in helper"
        assert controller.driver is not None, "Driver object not created in helper"
        assert controller.driver.initialized is True, "Driver not initialized in helper"
        # Explicit format is set, custom diskdef should be created
        assert controller.driver.fmt_cls is not None, "Custom diskdef (fmt_cls) not set in helper"
        assert controller.filesystem == mock_fs, "Filesystem not set correctly in helper"

def test_01_open_physical_drive_A_35_auto_detect_mocked(mocked_controller):
    controller, mocks_bundle = mocked_controller
    mock_usb = mocks_bundle['mock_usb']
    mock_usb_open = mocks_bundle['mock_usb_open']
    mock_drive = mocks_bundle['mock_drive']
    mock_read_with_retry = mocks_bundle['mock_read_with_retry']

    drive_letter = 'A'
    drive_size = "3.5"
    expected_format = FMT_144

    # Ensure read_track mock returns something reasonable for RPM calc
    mock_flux_rpm = MagicMock()
    mock_flux_rpm.index_list = [0, 20000000, 40000000] # Simulate ~300 RPM
    mock_flux_rpm.ticks_per_rev = 20000000
    mock_usb.read_track.return_value = mock_flux_rpm

    # Mock read_with_retry to return data consistent with expected_format
    def mock_read_retry_auto_detect(usb, args, track_iter):
        track_str = str(args.tracks)
        c, h = -1, -1
        try:
            parts = track_str.split(':')
            c = int(parts[0][2:])
            h = int(parts[1][2:])
        except:
            pass
        # Use the expected format to create mock data
        return (create_mock_flux(), create_mock_track_data(c, h, expected_format))

    mock_read_with_retry.side_effect = mock_read_retry_auto_detect

    mock_fs = MagicMock(spec=FATFilesystem)
    mock_fs.is_valid.return_value = True
    mock_fat_bs = MagicMock(spec=FATBootSector)
    bsd = expected_format.boot_sector
    for attr, value in bsd.__dict__.items():
        setattr(mock_fat_bs, attr, value)
    mock_fat_bs.is_valid.return_value = True
    mock_fat_bs.calculate_fat_type.return_value = "FAT12"
    mock_fat_bs.sectors_per_track = expected_format.physical_format.track_formats[0].sectors_per_track
    mock_fat_bs.num_heads = expected_format.physical_format.heads
    mock_fat_bs.total_sectors = expected_format.physical_format.total_sectors
    mock_fs.boot_sector = mock_fat_bs

    # Mock read_boot_sector to return data consistent with the expected format
    mock_boot_sector_bytes = bsd.to_bytes()
    # --- FIX: Patch Disk.read_boot_sector, not DiskController ---
    with patch('fatfloppy.core.disk.Disk.read_boot_sector', return_value=mock_boot_sector_bytes), \
         patch('fatfloppy.core.controller.create_filesystem', return_value=mock_fs):
        success = controller.open_disk(source=None, disk_type="physical", drive_letter=drive_letter, drive_size=drive_size)
    # --- End Fix ---

    assert success is True, "controller.open_disk should return True"
    assert isinstance(controller.driver, GreaseweazleDriver), "Driver type mismatch"
    mock_usb_open.assert_called_with(None)
    mock_drive.assert_called()
    assert mock_usb.read_track.called, "mock_usb.read_track should be called for RPM"
    assert controller.driver.initialized is True, "Driver should be initialized"
    assert controller.disk is not None, "Disk object should be created"
    assert controller.disk.physical_format is not None, "Disk geometry should be set"
    geom = controller.disk.physical_format
    assert geom.get_sectors_per_track(0, 0) == mock_fat_bs.sectors_per_track
    assert geom.heads == mock_fat_bs.num_heads
    assert controller.filesystem == mock_fs, "Filesystem object should be set"
    pf = controller.driver.physical_format
    assert pf is not None, "Driver physical format should be set"
    assert pf.get_sectors_per_track(0, 0) == expected_format.physical_format.get_sectors_per_track(0, 0)
    assert pf.heads == expected_format.physical_format.heads

def test_02_open_physical_with_explicit_format_mocked(mocked_controller):
    controller, mocks_bundle = mocked_controller
    mock_usb = mocks_bundle['mock_usb']
    mock_with_drive_selected = mocks_bundle['mock_with_drive_selected']
    expected_format = FMT_144

    # Extract info correctly for format_info dict
    phys_fmt = expected_format.physical_format
    track_fmt = phys_fmt.track_formats[0]
    format_info = {
        "cylinders": phys_fmt.cylinders,
        "heads": phys_fmt.heads,
        "sectors_per_track": track_fmt.sectors_per_track,
        "bytes_per_sector": phys_fmt.bytes_per_sector,
        "encoding": track_fmt.encoding,
        "rate": track_fmt.rate,
        "rpm": phys_fmt.rpm,
        "gap3": track_fmt.gap3,
        "interleave": track_fmt.interleave
    }

    mock_fs = MagicMock(spec=FATFilesystem)
    mock_fs.is_valid.return_value = True
    mock_fat_bs = MagicMock(spec=FATBootSector)
    bsd = expected_format.boot_sector
    for attr, value in bsd.__dict__.items():
        setattr(mock_fat_bs, attr, value)
    mock_fat_bs.is_valid.return_value = True
    mock_fat_bs.calculate_fat_type.return_value = "FAT12"
    mock_fat_bs.sectors_per_track = track_fmt.sectors_per_track
    mock_fat_bs.num_heads = phys_fmt.heads
    mock_fat_bs.total_sectors = phys_fmt.total_sectors
    mock_fs.boot_sector = mock_fat_bs

    # Ensure read_track mock returns something reasonable for RPM calc
    mock_flux_rpm = MagicMock()
    mock_flux_rpm.index_list = [0, 20000000, 40000000] # Simulate ~300 RPM
    mock_flux_rpm.ticks_per_rev = 20000000
    mock_usb.read_track.return_value = mock_flux_rpm

    with patch('fatfloppy.core.controller.create_filesystem', return_value=mock_fs):
        mock_with_drive_selected.side_effect = lambda func, *args, **kwargs: func()
        success = controller.open_disk(None, "physical", "A", "3.5", format_info)
        assert success, "Failed to open disk with explicit format"
        assert controller.filesystem == mock_fs, "Filesystem not set correctly"
        assert controller.disk.physical_format is not None, "Disk geometry should be set"
        geom = controller.disk.physical_format
        assert geom.get_sectors_per_track(0, 0) == mock_fat_bs.sectors_per_track
        assert geom.get_sectors_per_track(0, 0) == expected_format.physical_format.get_sectors_per_track(0, 0)
        assert geom.heads == mock_fat_bs.num_heads
        assert controller.driver.physical_format is not None
        assert controller.driver.physical_format.get_sectors_per_track(0, 0) == expected_format.physical_format.get_sectors_per_track(0, 0)

def test_03_physical_read_sector_success_mocked(mocked_controller):
    controller, mocks_bundle = mocked_controller
    mock_read_with_retry = mocks_bundle['mock_read_with_retry']
    test_format = FMT_144 # Use a specific format for setup
    open_disk_for_rw_tests(controller, mocks_bundle, test_format=test_format)
    bps = test_format.physical_format.bytes_per_sector
    mock_track_data = create_mock_track_data(0, 0, test_format)
    mock_read_with_retry.return_value = (create_mock_flux(), mock_track_data)
    data = controller.disk.read_sector(0, 0, 1)
    assert data == b'\x00' * bps, "Sector data should match expected bytes"

def test_04_physical_read_sector_not_found_mocked(mocked_controller):
    controller, mocks_bundle = mocked_controller
    mock_read_with_retry = mocks_bundle['mock_read_with_retry']
    test_format = FMT_144
    open_disk_for_rw_tests(controller, mocks_bundle, test_format=test_format)
    bps = test_format.physical_format.bytes_per_sector
    mock_track_data = MagicMock()
    mock_track_data.sectors = {}  # No sectors available
    mock_read_with_retry.return_value = (create_mock_flux(), mock_track_data)
    data = controller.disk.read_sector(0, 0, 1)
    assert data == b'\x00' * bps, "Sector not found should return zero-filled bytes"

def test_06_physical_write_sector_and_flush_success_mocked(mocked_controller):
    controller, mocks_bundle = mocked_controller
    mock_usb = mocks_bundle['mock_usb']
    test_format = FMT_144
    open_disk_for_rw_tests(controller, mocks_bundle, test_format=test_format)
    bps = test_format.physical_format.bytes_per_sector
    data = bytearray(bps)
    controller.disk.write_sector(0, 0, 1, data)
    controller.driver.flush()
    assert mock_usb.write_track.called, "write_track should be called during flush"

def test_07_physical_flush_write_error_mocked(mocked_controller):
    controller, mocks_bundle = mocked_controller
    mock_usb = mocks_bundle['mock_usb']
    test_format = FMT_144
    open_disk_for_rw_tests(controller, mocks_bundle, test_format=test_format)
    bps = test_format.physical_format.bytes_per_sector
    mock_usb.write_track.side_effect = Exception("Write error")
    controller.disk.write_sector(0, 0, 1, bytearray(bps))
    controller.driver.flush()
    assert mock_usb.write_track.call_count == 1, "write_track should be attempted once"

def test_09_physical_flush_partial_track_reads_first_mocked(mocked_controller):
    controller, mocks_bundle = mocked_controller
    mock_read_with_retry = mocks_bundle['mock_read_with_retry']
    test_format = FMT_144
    open_disk_for_rw_tests(controller, mocks_bundle, test_format=test_format)
    bps = test_format.physical_format.bytes_per_sector
    mock_track_data = create_mock_track_data(0, 0, test_format)
    mock_read_with_retry.return_value = (create_mock_flux(), mock_track_data)
    controller.disk.write_sector(0, 0, 1, bytearray(bps)) # Write only one sector
    controller.driver.flush()
    # Check if read was called because the written track wasn't full
    # It should have been called if len(dirty_sectors) < sectors_per_track
    spt = test_format.physical_format.get_sectors_per_track(0, 0)
    if spt > 1:
        assert mock_read_with_retry.called, "read_with_retry should be called before flush for partial track"
    else:
        # If spt is 1, read might not be called, skip assertion
        pass


def test_10_gw_cache_invalidation_mocked(mocked_controller):
    controller, mocks_bundle = mocked_controller
    mock_read_with_retry = mocks_bundle['mock_read_with_retry']
    mock_usb = mocks_bundle['mock_usb']
    test_format = FMT_144
    open_disk_for_rw_tests(controller, mocks_bundle, test_format=test_format)
    bps = test_format.physical_format.bytes_per_sector
    # Enable write verification
    controller.driver.verify_writes = True
    # Set up mock for initial read and verification read
    initial_track_data = create_mock_track_data(0, 0, test_format)
    verify_track_data = MagicMock()
    verify_track_data.sectors = {1: MagicMock(data=bytearray([0xAA] * bps))} # Use correct bps
    verify_track_data.track = initial_track_data.track # Reuse track mock info
    verify_track_data.idam = initial_track_data.idam
    verify_track_data.dam = initial_track_data.dam

    # Setup side effect for read_with_retry
    # First call (potentially in flush for partial read), then verification read
    mock_read_with_retry.side_effect = [
        (create_mock_flux(), initial_track_data),
        (create_mock_flux(), verify_track_data)
    ]

    # Write and flush
    test_data = bytearray([0xAA] * bps) # Use correct bps
    controller.disk.write_sector(0, 0, 1, test_data)
    controller.driver.flush()

    # Ensure write and verify happened
    assert mock_usb.write_track.called, "write_track should be called during flush"
    # Verification read happens inside flush -> write_track_wrapper -> _write_track
    # Adjust assertion based on expected calls
    spt = test_format.physical_format.get_sectors_per_track(0, 0)
    expected_read_calls = 1 if spt == 1 else 2 # 1 for verify, +1 if partial read needed
    assert mock_read_with_retry.call_count >= 1, f"read_with_retry should be called at least once (verify). Called: {mock_read_with_retry.call_count}"


    # Clear cache to simulate invalidation
    controller.driver.track_data.clear()
    controller.driver.sector_cache.clear()

    # Reset mock and set new return value for the next read
    mock_read_with_retry.reset_mock()
    mock_read_with_retry.side_effect = None # Clear side effect
    mock_read_with_retry.return_value = (create_mock_flux(), create_mock_track_data(0, 0, test_format))

    # Read same sector again
    controller.disk.read_sector(0, 0, 1)
    assert mock_read_with_retry.called, "read_with_retry should be called again, indicating cache invalidation"

def test_11_gw_write_verify_success_mocked(mocked_controller):
    controller, mocks_bundle = mocked_controller
    mock_read_with_retry = mocks_bundle['mock_read_with_retry']
    test_format = FMT_144
    open_disk_for_rw_tests(controller, mocks_bundle, test_format=test_format)
    bps = test_format.physical_format.bytes_per_sector
    data = bytearray(bps)
    mock_track_data = MagicMock()
    mock_track_data.sectors = {1: MagicMock(data=data)}
    mock_track_data.track = create_mock_track_data(0, 0, test_format).track # Add nested track mock
    mock_track_data.idam = create_mock_track_data(0, 0, test_format).idam
    mock_track_data.dam = create_mock_track_data(0, 0, test_format).dam

    # Side effect: initial read (if partial track), then verification read
    spt = test_format.physical_format.get_sectors_per_track(0, 0)
    side_effect_list = []
    if spt > 1:
        side_effect_list.append((create_mock_flux(), create_mock_track_data(0, 0, test_format))) # Initial read for partial flush
    side_effect_list.append((create_mock_flux(), mock_track_data)) # Verification read
    mock_read_with_retry.side_effect = side_effect_list

    controller.driver.verify_writes = True # Ensure verification is on
    controller.disk.write_sector(0, 0, 1, data)
    controller.driver.flush()

    # Check if read_with_retry was called (for verification)
    assert mock_read_with_retry.called, "read_with_retry should be called to verify write"
    # Assert it was called the expected number of times
    expected_calls = 1 if spt == 1 else 2
    assert mock_read_with_retry.call_count == expected_calls, f"Expected {expected_calls} read calls, got {mock_read_with_retry.call_count}"
