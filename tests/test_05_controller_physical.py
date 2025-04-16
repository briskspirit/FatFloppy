# tests/test_05_controller_physical_mocked.py # Renamed file
import unittest
from unittest.mock import patch, MagicMock, ANY, PropertyMock, call # Added call
import os
import sys
import time
import struct
import types

# Ensure src is in path or install the package
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src')))

from fatfloppy.core.controller import DiskController
from fatfloppy.core.drivers import GreaseweazleDriver, PhysicalFormat
from fatfloppy.core.filesystem import FATFilesystem
from fatfloppy.core.disk import DiskGeometry
from fatfloppy.core.format_definitions import FLOPPY_FORMATS

# Attempt to import real Greaseweazle components for realistic mocking/typing
try:
    from greaseweazle import usb as real_gw_usb
    from greaseweazle.flux import Flux as RealFlux, WriteoutFlux as RealWriteoutFlux
    from greaseweazle.codec import codec as real_gw_codec
    from greaseweazle.codec.ibm import ibm as real_gw_ibm # For IBMTrack types
    from greaseweazle.track import MasterTrack as RealMasterTrack, RawTrack as RealRawTrack # Add RawTrack
    from greaseweazle.tools import util as real_gw_util # For Drive class structure if needed
    from greaseweazle.tools import read as real_gw_read # For read_with_retry structure
    REAL_GW_AVAILABLE = True
except ImportError:
    print("Warning: Real Greaseweazle library not found. Mocking based on provided source.")
    REAL_GW_AVAILABLE = False
    # Define dummy classes/constants if real ones aren't importable
    class DummyCmdError(Exception): pass
    class DummyAck: Okay = 0; BadCommand = 1; NoIndex = 2; NoTrk0 = 3 # etc.
    class DummyCmd: GetInfo = 0; Seek = 2; Head = 3; ReadFlux = 7; WriteFlux = 8 # etc.
    real_gw_usb = MagicMock(); real_gw_usb.CmdError = DummyCmdError
    real_gw_usb.Ack = DummyAck; real_gw_usb.Cmd = DummyCmd
    RealFlux = MagicMock; RealWriteoutFlux = MagicMock; real_gw_codec = MagicMock()
    real_gw_ibm = MagicMock; RealMasterTrack = MagicMock; RealRawTrack = MagicMock
    real_gw_util = MagicMock; real_gw_read = MagicMock()

FMT_144 = FLOPPY_FORMATS['ibm_3.5_1.44m']
FMT_360 = FLOPPY_FORMATS['ibm_5.25_360k']

# Helper functions (create_mock_flux, create_mock_track_data) remain the same as in the previous version
# Helper function to create a mock Flux object
def create_mock_flux(sample_freq=16000000, ticks_per_rev=3200000, revs=2):
    mock_flux = MagicMock(spec=RealFlux if REAL_GW_AVAILABLE else None)
    mock_flux.sample_freq = sample_freq
    mock_flux.index_list = [float(ticks_per_rev)] * revs
    avg_interval = 4e-6 * sample_freq # ~64 ticks at 16MHz for MFM 500kbps
    num_fluxes = int((ticks_per_rev * revs) / avg_interval) if avg_interval > 0 else 0
    mock_flux.list = [float(avg_interval + (-1)**i * avg_interval * 0.1) for i in range(num_fluxes)]
    current_sum = sum(mock_flux.list)
    target_sum = ticks_per_rev * revs
    if current_sum > 0:
         scale = target_sum / current_sum
         mock_flux.list = [x * scale for x in mock_flux.list]
    elif target_sum > 0:
         mock_flux.list = [float(target_sum)]
    type(mock_flux).ticks_per_rev = unittest.mock.PropertyMock(return_value=float(ticks_per_rev))
    type(mock_flux).time_per_rev = unittest.mock.PropertyMock(return_value=float(ticks_per_rev / sample_freq))
    mock_flux.summary_string.return_value = f"Mock Flux ({len(mock_flux.list)} samples, {ticks_per_rev/sample_freq*1000:.2f}ms/rev)"
    mock_flux.flux.return_value = mock_flux
    return mock_flux

