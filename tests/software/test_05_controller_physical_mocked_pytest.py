# tests/test_05_controller_physical_mocked_pytest.py
import pytest
import sys
from pathlib import Path
import copy
import types
from typing import Optional
from unittest.mock import patch, MagicMock, PropertyMock, call

# Assuming greaseweazle might raise specific errors, e.g., USBError
try:
    from greaseweazle.usb import USBError
except ImportError:
    USBError = IOError


sys.path.insert(0, str(Path(__file__).parent.parent.parent / 'src'))

from fatfloppy.core.controller import DiskController
from fatfloppy.core.drivers import GreaseweazleDriver
from fatfloppy.core.filesystems.fat12fs import FATFilesystem, FATVolumeInfo
from fatfloppy.core.format_profile import FormatProfile
from fatfloppy.core.format_definitions import FLOPPY_FORMATS
from fatfloppy.core.disk import Disk
from greaseweazle.codec import codec
from greaseweazle.codec.ibm import ibm

FMT_144 = FLOPPY_FORMATS['ibm_3.5_1.44m']
FMT_360 = FLOPPY_FORMATS['ibm_5.25_360k']
FMT_720 = FLOPPY_FORMATS['ibm_3.5_720k']

def create_mock_flux(ticks=20000000):
    mock = MagicMock()
    mock.index_list = [0, ticks, ticks*2]
    mock.ticks_per_rev = ticks
    mock.list = [ticks // 2] * 2
    mock.ticks_to_index = ticks
    return mock

def create_mock_track_data(cyl, head, fmt: Optional[FormatProfile], sectors_present=None):
    mock_track = MagicMock()
    bps = 512
    spt = 18
    if fmt and fmt.physical_format:
        bps = fmt.physical_format.bytes_per_sector
        try:
            tf = fmt.physical_format.get_track_format(cyl, head)
            spt = tf.sectors_per_track
        except ValueError:
             pass

    if sectors_present is None:
        sectors_present = list(range(1, spt + 1))

    mock_sectors = []
    for s_num in sectors_present:
         mock_sector = MagicMock()
         mock_sector.idam = MagicMock()
         mock_sector.idam.r = s_num
         mock_sector.idam.crc = 0
         mock_sector.dam = MagicMock()
         mock_sector.dam.data = bytearray(b'\x00' * bps)
         mock_sector.dam.crc = 0
         mock_sector.crc = 0
         mock_sectors.append(mock_sector)

    mock_dat = MagicMock()
    mock_dat.sectors = mock_sectors
    mock_dat.track = MagicMock()
    mock_dat.track.mode = "IBM MFM"
    mock_dat.track.clock = 1.0 / (500 * 2000)
    return mock_dat


@pytest.fixture(scope="function")
def mocked_controller(request):
    print(f"\n--- [Fixture Setup] Mocking GW Controller for test: {request.node.name} ---")
    mock_custom_diskdef_instance = MagicMock(spec=codec.DiskDef)
    mock_custom_diskdef_instance.name = "fixture_mock_diskdef"
    mock_ibm_track = MagicMock(spec=ibm.IBMTrack)
    mock_ibm_track.sectors = [MagicMock(idam=MagicMock(r=i, crc=0), dam=MagicMock(data=bytearray(512), crc=0), crc=0) for i in range(1, 19)]
    mock_ibm_track.master_track.return_value.flux_for_writeout.return_value = create_mock_flux()
    mock_track_def = MagicMock(spec=ibm.IBMTrack_FixedDef) # Correct spec
    mock_track_def.mk_track.return_value = mock_ibm_track
    mock_custom_diskdef_instance.track_map = MagicMock()
    mock_custom_diskdef_instance.track_map.get.return_value = mock_track_def

    mock_get_diskdef_instance = MagicMock(spec=codec.DiskDef)
    mock_get_diskdef_instance.name = "mock_get_diskdef_name"
    mock_get_diskdef_instance.track_map = MagicMock()
    mock_get_diskdef_instance.track_map.get.return_value = mock_track_def

    # Patch create_gw_diskdef at the module level where it's imported in greaseweazle.py
    with patch('fatfloppy.core.drivers.greaseweazle.create_greaseweazle_diskdef', return_value=mock_custom_diskdef_instance) as mock_create_gw_diskdef, \
         patch('greaseweazle.tools.util.usb_open') as mock_usb_open, \
         patch('greaseweazle.tools.util.Drive') as mock_drive, \
         patch('greaseweazle.tools.util.with_drive_selected') as mock_with_drive_selected, \
         patch('greaseweazle.codec.codec.get_diskdef', return_value=mock_get_diskdef_instance) as mock_get_diskdef, \
         patch('fatfloppy.core.drivers.greaseweazle.read.read_with_retry') as mock_read_with_retry:

        mock_usb = MagicMock()
        mock_usb.sample_freq = 96000000.0
        mock_usb.read_track.return_value = create_mock_flux()
        mock_usb.write_track = MagicMock()
        mock_usb.seek.return_value = None
        mock_usb.update_track = MagicMock()

        mock_usb_open.return_value = mock_usb
        mock_drive_instance = MagicMock()
        mock_drive.return_value = mock_drive_instance
        mock_with_drive_selected.side_effect = lambda func, *args, **kwargs: func()
        mock_read_with_retry.return_value = (create_mock_flux(), create_mock_track_data(0, 0, FMT_144))

        controller = DiskController()
        yield controller, {
            'mock_usb_open': mock_usb_open, 'mock_usb': mock_usb,
            'mock_drive': mock_drive, 'mock_drive_instance': mock_drive_instance,
            'mock_with_drive_selected': mock_with_drive_selected,
            'mock_get_diskdef': mock_get_diskdef,
            'mock_read_with_retry': mock_read_with_retry,
            'mock_create_gw_diskdef': mock_create_gw_diskdef,
            'mock_custom_diskdef_instance': mock_custom_diskdef_instance
        }
    print(f"--- [Fixture Teardown] Mock GW Controller test: {request.node.name} ---")

def open_disk_for_rw_tests(controller: DiskController, mocks_bundle: dict, test_format: FormatProfile = FMT_144):
    mock_usb = mocks_bundle['mock_usb']
    mock_with_drive_selected = mocks_bundle['mock_with_drive_selected']
    mock_custom_diskdef_instance = mocks_bundle['mock_custom_diskdef_instance']

    phys_fmt = test_format.physical_format
    track_fmt = phys_fmt.track_formats[0]
    format_info_dict = {
        "format_name": test_format.name, "cylinders": phys_fmt.cylinders, "heads": phys_fmt.heads,
        "sectors_per_track": track_fmt.sectors_per_track, "bytes_per_sector": phys_fmt.bytes_per_sector,
        "encoding": track_fmt.encoding, "rate": track_fmt.rate, "rpm": phys_fmt.rpm,
        "gap3_bytes": track_fmt.gap3_bytes, "interleave": track_fmt.interleave
    }

    mock_fs = MagicMock(spec=FATFilesystem)
    mock_fs.is_valid.return_value = True
    mock_fat_bs = MagicMock(spec=FATVolumeInfo)
    bsd = test_format.filesystem_config or FATVolumeInfo()
    for attr, value in bsd.__dict__.items():
         if attr not in ['logger']: setattr(mock_fat_bs, attr, value)
    mock_fat_bs.is_valid.return_value = True
    mock_fat_bs.sectors_per_track = track_fmt.sectors_per_track
    mock_fat_bs.num_heads = phys_fmt.heads
    mock_fat_bs.total_sectors = phys_fmt.total_sectors
    mock_fs.boot_sector = mock_fat_bs
    mock_fs.get_allocated_units.return_value = []
    mock_fs.get_free_space.return_value = (phys_fmt.total_bytes, phys_fmt.total_bytes)

    mock_usb.read_track.return_value = create_mock_flux()

    mock_boot_sector_bytes = bsd.to_bytes()
    # No need to patch create_gw_diskdef here; it's handled by the fixture
    with patch('fatfloppy.core.disk.Disk.read_sector', return_value=mock_boot_sector_bytes), \
         patch('fatfloppy.core.controller.create_filesystem', return_value=mock_fs):
        mock_with_drive_selected.side_effect = lambda func, *args, **kwargs: func()
        success = controller.open_disk(None, "physical", 'A', "3.5", format_info=format_info_dict)
        assert success is True, f"open_disk failed in helper for format {test_format.name}"
        assert controller.disk is not None
        assert controller.driver is not None
        assert controller.driver.initialized is True
        assert controller.driver.fmt_cls is mock_custom_diskdef_instance
        assert hasattr(controller.driver.fmt_cls, 'name'), "Mock fmt_cls missing name after setup"
        assert controller.driver.fmt_cls.name == "fixture_mock_diskdef"
        assert controller.filesystem == mock_fs


# --- Tests 01-09 remain the same ---
def test_01_open_physical_drive_A_35_auto_detect_mocked(mocked_controller):
    controller, mocks_bundle = mocked_controller
    mock_usb = mocks_bundle['mock_usb']
    mock_usb_open = mocks_bundle['mock_usb_open']
    mock_drive = mocks_bundle['mock_drive']
    mock_read_with_retry = mocks_bundle['mock_read_with_retry']
    mock_with_drive_selected = mocks_bundle['mock_with_drive_selected']

    drive_letter = 'A'
    drive_size = "3.5"
    expected_format = FMT_144

    mock_usb.read_track.return_value = create_mock_flux()
    measure_rpm_called = MagicMock()
    def with_drive_side_effect(func, *args, **kwargs):
        if func.__name__ == 'measure_rpm':
             measure_rpm_called()
             func()
        else:
             func()
    mock_with_drive_selected.side_effect = with_drive_side_effect

    def mock_read_retry_auto_detect(usb, args, track_iter):
        cyl, head = 0, 0
        try:
             track_info = str(args.tracks)
             c_part = track_info.split(':')[0]
             h_part = track_info.split(':')[1]
             cyl = int(c_part.split('=')[1])
             head = int(h_part.split('=')[1])
        except (IndexError, ValueError, AttributeError):
            pass
        return (create_mock_flux(), create_mock_track_data(cyl, head, expected_format))
    mock_read_with_retry.side_effect = mock_read_retry_auto_detect

    mock_fs = MagicMock(spec=FATFilesystem)
    mock_fs.is_valid.return_value = True
    bsd = expected_format.filesystem_config
    mock_fat_bs = MagicMock(spec=FATVolumeInfo)
    for attr, value in bsd.__dict__.items():
        if attr != 'logger': setattr(mock_fat_bs, attr, value)
    mock_fat_bs.is_valid.return_value = True
    mock_fat_bs.sectors_per_track = expected_format.physical_format.track_formats[0].sectors_per_track
    mock_fat_bs.num_heads = expected_format.physical_format.heads
    mock_fat_bs.total_sectors = expected_format.physical_format.total_sectors
    mock_fs.boot_sector = mock_fat_bs
    mock_boot_sector_bytes = bsd.to_bytes()

    with patch('fatfloppy.core.disk.Disk.read_sector', return_value=mock_boot_sector_bytes), \
         patch('fatfloppy.core.controller.create_filesystem', return_value=mock_fs):
        success = controller.open_disk(source=None, disk_type="physical", drive_letter=drive_letter, drive_size=drive_size)

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


def test_02_open_physical_with_explicit_format_mocked(mocked_controller):
    controller, mocks_bundle = mocked_controller
    mock_usb = mocks_bundle['mock_usb']
    mock_with_drive_selected = mocks_bundle['mock_with_drive_selected']
    mock_custom_diskdef_instance = mocks_bundle['mock_custom_diskdef_instance']
    expected_format = FMT_144

    phys_fmt = expected_format.physical_format
    track_fmt = phys_fmt.track_formats[0]
    format_info = {
        "format_name": expected_format.name,
        "cylinders": phys_fmt.cylinders, "heads": phys_fmt.heads,
        "sectors_per_track": track_fmt.sectors_per_track, "bytes_per_sector": phys_fmt.bytes_per_sector,
        "encoding": track_fmt.encoding, "rate": track_fmt.rate, "rpm": phys_fmt.rpm,
        "gap3_bytes": track_fmt.gap3_bytes, "interleave": track_fmt.interleave
    }

    mock_fs = MagicMock(spec=FATFilesystem)
    mock_fs.is_valid.return_value = True
    mock_fat_bs = MagicMock(spec=FATVolumeInfo)
    bsd = expected_format.filesystem_config
    for attr, value in bsd.__dict__.items():
        if attr != 'logger': setattr(mock_fat_bs, attr, value)
    mock_fat_bs.is_valid.return_value = True
    mock_fat_bs.sectors_per_track = track_fmt.sectors_per_track
    mock_fat_bs.num_heads = phys_fmt.heads
    mock_fat_bs.total_sectors = phys_fmt.total_sectors
    mock_fs.boot_sector = mock_fat_bs

    mock_usb.read_track.return_value = create_mock_flux()

    with patch('fatfloppy.core.controller.create_filesystem', return_value=mock_fs):
        mock_with_drive_selected.side_effect = lambda func, *args, **kwargs: func()
        success = controller.open_disk(None, "physical", "A", "3.5", format_info)
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

def test_03_physical_read_sector_success_mocked(mocked_controller):
    controller, mocks_bundle = mocked_controller
    mock_read_with_retry = mocks_bundle['mock_read_with_retry']
    test_format = FMT_144
    open_disk_for_rw_tests(controller, mocks_bundle, test_format=test_format)
    bps = test_format.physical_format.bytes_per_sector
    mock_track_data = create_mock_track_data(0, 0, test_format)
    mock_read_with_retry.return_value = (create_mock_flux(), mock_track_data)
    data = controller.disk.read_sector(0, 0, 1)
    assert data == b'\x00' * bps

def test_04_physical_read_sector_not_found_mocked(mocked_controller):
    controller, mocks_bundle = mocked_controller
    mock_read_with_retry = mocks_bundle['mock_read_with_retry']
    test_format = FMT_144
    open_disk_for_rw_tests(controller, mocks_bundle, test_format=test_format)
    bps = test_format.physical_format.bytes_per_sector
    mock_dat_empty = MagicMock()
    mock_dat_empty.sectors = []
    mock_dat_empty.track = MagicMock()
    mock_dat_empty.track.mode = "IBM MFM"
    mock_dat_empty.track.clock = 1.0 / (500 * 2000)
    mock_read_with_retry.return_value = (create_mock_flux(), mock_dat_empty)

    data = controller.disk.read_sector(0, 0, 1)
    assert data == b'\x00' * bps

def test_06_physical_write_sector_and_flush_success_mocked(mocked_controller):
    controller, mocks_bundle = mocked_controller
    mock_usb = mocks_bundle['mock_usb']
    mock_read_with_retry = mocks_bundle['mock_read_with_retry']
    test_format = FMT_144
    open_disk_for_rw_tests(controller, mocks_bundle, test_format=test_format)
    bps = test_format.physical_format.bytes_per_sector
    data = bytearray(bps)
    mock_track_data = create_mock_track_data(0, 0, test_format, sectors_present=list(range(2,19)))
    mock_read_with_retry.return_value = (create_mock_flux(), mock_track_data)

    controller.disk.write_sector(0, 0, 1, data)
    controller.driver.flush()
    mock_usb.write_track.assert_called()

def test_07_physical_flush_write_error_mocked(mocked_controller):
    controller, mocks_bundle = mocked_controller
    mock_usb = mocks_bundle['mock_usb']
    mock_read_with_retry = mocks_bundle['mock_read_with_retry']
    test_format = FMT_144
    open_disk_for_rw_tests(controller, mocks_bundle, test_format=test_format)
    bps = test_format.physical_format.bytes_per_sector
    mock_track_data = create_mock_track_data(0, 0, test_format, sectors_present=list(range(2,19)))
    mock_read_with_retry.return_value = (create_mock_flux(), mock_track_data)
    mock_usb.write_track.side_effect = USBError("Write error")

    controller.disk.write_sector(0, 0, 1, bytearray(bps))
    controller.driver.flush() # Should catch the error
    mock_usb.write_track.assert_called_once()

def test_09_physical_flush_partial_track_reads_first_mocked(mocked_controller):
    controller, mocks_bundle = mocked_controller
    mock_read_with_retry = mocks_bundle['mock_read_with_retry']
    test_format = FMT_144
    spt = test_format.physical_format.get_sectors_per_track(0, 0)
    open_disk_for_rw_tests(controller, mocks_bundle, test_format=test_format)
    bps = test_format.physical_format.bytes_per_sector

    mock_track_data_partial = create_mock_track_data(0, 0, test_format, sectors_present=list(range(2, spt+1)))
    mock_read_with_retry.return_value = (create_mock_flux(), mock_track_data_partial)

    controller.disk.write_sector(0, 0, 1, bytearray(bps))

    mock_read_with_retry.reset_mock()
    mock_read_with_retry.return_value = (create_mock_flux(), mock_track_data_partial)

    controller.driver.flush()

    if spt > 1:
        found_read_call = False
        for call_args in mock_read_with_retry.call_args_list:
            args, kwargs = call_args
            if len(args) > 2 and isinstance(args[1], types.SimpleNamespace):
                track_set_str = str(args[1].tracks)
                if track_set_str == "c=0:h=0":
                     found_read_call = True
                     break
        assert found_read_call, "read_with_retry should be called for C=0,H=0 during flush for partial track"
    else:
        pass

def test_10_gw_cache_invalidation_mocked(mocked_controller):
    controller, mocks_bundle = mocked_controller
    mock_read_with_retry = mocks_bundle['mock_read_with_retry']
    mock_usb = mocks_bundle['mock_usb']
    test_format = FMT_144
    spt = test_format.physical_format.get_sectors_per_track(0, 0)
    bps = test_format.physical_format.bytes_per_sector

    open_disk_for_rw_tests(controller, mocks_bundle, test_format=test_format)
    controller.driver.verify_writes = True
    assert controller.driver.fmt_cls is not None
    assert hasattr(controller.driver.fmt_cls, 'name'), "Mock fmt_cls missing name after setup"

    initial_track_data = create_mock_track_data(0, 0, test_format, sectors_present=list(range(2, spt+1)))
    verify_track_data = create_mock_track_data(0, 0, test_format, sectors_present=list(range(1, spt+1)))
    verify_track_data.sectors[0].dam.data = bytearray([0xAA] * bps)
    final_read_data = create_mock_track_data(0, 0, test_format) # Data for the read after cache clear

    mock_read_with_retry.reset_mock()
    flush_reads_list = []
    def flush_read_side_effect(*args, **kwargs):
        nonlocal flush_reads_list
        call_num = len(flush_reads_list) + 1
        print(f"DEBUG: flush_read_side_effect call {call_num}")
        if call_num == 1 and spt > 1:
            print("DEBUG: Returning initial data for pre-read")
            flush_reads_list.append("pre-read")
            return (create_mock_flux(), initial_track_data)
        elif (call_num == 1 and spt == 1) or (call_num == 2 and spt > 1):
            print("DEBUG: Returning verify data")
            flush_reads_list.append("verify-read")
            return (create_mock_flux(), verify_track_data)
        else:
            pytest.fail(f"Unexpected flush read call {call_num}")
    mock_read_with_retry.side_effect = flush_read_side_effect

    test_data = bytearray([0xAA] * bps)
    controller.disk.write_sector(0, 0, 1, test_data)
    controller.driver.flush()

    mock_usb.write_track.assert_called()
    expected_flush_reads = 1 if spt == 1 else 2
    assert len(flush_reads_list) == expected_flush_reads
    assert mock_read_with_retry.call_count == expected_flush_reads

    # --- Test cache invalidation ---
    controller.driver.track_data.clear()
    controller.driver.sector_cache.clear()
    controller.driver.last_successful_format = None

    mock_read_with_retry.reset_mock()
    # --- Patch _read_track_with_format directly for the final read ---
    final_read_call_count = 0
    original_read_track_with_format = controller.driver._read_track_with_format
    def final_read_mock_wrapper(cyl, head, fmt_tuple):
        nonlocal final_read_call_count
        final_read_call_count += 1
        fmt_name_attempted = fmt_tuple[0]
        print(f"DEBUG: final_read_mock_wrapper call {final_read_call_count} with format '{fmt_name_attempted}'")
        # We expect the first attempt to use the custom format
        if fmt_name_attempted == "custom":
             print("DEBUG: Returning final_read_data for expected custom format")
             # Simulate success by returning the data dict
             # This *should* cause the _read_track loop to exit
             return {1: bytes(final_read_data.sectors[0].dam.data)} # Return the sector dict directly
        else:
            pytest.fail(f"Unexpected format '{fmt_name_attempted}' tried during final read")
            return None # Should not be reached

    with patch.object(controller.driver, '_read_track_with_format', side_effect=final_read_mock_wrapper) as mock_final_read_method:
        print("DEBUG: Performing final read_sector after cache clear")
        controller.disk.read_sector(0, 0, 1) # This triggers the patched method

        # Assert that the *patched method* was called exactly once
        mock_final_read_method.assert_called_once_with(0, 0, ('custom', None))
        assert final_read_call_count == 1, "Mock wrapper for final read was called more than once"


def test_11_gw_write_verify_success_mocked(mocked_controller):
    controller, mocks_bundle = mocked_controller
    mock_read_with_retry = mocks_bundle['mock_read_with_retry']
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
    def flush_read_side_effect(*args, **kwargs):
        nonlocal flush_reads_list
        call_num = len(flush_reads_list) + 1
        if call_num == 1 and spt > 1:
            flush_reads_list.append("pre-read")
            return (create_mock_flux(), create_mock_track_data(0, 0, test_format, sectors_present=list(range(2,spt+1))))
        elif (call_num == 1 and spt == 1) or (call_num == 2 and spt > 1):
            flush_reads_list.append("verify-read")
            return (create_mock_flux(), mock_dat_verify)
        else:
            pytest.fail(f"Unexpected flush read call {call_num}")
    mock_read_with_retry.side_effect = flush_read_side_effect

    controller.disk.write_sector(0, 0, 1, data)
    controller.driver.flush()

    expected_calls = 1 if spt == 1 else 2
    assert mock_read_with_retry.call_count == expected_calls, f"Expected {expected_calls} read calls, got {mock_read_with_retry.call_count}"
    assert "verify-read" in flush_reads_list, "Verification read did not seem to happen"


def test_12_gw_initialize_rpm_fail(mocked_controller):
    controller, mocks_bundle = mocked_controller
    mock_usb = mocks_bundle['mock_usb']
    mock_with_drive_selected = mocks_bundle['mock_with_drive_selected']

    rpm_error = USBError("RPM measurement failed")
    original_side_effect = mock_with_drive_selected.side_effect
    def rpm_fail_side_effect(func, *args, **kwargs):
        if func.__name__ == 'measure_rpm':
             raise rpm_error
        elif original_side_effect:
             return original_side_effect(func, *args, **kwargs)
        else:
             func()
    mock_with_drive_selected.side_effect = rpm_fail_side_effect

    with patch('fatfloppy.core.controller.create_filesystem', return_value=None):
        format_info_dict = {"format_name": FMT_144.name}
        controller.open_disk(None, "physical", 'A', "3.5", format_info=format_info_dict)

    assert controller.driver is not None
    assert controller.driver.initialized is True
    assert controller.driver.drive_ticks_per_rev == 0.2 * mock_usb.sample_freq

def test_13_gw_convert_to_flux_no_format(mocked_controller):
    controller, mocks_bundle = mocked_controller
    driver = GreaseweazleDriver()
    driver.usb = mocks_bundle['mock_usb']
    driver.initialized = True

    assert driver.physical_format is None
    assert driver.fmt_cls is None
    with pytest.raises(ValueError, match="No format defined for writing"):
        driver._convert_to_flux(0, 0)

def test_14_gw_convert_to_flux_no_trackdef(mocked_controller):
    controller, mocks_bundle = mocked_controller
    test_format = FMT_144
    open_disk_for_rw_tests(controller, mocks_bundle, test_format=test_format)

    if not isinstance(controller.driver.fmt_cls, MagicMock):
        controller.driver.fmt_cls = MagicMock(spec=codec.DiskDef)
    if not hasattr(controller.driver.fmt_cls, 'track_map'):
         controller.driver.fmt_cls.track_map = MagicMock()

    controller.driver.fmt_cls.track_map.get.return_value = None

    with pytest.raises(ValueError, match="No track definition for C:1 H:1"):
        controller.driver._convert_to_flux(1, 1)

def test_15_gw_read_track_format_codec_error(mocked_controller):
    controller, mocks_bundle = mocked_controller
    mock_get_diskdef = mocks_bundle['mock_get_diskdef']
    test_format = FMT_144
    open_disk_for_rw_tests(controller, mocks_bundle, test_format=test_format)

    mock_get_diskdef.side_effect = KeyError("Format 'ibm.mfm' not found")

    controller.driver.fmt_cls = None
    controller.driver.using_custom_diskdef = False
    controller.driver.last_successful_format = None

    result = controller.driver._read_track_with_format(0, 0, ("ibm.mfm", 500))

    assert result is None
    mock_get_diskdef.assert_called_with("ibm.mfm")


def test_16_gw_update_physical_format_incomplete_data(mocked_controller):
    controller, mocks_bundle = mocked_controller
    import copy
    open_disk_for_rw_tests(controller, mocks_bundle, test_format=FMT_144)

    mock_dat_no_track = MagicMock()
    if hasattr(mock_dat_no_track, 'track'): del mock_dat_no_track.track

    original_format = copy.deepcopy(controller.driver.physical_format)
    controller.driver._update_physical_format(mock_dat_no_track, 9)
    assert controller.driver.physical_format == original_format

    mock_dat_no_clock = MagicMock()
    mock_dat_no_clock.track = MagicMock()
    mock_dat_no_clock.track.mode = "IBM MFM"
    if hasattr(mock_dat_no_clock.track, 'clock'): del mock_dat_no_clock.track.clock

    controller.driver._update_physical_format(mock_dat_no_clock, 9)
    assert controller.driver.physical_format == original_format
