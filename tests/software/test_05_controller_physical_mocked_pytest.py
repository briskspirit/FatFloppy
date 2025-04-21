# tests/test_05_controller_physical_mocked_pytest.py
import pytest
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent.parent / 'src'))

from fatfloppy.core.controller import DiskController
from fatfloppy.core.drivers import GreaseweazleDriver
from fatfloppy.core.filesystem import FATFilesystem, FATBootSector
from fatfloppy.core.formats import BootSectorData
from fatfloppy.core.format_definitions import FLOPPY_FORMATS

FMT_144 = FLOPPY_FORMATS['ibm_3.5_1.44m']
FMT_360 = FLOPPY_FORMATS['ibm_5.25_360k']
FMT_720 = FLOPPY_FORMATS['ibm_3.5_720k']

def create_mock_flux():
    return MagicMock()

def create_mock_track_data(cyl, head, fmt):
    mock_track = MagicMock()
    mock_track.sectors = {1: MagicMock(data=b'\x00' * 512)}
    return mock_track

@pytest.fixture(scope="function")
def mocked_controller():
    with patch('greaseweazle.tools.util.usb_open') as mock_usb_open, \
         patch('greaseweazle.tools.util.Drive') as mock_drive, \
         patch('greaseweazle.tools.util.with_drive_selected') as mock_with_drive_selected, \
         patch('greaseweazle.codec.codec.get_diskdef') as mock_get_diskdef, \
         patch('fatfloppy.core.drivers.read.read_with_retry') as mock_read_with_retry:

        mock_usb = MagicMock()
        mock_usb.sample_freq = 96000000.0
        mock_usb.read_track.return_value = create_mock_flux()
        mock_usb.write_track = MagicMock()
        mock_usb.seek.return_value = None
        mock_usb.update_track = MagicMock()

        mock_usb_open.return_value = mock_usb
        mock_drive.return_value = MagicMock()
        mock_with_drive_selected.side_effect = lambda func, *args, **kwargs: func()
        mock_get_diskdef.return_value = MagicMock()
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

def open_disk_for_rw_tests(controller, mocks_bundle, test_format=FMT_144):
    mock_usb = mocks_bundle['mock_usb']
    mock_with_drive_selected = mocks_bundle['mock_with_drive_selected']
    format_info_dict = {
        "cylinders": test_format.geometry.cylinders, "heads": test_format.geometry.heads,
        "sectors_per_track": test_format.geometry.sectors_per_track, "sector_size": test_format.geometry.sector_size,
        "encoding": test_format.physical_format.encoding, "rate": test_format.physical_format.rate,
        "rpm": test_format.physical_format.rpm, "gap3": test_format.physical_format.gap3,
        "cskew": test_format.physical_format.cskew, "interleave": test_format.physical_format.interleave
    }
    mock_fs = MagicMock(spec=FATFilesystem)
    mock_fs.is_valid.return_value = True
    mock_fat_bs = MagicMock(spec=FATBootSector)
    bsd = test_format.boot_sector or BootSectorData()
    for attr, value in bsd.__dict__.items():
        setattr(mock_fat_bs, attr, value)
    mock_fat_bs.is_valid.return_value = True
    mock_fat_bs.calculate_fat_type.return_value = "FAT12"
    mock_fat_bs.sectors_per_track = test_format.geometry.sectors_per_track
    mock_fat_bs.num_heads = test_format.geometry.heads
    mock_fat_bs.total_sectors = test_format.geometry.total_sectors
    mock_fs.boot_sector = mock_fat_bs

    with patch('fatfloppy.core.controller.create_filesystem', return_value=mock_fs):
        mock_usb.read_track.return_value = create_mock_flux()
        mock_with_drive_selected.side_effect = lambda func, *args, **kwargs: func()
        success = controller.open_disk(None, "physical", 'A', "3.5", format_info=format_info_dict)
        assert success is True, f"open_disk failed in helper for format {test_format.name}"
        assert controller.disk is not None, "Disk object not created in helper"
        assert controller.driver is not None, "Driver object not created in helper"
        assert controller.driver.initialized is True, "Driver not initialized in helper"
        assert controller.driver.using_custom_diskdef is True, "Custom diskdef not set in helper"
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

    mock_usb.read_track.return_value = create_mock_flux()
    def mock_read_retry_auto_detect(usb, args, track_iter):
        track_str = str(args.tracks)
        c, h = -1, -1
        try:
            parts = track_str.split(':')
            c = int(parts[0][2:])
            h = int(parts[1][2:])
        except:
            pass
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
    mock_fs.boot_sector = mock_fat_bs

    with patch('fatfloppy.core.controller.create_filesystem', return_value=mock_fs):
        success = controller.open_disk(source=None, disk_type="physical", drive_letter=drive_letter, drive_size=drive_size)
        assert success is True, "controller.open_disk should return True"
        assert isinstance(controller.driver, GreaseweazleDriver), "Driver type mismatch"
        mock_usb_open.assert_called_with(None)
        mock_drive.assert_called()
        assert mock_usb.read_track.called, "mock_usb.read_track should be called for RPM"
        assert controller.driver.initialized is True, "Driver should be initialized"
        assert controller.disk is not None, "Disk object should be created"
        assert controller.disk.geometry is not None, "Disk geometry should be set"
        assert controller.filesystem == mock_fs, "Filesystem object should be set"
        geom = controller.disk.geometry
        assert geom.sectors_per_track == mock_fat_bs.sectors_per_track
        assert geom.heads == mock_fat_bs.num_heads
        pf = controller.driver.physical_format
        assert pf is not None, "Physical format should be set"
        assert pf.sectors_per_track == expected_format.physical_format.sectors_per_track
        assert pf.heads == expected_format.physical_format.heads