# Helper function to create mock Decoded Track Data
def create_mock_track_data(cyl, head, fmt_def, provide_boot_sector=False):
    mock_dat = MagicMock(spec=RealRawTrack if REAL_GW_AVAILABLE else None)
    mock_dat.track = MagicMock(spec=real_gw_ibm.IBMTrack if REAL_GW_AVAILABLE else None)
    num_sectors = fmt_def.geometry.sectors_per_track
    sector_size = fmt_def.geometry.sector_size
    mock_sectors = []
    for i in range(num_sectors):
        sec_nr = i + 1
        sec = MagicMock()
        sector_n_value = sector_size // 128 # Calculate 'n' for IDAM
        if sector_n_value > 7: sector_n_value = 7 # Clamp n if size is > 8192
        sec.idam = MagicMock(c=cyl, h=head, r=sec_nr, n=sector_n_value)
        sec.crc = 0
        sec.dam = MagicMock()
        if provide_boot_sector and cyl == 0 and head == 0 and sec_nr == 1:
             mock_boot_sector_data = bytearray(sector_size)
             # Populate minimal valid BPB based on fmt_def for testing
             bsd = fmt_def.boot_sector or FLOPPY_FORMATS['ibm_3.5_1.44m'].boot_sector # Fallback needed?
             struct.pack_into('<H', mock_boot_sector_data, 0x0B, bsd.bytes_per_sector)
             mock_boot_sector_data[0x0D] = bsd.sectors_per_cluster
             struct.pack_into('<H', mock_boot_sector_data, 0x0E, bsd.reserved_sectors)
             mock_boot_sector_data[0x10] = bsd.num_fats
             struct.pack_into('<H', mock_boot_sector_data, 0x11, bsd.root_entries)
             total_secs_16 = bsd.total_sectors if bsd.total_sectors < 65536 else 0
             total_secs_32 = bsd.total_sectors if bsd.total_sectors >= 65536 else 0
             struct.pack_into('<H', mock_boot_sector_data, 0x13, total_secs_16)
             struct.pack_into('<I', mock_boot_sector_data, 0x20, total_secs_32)
             struct.pack_into('<B', mock_boot_sector_data, 0x15, bsd.media_descriptor)
             struct.pack_into('<H', mock_boot_sector_data, 0x16, bsd.sectors_per_fat)
             struct.pack_into('<H', mock_boot_sector_data, 0x18, bsd.sectors_per_track)
             struct.pack_into('<H', mock_boot_sector_data, 0x1A, bsd.num_heads)
             struct.pack_into('<I', mock_boot_sector_data, 0x1C, bsd.hidden_sectors)
             struct.pack_into('<H', mock_boot_sector_data, 0x1FE, 0xAA55)
             sec.dam.data = bytes(mock_boot_sector_data)
        else:
             sec.dam.data = bytes([(cyl + head + sec_nr) % 256] * sector_size)
        mock_sectors.append(sec)
    mock_dat.track.sectors = mock_sectors
    mock_dat.nr_missing = MagicMock(return_value=0)
    if hasattr(mock_dat.track, 'mode'): mock_dat.track.mode = "IBM MFM" # Example
    if hasattr(mock_dat.track, 'clock'): mock_dat.track.clock = 2e-6 # Example
    return mock_dat


# Renamed class
class TestDiskControllerPhysicalMocked(unittest.TestCase):

    def setUp(self):
        print(f"\n--- Running Mocked test: {self.id()} ---")
        self.patches = []
        # --- Start Mocks ---
        # Define all targets clearly
        self.patch_targets = [
            'fatfloppy.core.drivers.util.usb_open',
            'fatfloppy.core.drivers.util.Drive',
            'fatfloppy.core.drivers.util.with_drive_selected',
            'fatfloppy.core.drivers.codec.get_diskdef',
            'fatfloppy.core.drivers.read.read_with_retry',
            'fatfloppy.core.drivers.GreaseweazleDriver._create_and_set_custom_diskdef' # Also mock this
        ]
        # Create and store mock objects
        self.mocks = {}
        for target in self.patch_targets:
            patcher = patch(target)
            self.patches.append(patcher)
            try:
                # Store mocks in a dictionary for easier access
                self.mocks[target.split('.')[-1]] = patcher.start()
            except Exception as e:
                 # Clean up already started patches on error
                 while self.patches: self.patches.pop().stop()
                 self.fail(f"Error starting patch for '{target}' in setUp: {e}")

        # Configure mock usb object returned by usb_open
        self.mock_usb = MagicMock(spec=real_gw_usb.Unit if REAL_GW_AVAILABLE else None)
        self.mock_usb.sample_freq = 16000000
        self.mock_usb.hw_model = "MockGW"; self.mock_usb.hw_major = 0; self.mock_usb.hw_minor = 0
        self.mock_usb.max_cmd_len = 64
        self.mocks['usb_open'].return_value = self.mock_usb

        # Configure mock drive object
        self.mock_drive_instance = MagicMock()
        self.mock_drive_instance.unit_id = 0 # Default for A
        self.mock_drive_instance.bus = MagicMock(value='ibm-pc')
        # Make the mocked Drive *class* return a callable, which returns the instance
        self.mocks['Drive'].return_value = MagicMock(return_value=self.mock_drive_instance)

        # Configure mock disk definition
        self.mock_fmt_cls = MagicMock()
        self.mock_fmt_cls.track_map = {}
        def mock_mk_track(cyl, head):
            track = MagicMock(); track.sectors = []; return track
        self.mock_fmt_cls.mk_track = mock_mk_track
        self.mocks['get_diskdef'].return_value = self.mock_fmt_cls

        # Configure default behaviors for mocked USB methods
        self.mock_usb.seek.return_value = None
        self.mock_usb.set_bus_type.return_value = None
        self.mock_usb.drive_select.return_value = None
        self.mock_usb.drive_deselect.return_value = None
        self.mock_usb.drive_motor.return_value = None
        self.mock_usb.get_pin.return_value = False
        self.mock_usb.set_pin.return_value = None
        self.mock_usb.write_track.return_value = None
        self.mock_usb.read_track.return_value = create_mock_flux() # For RPM measure

        # Configure default read_with_retry (can be overridden per test)
        self.mocks['read_with_retry'].return_value = (create_mock_flux(), create_mock_track_data(0, 0, FMT_144))

        # Configure default with_drive_selected (can be overridden per test)
        self.mocks['with_drive_selected'].side_effect = lambda func, usb, drive, motor=False: func()

        # Controller instance
        self.controller = DiskController()


    def tearDown(self):
        # Stop patches in reverse order
        while self.patches:
            patcher = self.patches.pop()
            try:
                patcher.stop()
            except RuntimeError as e:
                if "never started" not in str(e) and "already stopped" not in str(e):
                    print(f"Warning: Error stopping patch {patcher}: {e}")
        # Clean up controller
        if hasattr(self, 'controller') and self.controller:
            try:
                self.controller.close_disk()
            except Exception as e:
                print(f"Ignoring error during disk close in tearDown: {e}")
            del self.controller
        print(f"--- Finished Mocked test: {self.id()} ---")

    # --- Mocked Test Cases ---

    def test_01_open_physical_drive_A_35_auto_detect_mocked(self):
        """(Mocked) Tests opening Drive A (3.5") with auto-detection."""
        drive_letter = 'A'
        drive_size = "3.5"
        expected_format = FMT_144

        # --- Configure Mocks for Auto-Detect ---
        def rpm_side_effect(func, usb, drive, motor=False):
            drive_obj = self.mock_drive_instance # Use setUp instance
            print(f"DEBUG: [with_drive_selected mock RPM] Wrapping func={func.__name__}")
            usb.drive_select(drive_obj.unit_id); usb.drive_motor(drive_obj.unit_id, motor)
            if func.__name__ == 'measure_rpm':
                 controller_usb = self.controller.driver.usb # Get driver's mock usb
                 controller_usb.read_track.return_value = create_mock_flux(ticks_per_rev=3200000)
            result = func()
            usb.drive_motor(drive_obj.unit_id, False); usb.drive_deselect()
            return result
        self.mocks['with_drive_selected'].side_effect = rpm_side_effect

        def mock_read_retry_func(usb, args, track_iter):
            track_str = str(args.tracks); current_cyl = -1; current_head = -1
            try: # Simple parse
                parts = track_str.split(':')
                for part in parts:
                    if part.startswith('c='): current_cyl = int(part[2:])
                    if part.startswith('h='): current_head = int(part[2:])
            except Exception: pass
            print(f"DEBUG: [read_with_retry mock] Called for C:{current_cyl} H:{current_head}")
            if current_cyl == 0 and current_head == 0:
                print("DEBUG: [read_with_retry mock] Simulating SUCCESS for C=0, H=0 (boot sector)")
                return (create_mock_flux(), create_mock_track_data(0, 0, expected_format, provide_boot_sector=True))
            elif current_cyl == 0 and current_head == 1:
                 print("DEBUG: [read_with_retry mock] Simulating SUCCESS for C=0, H=1 (head check)")
                 return (create_mock_flux(), create_mock_track_data(0, 1, expected_format))
            else:
                 print(f"DEBUG: [read_with_retry mock] Simulating GENERIC SUCCESS for C:{current_cyl} H:{current_head}")
                 return (create_mock_flux(), create_mock_track_data(current_cyl, current_head, expected_format))
        self.mocks['read_with_retry'].side_effect = mock_read_retry_func

        # --- Execute Test ---
        success = self.controller.open_disk(source=None, disk_type="physical", drive_letter=drive_letter, drive_size=drive_size)

        # --- Assertions ---
        self.assertTrue(success)
        self.assertIsInstance(self.controller.driver, GreaseweazleDriver)
        self.assertEqual(self.controller.driver.drive_obj, self.mock_drive_instance)
        self.mocks['usb_open'].assert_called_with(None)
        self.mocks['Drive'].assert_called()
        self.assertTrue(any('measure_rpm' in str(call) for call in self.mocks['with_drive_selected'].call_args_list))
        self.assertGreaterEqual(self.mocks['read_with_retry'].call_count, 1)
        self.assertTrue(self.controller.driver.initialized)
        self.assertIsNotNone(self.controller.disk)
        self.assertIsNotNone(self.controller.disk.geometry)
        self.assertIsNotNone(self.controller.filesystem)
        self.assertIsInstance(self.controller.filesystem, FATFilesystem)
        geom = self.controller.disk.geometry
        self.assertEqual(geom.sectors_per_track, expected_format.geometry.sectors_per_track)
        self.assertEqual(geom.heads, expected_format.geometry.heads)
        pf = self.controller.driver.physical_format
        self.assertEqual(pf.sectors_per_track, expected_format.geometry.sectors_per_track)
        self.assertEqual(pf.heads, expected_format.geometry.heads)

    def test_02_open_physical_with_explicit_format_mocked(self):
        """(Mocked) Tests opening with a user-specified format."""
        drive_letter = 'B'
        drive_size = "5.25"
        expected_format = FMT_360
        source_device = "COM3"
        format_info = {**expected_format.geometry.__dict__, **expected_format.physical_format.__dict__}

        # Mock Drive B instance selection
        self.mock_drive_instance.unit_id = 1
        self.mocks['Drive'].return_value = MagicMock(return_value=self.mock_drive_instance)

        # Mock detect_filesystem to succeed
        mock_fs = MagicMock(spec=FATFilesystem); mock_fs.is_valid.return_value = True; mock_fs.fat_type = "FAT12"
        mock_fs.boot_sector = MagicMock(); mock_fs.boot_sector.sectors_per_track = expected_format.geometry.sectors_per_track
        mock_fs.boot_sector.num_heads = expected_format.geometry.heads
        with patch.object(DiskController, 'detect_filesystem', return_value="FAT12") as mock_detect:
             # Configure the mock to set the filesystem attribute when called
             def set_fs_side_effect(*args, **kwargs):
                 # Only set FS if controller has a disk (simulating success)
                 if self.controller.disk:
                     self.controller.filesystem = mock_fs
                 return "FAT12"
             mock_detect.side_effect = set_fs_side_effect

             # --- Execute open_disk ---
             print("DEBUG: Calling controller.open_disk for explicit format...")
             success = self.controller.open_disk(
                 source=source_device, disk_type="physical",
                 drive_letter=drive_letter, drive_size=drive_size,
                 format_info=format_info
             )
             print(f"DEBUG: controller.open_disk returned: {success}")
             self.assertTrue(success, "open_disk should return True when format_info is provided")

             # --- Ensure Driver Initialization ---
             # Explicitly call initialize() because format_info bypasses auto-detection paths
             # Configure mocks needed *by initialize* first
             self.mock_usb.read_track.return_value = create_mock_flux()
             def rpm_side_effect(func, usb, drive, motor=False): func() # Simple call through
             self.mocks['with_drive_selected'].side_effect = rpm_side_effect
             print("DEBUG: Explicitly calling driver.initialize()...")
             self.controller.driver.initialize()
             print("DEBUG: driver.initialize() called.")
             # --- End Initialization ---


             # --- Assertions ---
             self.assertIsInstance(self.controller.driver, GreaseweazleDriver)
             # FIX: Assert these *after* initialize() has run
             self.mocks['Drive'].assert_called() # Drive class accessed
             self.mocks['Drive']().assert_called_with(drive_letter) # Instance creator called

             self.mocks['usb_open'].assert_called_once_with(source_device)
             self.mocks['with_drive_selected'].assert_called() # Called for RPM
             self.mocks['_create_and_set_custom_diskdef'].assert_called_once() # Called due to format_info
             mock_detect.assert_called_once() # detect_filesystem was still called
             self.assertIsNotNone(self.controller.filesystem) # Check filesystem was set by the mock
             pf = self.controller.driver.physical_format
             geom = self.controller.disk.geometry
             self.assertEqual(pf.rate, expected_format.physical_format.rate)
             self.assertEqual(geom.cylinders, expected_format.geometry.cylinders)
             self.assertEqual(geom.sectors_per_track, expected_format.geometry.sectors_per_track)


    def test_03_physical_read_sector_success_mocked(self):
        """(Mocked) Tests reading a single sector successfully."""
        cyl, head, sect = 10, 1, 5
        expected_data = bytes([(cyl + head + sect) % 256] * 512)
        test_format = FMT_144
        format_info_dict = {**test_format.geometry.__dict__, **test_format.physical_format.__dict__}

        # --- Mock Setup ---
        # Ensure drive A is selected
        self.mock_drive_instance.unit_id = 0
        self.mocks['Drive'].return_value = MagicMock(return_value=self.mock_drive_instance)

        self.controller.open_disk(None, "physical", 'A', "3.5", format_info=format_info_dict)

        def mock_read_retry_func(usb, args, track_iter):
            track_str = str(args.tracks); c,h = -1,-1 # Parse C/H
            try:
                parts=track_str.split(':')
                for p in parts:
                    if p.startswith('c='): c=int(p[2:])
                    if p.startswith('h='): h=int(p[2:])
            except: pass
            print(f"DEBUG: [read_with_retry mock] Called for C:{c} H:{h}")
            # Simulate finding the requested sector data
            mock_track = create_mock_track_data(c, h, test_format)
            found = False
            for s in mock_track.track.sectors:
                if s.idam.c == cyl and s.idam.h == head and s.idam.r == sect:
                     s.dam.data = expected_data; found = True; break
            # self.assertTrue(found, f"Test setup error: Sector {sect} not simulated in mock data for track C:{c} H:{h}")
            return (create_mock_flux(), mock_track)
        self.mocks['read_with_retry'].side_effect = mock_read_retry_func
        self.mocks['with_drive_selected'].side_effect = lambda func, usb, drive, motor=False: func() # Simple wrapper for read

        # --- Execute Read ---
        read_data = self.controller.disk.read_sector(cyl, head, sect)

        # --- Assertions ---
        self.mocks['read_with_retry'].assert_called()
        last_call_args = self.mocks['read_with_retry'].call_args[0]
        self.assertIn(f'c={cyl}:h={head}', str(last_call_args[1].tracks))
        self.assertEqual(read_data, expected_data)
        self.assertIn((cyl, head, sect), self.controller.driver.sector_cache)

        # --- Test Cache Hit ---
        self.mocks['read_with_retry'].reset_mock()
        read_data_cached = self.controller.disk.read_sector(cyl, head, sect)
        self.mocks['read_with_retry'].assert_not_called()
        self.assertEqual(read_data_cached, expected_data)


    def test_04_physical_read_sector_not_found_mocked(self):
        """(Mocked) Tests reading a sector that isn't found."""
        cyl, head, sect = 11, 0, 15
        expected_data = b'\x00' * 512
        test_format = FMT_144
        format_info_dict = {**test_format.geometry.__dict__, **test_format.physical_format.__dict__}

        # --- Mock Setup ---
        self.mock_drive_instance.unit_id = 0
        self.mocks['Drive'].return_value = MagicMock(return_value=self.mock_drive_instance)
        self.controller.open_disk(None, "physical", 'A', "3.5", format_info=format_info_dict)

        def mock_read_retry_func(usb, args, track_iter):
             track_str = str(args.tracks); c,h = -1,-1
             try: # Simple parse
                parts=track_str.split(':')
                for p in parts:
                    if p.startswith('c='): c=int(p[2:])
                    if p.startswith('h='): h=int(p[2:])
             except: pass
             print(f"DEBUG: [read_with_retry mock - missing] Called for C:{c} H:{h}")
             mock_track = create_mock_track_data(c, h, test_format)
             # Remove the target sector from the mock data
             mock_track.track.sectors = [s for s in mock_track.track.sectors if not (s.idam.c == cyl and s.idam.h == head and s.idam.r == sect)]
             return (create_mock_flux(), mock_track)
        self.mocks['read_with_retry'].side_effect = mock_read_retry_func
        self.mocks['with_drive_selected'].side_effect = lambda func, usb, drive, motor=False: func()

        # --- Execute Read ---
        read_data = self.controller.disk.read_sector(cyl, head, sect)

        # --- Assertions ---
        self.mocks['read_with_retry'].assert_called()
        self.assertEqual(read_data, expected_data)
        self.assertNotIn((cyl, head, sect), self.controller.driver.sector_cache)
        self.assertIn((cyl, head), self.controller.driver.track_data)
        self.assertNotIn(sect, self.controller.driver.track_data[(cyl, head)])


    def test_05_physical_read_sector_read_error_mocked(self):
        """(Mocked) Tests handling of a hardware read error."""
        cyl, head, sect = 12, 1, 1
        test_format = FMT_144
        format_info_dict = {**test_format.geometry.__dict__, **test_format.physical_format.__dict__}

        # --- Mock Setup ---
        self.mock_drive_instance.unit_id = 0
        self.mocks['Drive'].return_value = MagicMock(return_value=self.mock_drive_instance)
        self.controller.open_disk(None, "physical", 'A', "3.5", format_info=format_info_dict)

        error_to_raise = real_gw_usb.CmdError(cmd=b'', code=real_gw_usb.Ack.NoIndex) if REAL_GW_AVAILABLE else RuntimeError("Mock Read Error")
        self.mocks['read_with_retry'].side_effect = error_to_raise
        self.mocks['with_drive_selected'].side_effect = lambda func, usb, drive, motor=False: func()

        # --- Execute Read ---
        read_data = self.controller.disk.read_sector(cyl, head, sect)

        # --- Assertions ---
        self.mocks['read_with_retry'].assert_called()
        self.assertEqual(read_data, b'\x00' * 512) # Expect zeros on error
        self.assertEqual(self.controller.driver.track_data.get((cyl, head)), {}) # Expect empty dict


    def test_06_physical_write_sector_and_flush_success_mocked(self):
        """(Mocked) Tests writing a sector and flushing successfully."""
        cyl, head, sect = 20, 0, 9
        write_data = b'\xDB' * 512
        test_format = FMT_144
        format_info_dict = {**test_format.geometry.__dict__, **test_format.physical_format.__dict__}

        # --- Mock Setup ---
        self.mock_drive_instance.unit_id = 0
        self.mocks['Drive'].return_value = MagicMock(return_value=self.mock_drive_instance)
        self.controller.open_disk(None, "physical", 'A', "3.5", format_info=format_info_dict)
        self.controller.driver.verify_writes = False # Disable verify for simplicity

        mock_flux_list = [100.0, 200.0, 150.0]
        convert_patch = patch.object(self.controller.driver, '_convert_to_flux', return_value=mock_flux_list)
        mock_convert = convert_patch.start()
        self.addCleanup(convert_patch.stop)

        self.mock_usb.write_track.return_value = None
        self.mock_usb.seek.return_value = None
        # Define side effect for with_drive_selected specific to this test
        def flush_wrapper_side_effect(func, usb, drive, motor=True): func()
        self.mocks['with_drive_selected'].side_effect = flush_wrapper_side_effect

        # --- Execute Write & Flush ---
        self.controller.disk.write_sector(cyl, head, sect, write_data)
        self.controller.driver.flush()

        # --- Assertions ---
        mock_convert.assert_called_once_with(cyl, head)
        self.mocks['with_drive_selected'].assert_called() # Check wrapper called
        self.mock_usb.seek.assert_called_with(cyl, head) # Check calls *inside* wrapper
        self.mock_usb.write_track.assert_called_once_with(flux_list=mock_flux_list, cue_at_index=True, terminate_at_index=True)
        self.assertNotIn((cyl, head), self.controller.driver.dirty_sectors)
        self.assertEqual(len(self.controller.driver.dirty_tracks), 0)
        self.assertNotIn((cyl, head), self.controller.driver.track_data)


    def test_07_physical_flush_write_error_mocked(self):
        """(Mocked) Tests handling a write error during flush."""
        cyl, head, sect = 21, 1, 2
        write_data = b'\xEE' * 512
        test_format = FMT_144
        format_info_dict = {**test_format.geometry.__dict__, **test_format.physical_format.__dict__}

        # --- Mock Setup ---
        self.mock_drive_instance.unit_id = 0
        self.mocks['Drive'].return_value = MagicMock(return_value=self.mock_drive_instance)
        self.controller.open_disk(None, "physical", 'A', "3.5", format_info=format_info_dict)
        self.controller.driver.verify_writes = False

        mock_flux_list = [111.0, 222.0, 111.0]
        convert_patch = patch.object(self.controller.driver, '_convert_to_flux', return_value=mock_flux_list)
        mock_convert = convert_patch.start()
        self.addCleanup(convert_patch.stop)

        error_to_raise = real_gw_usb.CmdError(cmd=b'', code=real_gw_usb.Ack.Wrprot) if REAL_GW_AVAILABLE else RuntimeError("Mock Write Error")
        self.mock_usb.write_track.side_effect = error_to_raise
        self.mock_usb.seek.return_value = None
        # Define side effect for with_drive_selected specific to this test
        def flush_wrapper_side_effect(func, usb, drive, motor=True): func()
        self.mocks['with_drive_selected'].side_effect = flush_wrapper_side_effect


        # --- Execute Write & Flush ---
        self.controller.disk.write_sector(cyl, head, sect, write_data)
        self.controller.driver.flush() # Error should be caught internally by flush

        # --- Assertions ---
        mock_convert.assert_called_once_with(cyl, head)
        self.mocks['with_drive_selected'].assert_called()
        self.mock_usb.seek.assert_called_with(cyl, head)
        self.mock_usb.write_track.assert_called_once() # Write was attempted
        self.assertIn((cyl, head), self.controller.driver.dirty_sectors) # Should remain dirty
        self.assertIn((cyl, head), self.controller.driver.dirty_tracks) # Should remain dirty


    def test_09_physical_flush_partial_track_reads_first_mocked(self):
        """(Mocked) Tests flush reads track data first if track is partially dirty."""
        cyl, head, sect = 25, 1, 4
        write_data = b'\xCC' * 512
        test_format = FMT_144
        format_info_dict = {**test_format.geometry.__dict__, **test_format.physical_format.__dict__}

        # --- Mock Setup ---
        self.mock_drive_instance.unit_id = 0
        self.mocks['Drive'].return_value = MagicMock(return_value=self.mock_drive_instance)
        self.controller.open_disk(None, "physical", 'A', "3.5", format_info=format_info_dict)
        self.controller.driver.verify_writes = False

        mock_flux_list = [300.0, 100.0, 250.0]
        convert_patch = patch.object(self.controller.driver, '_convert_to_flux', return_value=mock_flux_list)
        mock_convert = convert_patch.start()
        self.addCleanup(convert_patch.stop)

        self.mock_usb.write_track.return_value = None
        self.mock_usb.seek.return_value = None

        # Mock read_with_retry to check it's called during flush
        mock_read_retry = MagicMock(return_value=(create_mock_flux(), create_mock_track_data(cyl, head, test_format)))
        self.mocks['read_with_retry'].side_effect = mock_read_retry # Assign mock

        # Configure with_drive_selected for flush
        def flush_wrapper_side_effect(func, usb, drive, motor=True): func()
        self.mocks['with_drive_selected'].side_effect = flush_wrapper_side_effect

        # --- Execute Write & Flush ---
        self.controller.disk.write_sector(cyl, head, sect, write_data) # Partial track dirty
        self.controller.driver.flush()

        # --- Assertions ---
        mock_read_retry.assert_called_once() # read_with_retry should be called for pre-read
        call_args = mock_read_retry.call_args[0]
        self.assertIn(f'c={cyl}:h={head}', str(call_args[1].tracks))
        mock_convert.assert_called_once_with(cyl, head)
        self.mocks['with_drive_selected'].assert_called()
        self.mock_usb.seek.assert_called_once_with(cyl, head)
        self.mock_usb.write_track.assert_called_once_with(flux_list=mock_flux_list, cue_at_index=True, terminate_at_index=True)
        self.assertNotIn((cyl, head), self.controller.driver.dirty_sectors)
        self.assertEqual(len(self.controller.driver.dirty_tracks), 0)


    def test_10_gw_cache_invalidation_mocked(self):
        """(Mocked) Tests track_data cache is invalidated after flush."""
        cyl, head, sect = 22, 0, 1
        write_data = b'\xAA' * 512
        test_format = FMT_144
        format_info_dict = {**test_format.geometry.__dict__, **test_format.physical_format.__dict__}

        # --- Mock Setup ---
        self.mock_drive_instance.unit_id = 0
        self.mocks['Drive'].return_value = MagicMock(return_value=self.mock_drive_instance)
        self.controller.open_disk(None, "physical", 'A', "3.5", format_info=format_info_dict)

        # 1. Read to populate cache
        mock_read_retry_1 = MagicMock(return_value=(create_mock_flux(), create_mock_track_data(cyl, head, test_format)))
        self.mocks['read_with_retry'].side_effect = mock_read_retry_1
        self.mocks['with_drive_selected'].side_effect = lambda func, usb, drive, motor=False: func() # Read wrapper
        self.controller.disk.read_sector(cyl, head, sect)
        self.assertIn((cyl, head), self.controller.driver.track_data)
        mock_read_retry_1.assert_called_once()

        # 2. Write & Flush
        self.controller.disk.write_sector(cyl, head, sect, write_data)
        # Mocks for flush
        convert_patch = patch.object(self.controller.driver, '_convert_to_flux', return_value=[1.0])
        mock_convert = convert_patch.start()
        self.addCleanup(convert_patch.stop)
        self.mock_usb.seek.return_value = None
        self.mock_usb.write_track.return_value = None
        # Mock read_with_retry *for the flush pre-read*
        mock_read_retry_flush = MagicMock(return_value=(create_mock_flux(), create_mock_track_data(cyl, head, test_format)))
        self.mocks['read_with_retry'].side_effect = mock_read_retry_flush
        # Flush wrapper
        def flush_wrapper_side_effect(func, usb, drive, motor=True): func()
        self.mocks['with_drive_selected'].side_effect = flush_wrapper_side_effect
        self.controller.driver.flush()

        # Assert read happened during flush
        mock_read_retry_flush.assert_called_once()

        # 3. Assert cache invalidated
        self.assertNotIn((cyl, head), self.controller.driver.track_data)

        # 4. Read again, verify physical read happens
        mock_read_retry_2 = MagicMock(return_value=(create_mock_flux(), create_mock_track_data(cyl, head, test_format)))
        self.mocks['read_with_retry'].side_effect = mock_read_retry_2
        self.mocks['with_drive_selected'].side_effect = lambda func, usb, drive, motor=False: func() # Read wrapper again
        self.controller.disk.read_sector(cyl, head, sect)
        mock_read_retry_2.assert_called_once()


    def test_11_gw_write_verify_success_mocked(self):
        """(Mocked) Tests successful write verification path."""
        cyl, head, sect = 23, 1, 7
        write_data = b'\xBB' * 512
        test_format = FMT_144
        format_info_dict = {**test_format.geometry.__dict__, **test_format.physical_format.__dict__}

        # --- Mock Setup ---
        self.mock_drive_instance.unit_id = 0
        self.mocks['Drive'].return_value = MagicMock(return_value=self.mock_drive_instance)
        self.controller.open_disk(None, "physical", 'A', "3.5", format_info=format_info_dict)
        self.controller.driver.verify_writes = True # Enable verification

        convert_patch = patch.object(self.controller.driver, '_convert_to_flux', return_value=[1.0])
        mock_convert = convert_patch.start()
        self.addCleanup(convert_patch.stop)

        self.mock_usb.seek.return_value = None
        self.mock_usb.write_track.return_value = None
        # Mock read_with_retry for the verification read
        mock_read_retry = MagicMock(return_value=(create_mock_flux(), create_mock_track_data(cyl, head, test_format)))
        self.mocks['read_with_retry'].side_effect = mock_read_retry
        # Mock with_drive_selected for both write and verify read steps
        # This side effect needs to call the function passed to it.
        def wrapper_verify_side_effect(func, usb, drive, motor=True): func()
        self.mocks['with_drive_selected'].side_effect = wrapper_verify_side_effect

        # --- Execute Write & Flush ---
        self.controller.disk.write_sector(cyl, head, sect, write_data)
        self.controller.driver.flush() # This will call write_track and then _read_track for verify

        # --- Assertions ---
        self.mock_usb.seek.assert_called_with(cyl, head) # Called for write
        self.mock_usb.write_track.assert_called_once()
        mock_read_retry.assert_called_once() # Called for verify
        self.assertNotIn((cyl, head), self.controller.driver.dirty_sectors) # Cleared on success


if __name__ == '__main__':
    print("** Running Mocked Physical Controller Tests **")
    unittest.main()