def test_02_open_physical_with_explicit_format_mocked(mocked_controller):
    controller, mocks_bundle = mocked_controller
    mock_usb = mocks_bundle['mock_usb']
    mock_with_drive_selected = mocks_bundle['mock_with_drive_selected']
    expected_format = FMT_144
    format_info = {
        "cylinders": expected_format.geometry.cylinders, "heads": expected_format.geometry.heads,
        "sectors_per_track": expected_format.geometry.sectors_per_track, "sector_size": expected_format.geometry.sector_size,
        "encoding": expected_format.physical_format.encoding, "rate": expected_format.physical_format.rate,
        "rpm": expected_format.physical_format.rpm, "gap3": expected_format.physical_format.gap3,
        "cskew": expected_format.physical_format.cskew, "interleave": expected_format.physical_format.interleave
    }
    mock_fs = MagicMock(spec=FATFilesystem)
    mock_fs.is_valid.return_value = True
    mock_fat_bs = MagicMock(spec=FATBootSector)
    bsd = expected_format.boot_sector
    for attr, value in bsd.__dict__.items():
        setattr(mock_fat_bs, attr, value)
    mock_fat_bs.is_valid.return_value = True
    mock_fat_bs.calculate_fat_type.return_value = "FAT12"
    mock_fs.boot_sector = mock_fat_bs

    with patch('fatfloppy.core.controller.create_filesystem', return_value=mock_fs):
        mock_usb.read_track.return_value = create_mock_flux()
        mock_with_drive_selected.side_effect = lambda func, *args, **kwargs: func()
        success = controller.open_disk(None, "physical", "A", "3.5", format_info)
        assert success, "Failed to open disk with explicit format"
        assert controller.filesystem == mock_fs, "Filesystem not set correctly"
        assert controller.disk.geometry.sectors_per_track == mock_fat_bs.sectors_per_track
        assert controller.disk.geometry.heads == mock_fat_bs.num_heads

def test_03_physical_read_sector_success_mocked(mocked_controller):
    controller, mocks_bundle = mocked_controller
    mock_read_with_retry = mocks_bundle['mock_read_with_retry']
    open_disk_for_rw_tests(controller, mocks_bundle)
    mock_track_data = MagicMock()
    mock_track_data.sectors = {1: MagicMock(data=b'\x00' * 512)}
    mock_read_with_retry.return_value = (create_mock_flux(), mock_track_data)
    data = controller.disk.read_sector(0, 0, 1)
    assert data == b'\x00' * 512, "Sector data should match expected bytes"

def test_04_physical_read_sector_not_found_mocked(mocked_controller):
    controller, mocks_bundle = mocked_controller
    mock_read_with_retry = mocks_bundle['mock_read_with_retry']
    open_disk_for_rw_tests(controller, mocks_bundle)
    mock_track_data = MagicMock()
    mock_track_data.sectors = {}  # No sectors available
    mock_read_with_retry.return_value = (create_mock_flux(), mock_track_data)
    data = controller.disk.read_sector(0, 0, 1)
    assert data is None or data == b'\x00' * 512, "Sector not found should return None or zero-filled bytes"

def test_06_physical_write_sector_and_flush_success_mocked(mocked_controller):
    controller, mocks_bundle = mocked_controller
    mock_usb = mocks_bundle['mock_usb']
    open_disk_for_rw_tests(controller, mocks_bundle)
    data = bytearray(512)
    controller.disk.write_sector(0, 0, 1, data)
    controller.driver.flush()
    assert mock_usb.write_track.called, "write_track should be called during flush"

def test_07_physical_flush_write_error_mocked(mocked_controller):
    controller, mocks_bundle = mocked_controller
    mock_usb = mocks_bundle['mock_usb']
    open_disk_for_rw_tests(controller, mocks_bundle)
    mock_usb.write_track.side_effect = Exception("Write error")
    controller.disk.write_sector(0, 0, 1, bytearray(512))
    controller.driver.flush()
    assert mock_usb.write_track.call_count == 1, "write_track should be attempted once"

def test_09_physical_flush_partial_track_reads_first_mocked(mocked_controller):
    controller, mocks_bundle = mocked_controller
    mock_read_with_retry = mocks_bundle['mock_read_with_retry']
    open_disk_for_rw_tests(controller, mocks_bundle)
    mock_track_data = create_mock_track_data(0, 0, FMT_144)
    mock_read_with_retry.return_value = (create_mock_flux(), mock_track_data)
    controller.disk.write_sector(0, 0, 1, bytearray(512))
    controller.driver.flush()
    assert mock_read_with_retry.called, "read_with_retry should be called before flush"

def test_10_gw_cache_invalidation_mocked(mocked_controller):
    controller, mocks_bundle = mocked_controller
    mock_read_with_retry = mocks_bundle['mock_read_with_retry']
    mock_usb = mocks_bundle['mock_usb']
    open_disk_for_rw_tests(controller, mocks_bundle)
    # Enable write verification
    controller.driver.verify_writes = True
    # Set up mock for initial read and verification read
    initial_track_data = create_mock_track_data(0, 0, FMT_144)
    verify_track_data = create_mock_track_data(0, 0, FMT_144)
    mock_read_with_retry.side_effect = [
        (create_mock_flux(), initial_track_data),  # Initial read if needed
        (create_mock_flux(), verify_track_data)    # Verification read after write
    ]
    # Write and flush
    test_data = bytearray([0xAA] * 512)
    controller.disk.write_sector(0, 0, 1, test_data)
    controller.driver.flush()
    # Ensure write and verify happened
    assert mock_usb.write_track.called, "write_track should be called during flush"
    assert mock_read_with_retry.call_count >= 1, "read_with_retry should be called for verification"
    # Reset mock to test cache invalidation
    mock_read_with_retry.reset_mock()
    mock_read_with_retry.return_value = (create_mock_flux(), create_mock_track_data(0, 0, FMT_144))
    # Read same sector again
    controller.disk.read_sector(0, 0, 1)
    assert mock_read_with_retry.called, "read_with_retry should be called again, indicating cache invalidation"

def test_11_gw_write_verify_success_mocked(mocked_controller):
    controller, mocks_bundle = mocked_controller
    mock_read_with_retry = mocks_bundle['mock_read_with_retry']
    open_disk_for_rw_tests(controller, mocks_bundle)
    data = bytearray(512)
    mock_track_data = MagicMock()
    mock_track_data.sectors = {1: MagicMock(data=data)}
    mock_read_with_retry.return_value = (create_mock_flux(), mock_track_data)
    controller.disk.write_sector(0, 0, 1, data)
    controller.driver.flush()
    assert mock_read_with_retry.called, "read_with_retry should be called to verify write"
