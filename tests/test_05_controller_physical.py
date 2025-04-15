# tests/test_05_controller_physical.py
import unittest
from unittest.mock import patch, MagicMock, ANY
import os
import sys
import time # For potential waits in real HW tests
import struct # For packing/unpacking mock data
import subprocess # Needed for running gw command
import tempfile   # Needed for temporary image files
import types      # Needed for SimpleNamespace in mock read

# Ensure src is in path or install the package
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src')))

from fatfloppy.core.controller import DiskController
from fatfloppy.core.drivers import GreaseweazleDriver, PhysicalFormat
from fatfloppy.core.filesystem import FATFilesystem
from fatfloppy.core.disk import DiskGeometry
from fatfloppy.core.format_definitions import FLOPPY_FORMATS

# Attempt to import real Greaseweazle components for realistic mocking/typing
try:
    # Import specific Greaseweazle components needed for mocking structure/constants
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
    class DummyAck:
        Okay = 0; BadCommand = 1; NoIndex = 2; NoTrk0 = 3; FluxOverflow = 4
        FluxUnderflow = 5; Wrprot = 6; NoUnit = 7; NoBus = 8; BadUnit = 9
        BadPin = 10; BadCylinder = 11
    class DummyCmd: GetInfo = 0; Seek = 2; Head = 3; ReadFlux = 7; WriteFlux = 8; GetFluxStatus = 9
    real_gw_usb = MagicMock() # Use MagicMock as placeholder type
    real_gw_usb.CmdError = DummyCmdError
    real_gw_usb.Ack = DummyAck
    real_gw_usb.Cmd = DummyCmd
    RealFlux = MagicMock
    RealWriteoutFlux = MagicMock
    real_gw_codec = MagicMock()
    real_gw_ibm = MagicMock()
    RealMasterTrack = MagicMock
    RealRawTrack = MagicMock
    real_gw_util = MagicMock()
    real_gw_read = MagicMock()


# --- Test Configuration ---
USE_REAL_HARDWARE = os.getenv('TEST_HW', 'false').lower() == 'true'
REAL_HW_DRIVE_A = 'A' # Assume 3.5" 1.44MB
REAL_HW_DRIVE_B = 'B' # Assume 5.25" 360KB
FMT_144 = FLOPPY_FORMATS['ibm_3.5_1.44m']
FMT_360 = FLOPPY_FORMATS['ibm_5.25_360k']

# Helper function to create a mock Flux object
def create_mock_flux(sample_freq=16000000, ticks_per_rev=3200000, revs=2):
    mock_flux = MagicMock(spec=RealFlux if REAL_GW_AVAILABLE else None)
    mock_flux.sample_freq = sample_freq
    # Store the intended raw value if needed, PropertyMock handles access
    # mock_flux._ticks_per_rev = ticks_per_rev
    mock_flux.index_list = [float(ticks_per_rev)] * revs
    # Create plausible flux timings
    avg_interval = 4e-6 * sample_freq # ~64 ticks at 16MHz for MFM 500kbps
    num_fluxes = int((ticks_per_rev * revs) / avg_interval) if avg_interval > 0 else 0
    mock_flux.list = [float(avg_interval + (-1)**i * avg_interval * 0.1) for i in range(num_fluxes)]
    # Adjust sum (handle potential division by zero if current_sum is 0)
    current_sum = sum(mock_flux.list)
    target_sum = ticks_per_rev * revs
    if current_sum > 0:
         scale = target_sum / current_sum
         mock_flux.list = [x * scale for x in mock_flux.list]
    elif target_sum > 0:
         # If no fluxes generated but time should have passed, add a single large interval? Or handle as error?
         mock_flux.list = [float(target_sum)] # Simplistic fallback

    # Add property getter for ticks_per_rev
    type(mock_flux).ticks_per_rev = unittest.mock.PropertyMock(return_value=float(ticks_per_rev))
    type(mock_flux).time_per_rev = unittest.mock.PropertyMock(return_value=float(ticks_per_rev / sample_freq))
    mock_flux.summary_string.return_value = f"Mock Flux ({len(mock_flux.list)} samples, {ticks_per_rev/sample_freq*1000:.2f}ms/rev)"
    mock_flux.flux.return_value = mock_flux # Return self for .flux() calls
    return mock_flux

# Helper function to create mock Decoded Track Data ('dat' object returned by read_with_retry)
# Needs more structure to mimic the real 'dat' object (often a RawTrack or IBMTrack instance)
def create_mock_track_data(cyl, head, fmt_def, provide_boot_sector=False):
    # Decide the type of track object to mock based on format or detection type
    # For simplicity, let's use a generic mock but add attributes expected by the driver
    mock_dat = MagicMock(spec=RealRawTrack if REAL_GW_AVAILABLE else None) # RawTrack is a common container

    # Add 'track' attribute which might hold IBMTrack specific details
    mock_dat.track = MagicMock(spec=real_gw_ibm.IBMTrack if REAL_GW_AVAILABLE else None)

    # Simulate sectors found on the track
    num_sectors = fmt_def.geometry.sectors_per_track
    sector_size = fmt_def.geometry.sector_size
    mock_sectors = []
    for i in range(num_sectors):
        sec_nr = i + 1 # Usually 1-based sector numbers in IDAM
        # Mock the sector object structure found in dat.track.sectors
        sec = MagicMock()
        sec.idam = MagicMock(c=cyl, h=head, r=sec_nr, n=fmt_def.physical_format.sector_size // 128) # n based on size
        sec.crc = 0 # Assume good read initially
        sec.dam = MagicMock()
        # Provide default empty data or specific data if needed
        if provide_boot_sector and cyl == 0 and head == 0 and sec_nr == 1:
             # Create mock boot sector data
             mock_boot_sector_data = bytearray(sector_size)
             struct.pack_into('<H', mock_boot_sector_data, 0x00B, sector_size)
             mock_boot_sector_data[0x00D] = 1 # sectors_per_cluster
             struct.pack_into('<H', mock_boot_sector_data, 0x00E, 1) # reserved_sectors
             mock_boot_sector_data[0x010] = 2 # num_fats
             struct.pack_into('<H', mock_boot_sector_data, 0x011, 224) # root_entries (for 1.44)
             # Need total sectors calculation based on fmt_def geometry
             total_sectors = fmt_def.geometry.total_sectors
             if total_sectors < 65536:
                 struct.pack_into('<H', mock_boot_sector_data, 0x013, total_sectors)
                 struct.pack_into('<I', mock_boot_sector_data, 0x020, 0)
             else:
                 struct.pack_into('<H', mock_boot_sector_data, 0x013, 0)
                 struct.pack_into('<I', mock_boot_sector_data, 0x020, total_sectors)

             media_desc = fmt_def.media_descriptor
             sectors_per_fat = fmt_def.boot_sector.sectors_per_fat if fmt_def.boot_sector else 9 # Default for 1.44MB
             struct.pack_into('<B', mock_boot_sector_data, 0x015, media_desc)
             struct.pack_into('<H', mock_boot_sector_data, 0x016, sectors_per_fat)
             struct.pack_into('<H', mock_boot_sector_data, 0x018, fmt_def.geometry.sectors_per_track)
             struct.pack_into('<H', mock_boot_sector_data, 0x01A, fmt_def.geometry.heads)
             struct.pack_into('<I', mock_boot_sector_data, 0x01C, 0) # hidden_sectors
             struct.pack_into('<H', mock_boot_sector_data, 0x1FE, 0xAA55) # Boot sig
             sec.dam.data = bytes(mock_boot_sector_data)
             print(f"DEBUG: [Mock Track] Provided mock boot sector data for C:{cyl} H:{head} S:{sec_nr}")
        else:
             # Provide default data for other sectors
             sec.dam.data = bytes([sec_nr % 256] * sector_size) # Just some identifiable data

        mock_sectors.append(sec)

    # Assign sectors to the correct place (usually dat.track.sectors)
    mock_dat.track.sectors = mock_sectors

    # Add other attributes that might be checked
    mock_dat.nr_missing = MagicMock(return_value=0) # Simulate no missing sectors

    # Simulate track properties if needed (e.g., for ibm.scan detection update)
    if hasattr(mock_dat.track, 'mode'):
         mock_dat.track.mode = "IBM MFM" if fmt_def.physical_format.encoding == "MFM" else "IBM FM"
    if hasattr(mock_dat.track, 'clock'):
        # Calculate clock based on rate (MFM: clock = 1 / (rate_kbps * 2000), FM: clock = 1 / (rate_kbps * 1000))
        rate_kbps = fmt_def.physical_format.rate
        if fmt_def.physical_format.encoding == "MFM":
             mock_dat.track.clock = 1.0 / (rate_kbps * 2000.0) if rate_kbps else 0
        else:
             mock_dat.track.clock = 1.0 / (rate_kbps * 1000.0) if rate_kbps else 0

    return mock_dat


@unittest.skipIf(USE_REAL_HARDWARE and not REAL_GW_AVAILABLE, "Real Greaseweazle library needed for hardware tests")
class TestDiskControllerPhysical(unittest.TestCase):

    def setUp(self):
        print(f"\n--- Running test: {self.id()} ---")
        self.using_real_hardware = USE_REAL_HARDWARE
        self.patches = []

        if self.using_real_hardware:
            print("INFO: Running with REAL Greaseweazle hardware.")
            self.controller = DiskController()
        else:
            print("INFO: Running with MOCKED Greaseweazle hardware.")
            # --- Start Mocks ---
            patch_targets = [
                'fatfloppy.core.drivers.util.usb_open',
                'fatfloppy.core.drivers.util.Drive',
                'fatfloppy.core.drivers.util.with_drive_selected',
                'fatfloppy.core.drivers.codec.get_diskdef',
                'fatfloppy.core.drivers.read.read_with_retry',
            ]
            for target in patch_targets:
                # ***** FIX: Remove autospec=True *****
                patcher = patch(target)
                self.patches.append(patcher)
                try:
                    setattr(self, f"mock_{target.split('.')[-1]}", patcher.start())
                except Exception as e:
                     patch.stopall()
                     self.fail(f"Error starting patch for '{target}' in setUp: {e}")


            # Configure mock usb object returned by usb_open
            if hasattr(self, 'mock_usb_open'):
                self.mock_usb = MagicMock(spec=real_gw_usb.Unit if REAL_GW_AVAILABLE else None) # Use spec for better type hinting
                self.mock_usb.sample_freq = 16000000 # Example frequency
                # Add necessary attributes if spec isn't perfect
                if not hasattr(self.mock_usb, 'hw_model'): self.mock_usb.hw_model = "MockGW"
                if not hasattr(self.mock_usb, 'hw_major'): self.mock_usb.hw_major = 0
                if not hasattr(self.mock_usb, 'hw_minor'): self.mock_usb.hw_minor = 0
                if not hasattr(self.mock_usb, 'max_cmd_len'): self.mock_usb.max_cmd_len= 64
                self.mock_usb_open.return_value = self.mock_usb
            else:
                 self.fail("mock_usb_open was not created during patching.")

            # Configure mock drive object
            if hasattr(self, 'mock_Drive'):
                # Create a mock Drive *instance* first
                self.mock_drive_instance = MagicMock() # spec=real_gw_util.Drive if REAL_GW_AVAILABLE else None causes issues
                # Set attributes expected by the driver (like unit_id, bus)
                self.mock_drive_instance.unit_id = 0 # Default for drive A
                self.mock_drive_instance.bus = MagicMock(value='ibm-pc') # Mock bus object if needed
                # Make the mocked Drive *class* return a callable, which returns the instance
                self.mock_Drive_callable = MagicMock(return_value=self.mock_drive_instance)
                self.mock_Drive.return_value = self.mock_Drive_callable # Drive() returns the callable
            else:
                 self.fail("mock_Drive was not created during patching.")


            # Configure mock disk definition
            if hasattr(self, 'mock_get_diskdef'):
                # Mock the return value for get_diskdef
                # We need a mock that can have track_map assigned etc.
                self.mock_fmt_cls = MagicMock() # spec=real_gw_codec.DiskDef if REAL_GW_AVAILABLE else None
                self.mock_fmt_cls.track_map = {} # Need a dict for track_map
                # If code checks for specific types, mock that too
                # self.mock_fmt_cls.__class__ = real_gw_codec.DiskDef if REAL_GW_AVAILABLE else MagicMock
                # Add a mk_track method if needed by _convert_to_flux
                def mock_mk_track(cyl, head):
                    track = MagicMock()
                    track.sectors = [] # Add sectors based on format if needed
                    return track
                self.mock_fmt_cls.mk_track = mock_mk_track
                self.mock_get_diskdef.return_value = self.mock_fmt_cls
            else:
                 self.fail("mock_get_diskdef was not created during patching.")

            # Configure default behaviors for mocked USB methods
            if hasattr(self, 'mock_usb'):
                self.mock_usb.seek.return_value = None
                self.mock_usb.set_bus_type.return_value = None
                self.mock_usb.drive_select.return_value = None
                self.mock_usb.drive_deselect.return_value = None
                self.mock_usb.drive_motor.return_value = None
                self.mock_usb.get_pin.return_value = False
                self.mock_usb.set_pin.return_value = None
                self.mock_usb.write_track.return_value = None
                # Default read_track for RPM measurement
                self.mock_usb.read_track.return_value = create_mock_flux()

            # Configure read_with_retry mock (use function side_effect later in tests)
            if hasattr(self, 'mock_read_with_retry'):
                 # Set a default simple return value; tests will override side_effect
                 self.mock_read_with_retry.return_value = (create_mock_flux(), create_mock_track_data(0, 0, FMT_144))
            else:
                 self.fail("mock_read_with_retry was not created during patching.")

            # Configure with_drive_selected mock (use function side_effect later in tests)
            if hasattr(self, 'mock_with_drive_selected'):
                # Default side effect just calls the function
                self.mock_with_drive_selected.side_effect = lambda func, usb, drive, motor=False: func()
            else:
                 self.fail("mock_with_drive_selected was not created during patching.")

            self.controller = DiskController()


    def tearDown(self):
        if not self.using_real_hardware:
            # Stop patches in reverse order of starting is generally safer
            while self.patches:
                patcher = self.patches.pop()
                try:
                    patcher.stop()
                except RuntimeError as e:
                    # Ignore "patch not active" errors during teardown
                    if "never started" not in str(e) and "already stopped" not in str(e):
                        print(f"Warning: Error stopping patch {patcher}: {e}")
        # Ensure disk is closed
        if hasattr(self, 'controller') and self.controller:
            print("Tearing down test, closing disk...")
            try:
                self.controller.close_disk()
            except Exception as e:
                print(f"Ignoring error during disk close in tearDown: {e}")
            del self.controller
        if self.using_real_hardware:
            time.sleep(0.1)
        print(f"--- Finished test: {self.id()} ---")


    # --- Test Cases ---

    def test_01_open_physical_drive_A_35_auto_detect(self):
        """Tests opening Drive A (3.5") with auto-detection."""
        drive_letter = REAL_HW_DRIVE_A
        drive_size = "3.5"
        expected_format = FMT_144 # 1.44MB for Drive A

        if self.using_real_hardware:
            # Real hardware test assumes disk is prepared with 1.44MB format
            success = self.controller.open_disk(source=None, disk_type="physical", drive_letter=drive_letter, drive_size=drive_size)
            self.assertTrue(success, "Failed to open real Drive A")
            self.assertIsInstance(self.controller.driver, GreaseweazleDriver)
            self.assertEqual(self.controller.driver.drive, drive_letter)
            # Format detection check
            self.assertIsNotNone(self.controller.filesystem, "Filesystem should be detected on real Drive A")
            geom = self.controller.disk.geometry
            self.assertEqual(geom.sectors_per_track, expected_format.geometry.sectors_per_track)
            self.assertEqual(geom.heads, expected_format.geometry.heads)

        else: # Mocked test
            # --- Configure Mocks for Auto-Detect ---

            # 1. Initialize (RPM measurement)
            def rpm_side_effect(func, usb, drive, motor=False): # Default motor to False as per util
                # Use the drive instance configured in setUp
                drive_obj = self.mock_drive_instance
                print(f"DEBUG: [with_drive_selected] Wrapping func={func.__name__}, motor={motor}, drive_unit={drive_obj.unit_id}")
                try:
                    usb.drive_select(drive_obj.unit_id)
                    usb.drive_motor(drive_obj.unit_id, motor)
                    if func.__name__ == 'measure_rpm':
                        print("DEBUG: [measure_rpm] Simulating read_track for RPM")
                        # Use the correct mock_usb instance associated with the controller's driver
                        controller_usb = self.controller.driver.usb
                        controller_usb.read_track.return_value = create_mock_flux(ticks_per_rev=3200000) # ~300 RPM at 16MHz
                    result = func()
                    print(f"DEBUG: [with_drive_selected] Func {func.__name__} executed.")
                    return result
                finally:
                     print(f"DEBUG: [with_drive_selected] Cleaning up motor/select for {func.__name__}")
                     usb.drive_motor(drive_obj.unit_id, False)
                     usb.drive_deselect()
            self.mock_with_drive_selected.side_effect = rpm_side_effect

            # 2. Mock read_with_retry with a function side_effect
            def mock_read_retry_func(usb, args, track_iter):
                # In auto-detect, _read_track is called multiple times.
                # First for boot sector check (C=0, H=0) by detect_filesystem
                # Then for head 1 check (C=0, H=1) by _detect_physical_disk_format
                # Then potentially again inside the format loop in _detect_physical_disk_format
                # We need to simulate success for the expected 1.44MB format.

                # Extract C/H from args.tracks (TrackSet string like 'c=0:h=0')
                # A simple approach for this test: assume call order or parse string
                track_str = str(args.tracks) # Should be like 'c=X:h=Y'
                current_cyl = 0
                current_head = 0
                try:
                    parts = track_str.split(':')
                    for part in parts:
                        if part.startswith('c='): current_cyl = int(part[2:])
                        if part.startswith('h='): current_head = int(part[2:])
                except Exception:
                     print(f"Warning: Could not parse C/H from track_str: {track_str}")

                call_num = self.mock_read_with_retry.call_count
                print(f"DEBUG: [read_with_retry] Called ({call_num}) for C:{current_cyl} H:{current_head}. Simulating read...")

                # Simulate based on expected calls in _detect_physical_disk_format
                if current_cyl == 0 and current_head == 0: # First call, likely for boot sector C=0, H=0
                    print("DEBUG: [read_with_retry] Simulating SUCCESS for C=0, H=0 (boot sector)")
                    # Provide valid boot sector data within the mock track data
                    return (create_mock_flux(), create_mock_track_data(0, 0, expected_format, provide_boot_sector=True))
                elif current_cyl == 0 and current_head == 1: # Second call, likely for head check C=0, H=1
                     print("DEBUG: [read_with_retry] Simulating SUCCESS for C=0, H=1 (head check)")
                     # Need to provide some valid data for H=1 as well
                     return (create_mock_flux(), create_mock_track_data(0, 1, expected_format))
                else: # Subsequent calls if the loop runs
                     print(f"DEBUG: [read_with_retry] Simulating GENERIC SUCCESS for call {call_num} C:{current_cyl} H:{current_head}")
                     # Return generic success to allow format loop to potentially succeed
                     return (create_mock_flux(), create_mock_track_data(current_cyl, current_head, expected_format))

            self.mock_read_with_retry.side_effect = mock_read_retry_func

            # --- Execute Test ---
            print("DEBUG: Calling controller.open_disk...")
            success = self.controller.open_disk(source=None, disk_type="physical", drive_letter=drive_letter, drive_size=drive_size)
            print(f"DEBUG: controller.open_disk returned: {success}")
            print(f"DEBUG: Filesystem after open_disk: {self.controller.filesystem}")
            print(f"DEBUG: Disk geometry after open_disk: {self.controller.disk.geometry if self.controller.disk else 'No Disk'}")
            print(f"DEBUG: Driver physical format after open_disk: {self.controller.driver.physical_format if self.controller.driver else 'No Driver'}")

            # --- Assertions ---
            self.assertTrue(success)
            self.assertIsInstance(self.controller.driver, GreaseweazleDriver)
            # Check the drive *instance* associated with the driver matches the one we configured
            self.assertEqual(self.controller.driver.drive_obj, self.mock_drive_instance)

            self.mock_usb_open.assert_called_with(None)
            self.mock_Drive.assert_called() # Check Drive class was called

            # Check RPM measurement was attempted via with_drive_selected
            self.assertTrue(any(
                call.args and len(call.args) > 0 and hasattr(call.args[0], '__name__') and call.args[0].__name__ == 'measure_rpm'
                for call in self.mock_with_drive_selected.call_args_list
            ), "measure_rpm was not called via with_drive_selected")

            # Check read_with_retry was called (at least for boot sector and head check)
            self.assertGreaterEqual(self.mock_read_with_retry.call_count, 1) # At least 1 call (boot sector)

            self.assertTrue(self.controller.driver.initialized)
            self.assertIsNotNone(self.controller.disk)
            self.assertIsNotNone(self.controller.disk.geometry)

            # *** The Key Assertion ***
            # Now that we are mocking the underlying reads, the actual detect_filesystem should run and succeed
            self.assertIsNotNone(self.controller.filesystem, "Filesystem should have been detected by the actual detect_filesystem method using mocked reads")
            self.assertIsInstance(self.controller.filesystem, FATFilesystem) # Check type

            # Verify geometry potentially updated by detect_filesystem based on mocked BPB
            geom = self.controller.disk.geometry
            self.assertEqual(geom.sectors_per_track, expected_format.geometry.sectors_per_track)
            self.assertEqual(geom.heads, expected_format.geometry.heads)
            # Check physical format reflects the detected geometry/BPB
            pf = self.controller.driver.physical_format
            self.assertEqual(pf.sectors_per_track, expected_format.geometry.sectors_per_track)
            self.assertEqual(pf.heads, expected_format.geometry.heads)


    def test_02_open_physical_with_explicit_format(self):
        """Tests opening with a user-specified format, bypassing auto-detect."""
        drive_letter = REAL_HW_DRIVE_B
        drive_size = "5.25"
        expected_format = FMT_360 # Use 360KB format for Drive B
        source_device = "COM3" if not self.using_real_hardware else None # Mock COM3, use default for real

        format_info = { # Based on FMT_360
            "encoding": expected_format.physical_format.encoding,
            "rate": expected_format.physical_format.rate,
            "rpm": expected_format.physical_format.rpm,
            "gap3": expected_format.physical_format.gap3,
            "cskew": expected_format.physical_format.cskew, # Include all params
            "interleave": expected_format.physical_format.interleave,
            "sectors_per_track": expected_format.geometry.sectors_per_track,
            "heads": expected_format.geometry.heads,
            "sector_size": expected_format.geometry.sector_size,
            "cylinders": expected_format.geometry.cylinders
        }

        if self.using_real_hardware:
            # Real hardware test assumes drive B has a 360KB formatted disk
            success = self.controller.open_disk(
                source=source_device, disk_type="physical",
                drive_letter=drive_letter, drive_size=drive_size,
                format_info=format_info
            )
            self.assertTrue(success, "Failed to open real Drive B with explicit format")
            # Filesystem detection might still fail if disk isn't perfect, but opening should succeed
            # self.assertIsNotNone(self.controller.filesystem, "Filesystem should be detected with explicit format on real Drive B")
            # Verify format applied
            pf = self.controller.driver.physical_format
            geom = self.controller.disk.geometry
            self.assertEqual(pf.rate, expected_format.physical_format.rate)
            self.assertEqual(geom.cylinders, expected_format.geometry.cylinders)

        else: # Mocked test
            # Mock the Drive instance specifically for drive B if needed
            self.mock_drive_instance.unit_id = 1 # Drive B is typically unit 1

            # Mock filesystem detection to succeed
            with patch.object(DiskController, 'detect_filesystem') as mock_detect_fs_patch:
                 def mock_detect_fs_explicit_inner(*args, **kwargs):
                     print("DEBUG: [detect_filesystem mock side_effect] Called.")
                     mock_fs = MagicMock(spec=FATFilesystem)
                     # ... (set attributes on mock_fs) ...
                     mock_fs.is_valid.return_value = True
                     mock_fs.fat_type = "FAT12"
                     mock_fs.boot_sector = MagicMock()
                     mock_fs.boot_sector.sectors_per_track = expected_format.geometry.sectors_per_track
                     mock_fs.boot_sector.num_heads = expected_format.geometry.heads
                     self.controller.filesystem = mock_fs
                     print(f"DEBUG: Set self.controller.filesystem to {self.controller.filesystem}")
                     return "FAT12"
                 mock_detect_fs_patch.side_effect = mock_detect_fs_explicit_inner

                 # Mock _read_track (needed by detect_filesystem if it *wasn't* patched)
                 # Although detect_filesystem is patched, let's keep a basic mock for read_with_retry
                 def mock_read_retry_explicit(usb, args, track_iter):
                      print("DEBUG: [read_with_retry mock] Called during explicit format test.")
                      return (create_mock_flux(), create_mock_track_data(0, 0, expected_format, provide_boot_sector=True))
                 self.mock_read_with_retry.side_effect = mock_read_retry_explicit

                 # Mock _create_and_set_custom_diskdef
                 with patch.object(GreaseweazleDriver, '_create_and_set_custom_diskdef') as mock_create_custom_observer:
                    print("DEBUG: Calling controller.open_disk for explicit format...")
                    success = self.controller.open_disk(
                        source=source_device, disk_type="physical",
                        drive_letter=drive_letter, drive_size=drive_size,
                        format_info=format_info
                    )
                    print(f"DEBUG: controller.open_disk returned: {success}")

                    # *** ASSERTION THAT FAILED ***
                    self.assertTrue(success, "open_disk should return True when format_info is provided")

                    # --- FIX: Explicitly call initialize() and configure its mocks ---
                    print("DEBUG: Explicitly calling driver.initialize()...")
                    # Configure with_drive_selected for the RPM measurement inside initialize
                    def rpm_side_effect(func, usb, drive, motor=False):
                         drive_obj = self.mock_drive_instance # Use configured instance
                         print(f"DEBUG: [with_drive_selected init] Wrapping func={func.__name__}, motor={motor}, drive_unit={drive_obj.unit_id}")
                         try:
                             usb.drive_select(drive_obj.unit_id)
                             usb.drive_motor(drive_obj.unit_id, motor)
                             if func.__name__ == 'measure_rpm':
                                 print("DEBUG: [measure_rpm] Simulating read_track for RPM")
                                 controller_usb = self.controller.driver.usb # Use the driver's usb mock
                                 controller_usb.read_track.return_value = create_mock_flux(ticks_per_rev=3200000)
                             result = func()
                             return result
                         finally:
                              usb.drive_motor(drive_obj.unit_id, False)
                              usb.drive_deselect()
                    self.mock_with_drive_selected.side_effect = rpm_side_effect
                    # Call initialize
                    self.controller.driver.initialize()
                    print("DEBUG: driver.initialize() called.")
                    # -------------------------------------------------------------

                    # Now assert that usb_open was called during initialize()
                    self.mock_usb_open.assert_called_once_with(source_device)

                    # Assertions continued...
                    self.assertIsInstance(self.controller.driver, GreaseweazleDriver)
                    self.mock_Drive_callable.assert_called_once_with(drive_letter)
                    mock_create_custom_observer.assert_called_once() # Check custom def creation was called

                    pf = self.controller.driver.physical_format
                    geom = self.controller.disk.geometry
                    self.assertEqual(pf.encoding, format_info['encoding'])
                    # ... (other format/geometry checks remain the same) ...
                    self.assertEqual(geom.sectors_per_track, format_info['sectors_per_track'])

                    mock_detect_fs_patch.assert_called_once() # detect_filesystem was still called by open_disk
                    self.assertIsNotNone(self.controller.filesystem) # Check filesystem was set by the mock


    def test_03_physical_read_sector_success(self):
        """Tests reading a single sector successfully."""
        cyl, head, sect = 10, 1, 5
        expected_data = bytes([(cyl + head + sect) % 256] * 512) # Use formula from mock_track_data
        test_format = FMT_144 # Use 1.44MB for this test

        if self.using_real_hardware:
            # Assumes Drive A is prepared
            success = self.controller.open_disk(None, "physical", REAL_HW_DRIVE_A, "3.5")
            self.assertTrue(success)
            # We can't know the *exact* data, but we expect 512 bytes
            read_data = self.controller.disk.read_sector(cyl, head, sect)
            self.assertEqual(len(read_data), 512)
        else:
            # --- Mock Setup ---
            # Open with explicit format to simplify setup
            format_info_dict = {**test_format.geometry.__dict__, **test_format.physical_format.__dict__}
            self.controller.open_disk(None, "physical", REAL_HW_DRIVE_A, "3.5", format_info=format_info_dict)

            # Mock the driver's _read_track mechanism (via read_with_retry)
            def mock_read_retry_for_sector(usb, args, track_iter):
                # Assume args.tracks provides C/H info correctly
                track_str = str(args.tracks); current_cyl = 0; current_head = 0
                try: # Basic parsing
                    parts = track_str.split(':')
                    for part in parts:
                        if part.startswith('c='): current_cyl = int(part[2:])
                        if part.startswith('h='): current_head = int(part[2:])
                except Exception: pass
                print(f"DEBUG: [read_with_retry mock] Called for read C:{current_cyl} H:{current_head}")
                # Simulate finding the sector data when the track is read
                mock_flux = create_mock_flux()
                # Create track data with the expected sector content
                mock_track = create_mock_track_data(current_cyl, current_head, test_format)
                # Ensure the specific sector has the expected mock data
                found_sector = False
                for s in mock_track.track.sectors:
                    if s.idam.c == cyl and s.idam.h == head and s.idam.r == sect:
                        s.dam.data = expected_data # Set the expected data
                        found_sector = True
                        break
                # If the test asks for a sector not generated by default, this ensures it's present
                # self.assertTrue(found_sector, f"Mock track data generation failed for C:{cyl} H:{head} S:{sect}")
                return (mock_flux, mock_track)

            self.mock_read_with_retry.side_effect = mock_read_retry_for_sector

            # --- Execute Read ---
            print(f"DEBUG: Reading sector C:{cyl} H:{head} S:{sect}...")
            read_data = self.controller.disk.read_sector(cyl, head, sect)
            print(f"DEBUG: Read returned {len(read_data)} bytes.")

            # --- Assertions ---
            self.mock_read_with_retry.assert_called()
            # Verify the arguments passed to read_with_retry (check TrackSet string)
            last_call_args = self.mock_read_with_retry.call_args[0] # Get args tuple from last call
            self.assertIn(f'c={cyl}:h={head}', str(last_call_args[1].tracks)) # Check args.tracks

            self.assertEqual(read_data, expected_data)
            # Verify sector cache was populated
            self.assertIn((cyl, head, sect), self.controller.driver.sector_cache)
            self.assertEqual(self.controller.driver.sector_cache[(cyl, head, sect)], expected_data)

            # --- Test Cache Hit ---
            self.mock_read_with_retry.reset_mock()
            print(f"DEBUG: Reading sector C:{cyl} H:{head} S:{sect} again (cache hit expected)...")
            read_data_cached = self.controller.disk.read_sector(cyl, head, sect)
            self.mock_read_with_retry.assert_not_called() # Should not call read again
            self.assertEqual(read_data_cached, expected_data)


    def test_04_physical_read_sector_not_found(self):
        """Tests reading a sector that isn't found on the track."""
        cyl, head, sect = 11, 0, 15 # Sector within range, but mock won't provide it
        expected_data = b'\x00' * 512 # Expect zeros for not found
        test_format = FMT_144

        if self.using_real_hardware:
             # Assumes Drive A is prepared
            success = self.controller.open_disk(None, "physical", REAL_HW_DRIVE_A, "3.5")
            self.assertTrue(success)
            # Reading a valid but maybe empty sector might return zeros or real data
            # For a more robust test, try reading a sector > sectors_per_track
            invalid_sect = test_format.geometry.sectors_per_track + 1
            with self.assertRaisesRegex(ValueError, "Invalid sector address"):
                 self.controller.disk.read_sector(cyl, head, invalid_sect)
        else:
            # --- Mock Setup ---
            format_info_dict = {**test_format.geometry.__dict__, **test_format.physical_format.__dict__}
            self.controller.open_disk(None, "physical", REAL_HW_DRIVE_A, "3.5", format_info=format_info_dict)

            # Mock _read_track (via read_with_retry) to return *no* data for the specific sector
            def mock_read_retry_missing_sector(usb, args, track_iter):
                track_str = str(args.tracks); current_cyl = 0; current_head = 0
                try: # Basic parsing
                    parts = track_str.split(':')
                    for part in parts:
                        if part.startswith('c='): current_cyl = int(part[2:])
                        if part.startswith('h='): current_head = int(part[2:])
                except Exception: pass
                print(f"DEBUG: [read_with_retry mock] Called for read C:{current_cyl} H:{current_head} (missing sector test)")
                mock_flux = create_mock_flux()
                mock_track = create_mock_track_data(current_cyl, current_head, test_format)
                # Simulate missing sector by removing it or its data
                found_and_removed = False
                new_sectors = []
                for s in mock_track.track.sectors:
                    if s.idam.c == cyl and s.idam.h == head and s.idam.r == sect:
                        print(f"DEBUG: Simulating missing sector C:{cyl} H:{head} S:{sect}")
                        found_and_removed = True
                        # Option 1: Remove the sector entirely
                        # continue
                        # Option 2: Keep sector but remove data (more realistic for some errors)
                        s.dam = None # No data block found
                        new_sectors.append(s)
                    else:
                        new_sectors.append(s)
                mock_track.track.sectors = new_sectors
                # If removed entirely, update nr_missing
                # if found_and_removed: mock_track.nr_missing.return_value = 1
                return (mock_flux, mock_track)

            self.mock_read_with_retry.side_effect = mock_read_retry_missing_sector

            # --- Execute Read ---
            print(f"DEBUG: Reading sector C:{cyl} H:{head} S:{sect} (expected not found)...")
            read_data = self.controller.disk.read_sector(cyl, head, sect)

            # --- Assertions ---
            self.mock_read_with_retry.assert_called()
            self.assertEqual(read_data, expected_data) # Driver returns zeros
            # Sector cache should *not* contain the specific sector key if not found
            self.assertNotIn((cyl, head, sect), self.controller.driver.sector_cache)
            # Track data cache should exist, but the sector shouldn't be a key within it
            self.assertIn((cyl, head), self.controller.driver.track_data)
            self.assertNotIn(sect, self.controller.driver.track_data[(cyl, head)])


    def test_05_physical_read_sector_read_error(self):
        """Tests handling of a hardware read error during track read."""
        cyl, head, sect = 12, 1, 1
        test_format = FMT_144

        if self.using_real_hardware:
            self.skipTest("Simulating hardware read errors requires mocking.")
        else:
            # --- Mock Setup ---
            format_info_dict = {**test_format.geometry.__dict__, **test_format.physical_format.__dict__}
            self.controller.open_disk(None, "physical", REAL_HW_DRIVE_A, "3.5", format_info=format_info_dict)

            # Mock read_with_retry to raise a Greaseweazle error
            error_to_raise = real_gw_usb.CmdError(
                cmd=struct.pack('2B', real_gw_usb.Cmd.ReadFlux, 0), # Dummy command bytes
                code=real_gw_usb.Ack.NoIndex # Simulate NoIndex error
            ) if REAL_GW_AVAILABLE else RuntimeError("Mock Read Error") # Fallback exception
            self.mock_read_with_retry.side_effect = error_to_raise
            print(f"DEBUG: Configured read_with_retry to raise {type(error_to_raise).__name__}")

            # --- Execute and Assert ---
            print(f"DEBUG: Reading sector C:{cyl} H:{head} S:{sect} (expecting read error)...")
            # The driver's read_sector currently catches the exception from _read_track
            # and returns zeros, logging the error.
            read_data = self.controller.disk.read_sector(cyl, head, sect)

            self.mock_read_with_retry.assert_called() # Ensure the failing read was attempted
            self.assertEqual(read_data, b'\x00' * 512) # Driver returns zeros on error
            # Verify track cache was updated with empty dict for this track to prevent retries
            self.assertIn((cyl, head), self.controller.driver.track_data)
            self.assertEqual(self.controller.driver.track_data[(cyl, head)], {})
            # Check logs for error message (requires log capture setup in test runner)


    def test_06_physical_write_sector_and_flush_success(self):
        """Tests writing a sector and flushing successfully."""
        cyl, head, sect = 20, 0, 9
        write_data = b'\xDB' * 512
        test_format = FMT_144

        if self.using_real_hardware:
            # Assumes Drive A prepared
            success = self.controller.open_disk(None, "physical", REAL_HW_DRIVE_A, "3.5")
            self.assertTrue(success)
            # Enable verification for real hardware test
            self.controller.driver.verify_writes = True
            self.controller.disk.write_sector(cyl, head, sect, write_data)
            # Flush triggers the actual write and verification read
            self.controller.driver.flush()
            # Verify by reading back (should hit cache if verification passed)
            read_back_data = self.controller.disk.read_sector(cyl, head, sect)
            self.assertEqual(read_back_data, write_data)
        else:
            # --- Mock Setup ---
            format_info_dict = {**test_format.geometry.__dict__, **test_format.physical_format.__dict__}
            self.controller.open_disk(None, "physical", REAL_HW_DRIVE_A, "3.5", format_info=format_info_dict)
            # Disable verification for mock test unless specifically testing verification logic
            self.controller.driver.verify_writes = False

            # 1. Mock _convert_to_flux (needed by flush)
            mock_flux_list = [100.0, 200.0, 150.0] # Use floats like real flux lists
            # Ensure the mock is attached correctly
            self.controller.driver._convert_to_flux = MagicMock(return_value=mock_flux_list)
            # Patching might be cleaner if issues persist:
            # convert_patcher = patch.object(self.controller.driver, '_convert_to_flux', return_value=mock_flux_list)
            # mock_convert = convert_patcher.start()
            # self.addCleanup(convert_patcher.stop) # Use addCleanup

            # 2. Mock USB behavior needed by flush's internal wrapper
            self.mock_usb.write_track.return_value = None # Simulate success
            self.mock_usb.seek.return_value = None
            self.mock_usb.drive_motor.return_value = None

            # 3. Configure with_drive_selected side_effect for flush
            def write_flush_side_effect(func, usb, drive, motor=True): # Expect motor=True for write
                drive_obj = self.mock_drive_instance # Use configured instance
                print(f"DEBUG: [with_drive_selected flush] Wrapping func={func.__name__}, motor={motor}, drive_unit={drive_obj.unit_id}")
                self.assertTrue(motor, "Flush should call with_drive_selected with motor=True")
                try:
                    usb.drive_select(drive_obj.unit_id)
                    usb.drive_motor(drive_obj.unit_id, True) # Motor ON
                    result = func() # Execute the wrapped function (e.g., write_track_wrapper)
                    print(f"DEBUG: [with_drive_selected flush] Func {func.__name__} executed.")
                    return result
                finally:
                     print(f"DEBUG: [with_drive_selected flush] Cleaning up motor/select for {func.__name__}")
                     usb.drive_motor(drive_obj.unit_id, False) # Motor OFF
                     usb.drive_deselect()
            self.mock_with_drive_selected.side_effect = write_flush_side_effect

            # --- Execute Write & Flush ---
            print(f"DEBUG: Writing sector C:{cyl} H:{head} S:{sect}...")
            self.controller.disk.write_sector(cyl, head, sect, write_data)
            self.assertIn((cyl, head), self.controller.driver.dirty_sectors)
            self.assertEqual(self.controller.driver.dirty_sectors[(cyl, head)][sect], write_data)
            self.assertIn((cyl, head), self.controller.driver.dirty_tracks)

            print(f"DEBUG: Flushing...")
            self.controller.driver.flush()
            print(f"DEBUG: Flush completed.")

            # --- Assertions ---
            # Check mocks related to flush execution
            self.controller.driver._convert_to_flux.assert_called_once_with(cyl, head)

            # Check calls made *by the side_effect* wrapper
            self.mock_usb.drive_select.assert_called_with(self.mock_drive_instance.unit_id)
            # Check motor was turned on (True) and then off (False)
            self.mock_usb.drive_motor.assert_any_call(self.mock_drive_instance.unit_id, True) # Check ON
            self.mock_usb.drive_motor.assert_called_with(self.mock_drive_instance.unit_id, False) # Check OFF (last call)
            self.mock_usb.drive_deselect.assert_called()

            # Check mocks related to write_track_wrapper called *inside* the flush
            self.mock_usb.seek.assert_called_with(cyl, head)
            self.mock_usb.write_track.assert_called_once_with(
                flux_list=mock_flux_list,
                cue_at_index=True,
                terminate_at_index=True
            )
            # Verify dirty cache is cleared
            self.assertNotIn((cyl, head), self.controller.driver.dirty_sectors)
            self.assertEqual(len(self.controller.driver.dirty_tracks), 0)
            # Verify track_data cache for this track is invalidated
            self.assertNotIn((cyl, head), self.controller.driver.track_data)


    def test_07_physical_flush_write_error(self):
        """Tests handling a write error during flush."""
        cyl, head, sect = 21, 1, 2
        write_data = b'\xEE' * 512
        test_format = FMT_144

        if self.using_real_hardware:
            self.skipTest("Simulating hardware write errors requires mocking.")
        else:
             # --- Mock Setup ---
            format_info_dict = {**test_format.geometry.__dict__, **test_format.physical_format.__dict__}
            self.controller.open_disk(None, "physical", REAL_HW_DRIVE_A, "3.5", format_info=format_info_dict)
            self.controller.driver.verify_writes = False

            # 1. Mock _convert_to_flux
            mock_flux_list = [111.0, 222.0, 111.0]
            self.controller.driver._convert_to_flux = MagicMock(return_value=mock_flux_list)

            # 2. Mock USB write_track to RAISE an error (e.g., Write Protected)
            error_to_raise = real_gw_usb.CmdError(
                cmd=struct.pack('2B', real_gw_usb.Cmd.WriteFlux, 0), # Dummy command
                code=real_gw_usb.Ack.Wrprot # Simulate Write Protect
            ) if REAL_GW_AVAILABLE else RuntimeError("Mock Write Error")
            self.mock_usb.write_track.side_effect = error_to_raise
            self.mock_usb.seek.return_value = None

            # 3. Configure with_drive_selected for flush (similar to test_06)
            def write_flush_error_side_effect(func, usb, drive, motor=True):
                drive_obj = self.mock_drive_instance
                print(f"DEBUG: [with_drive_selected write error] Wrapping func={func.__name__}, motor={motor}")
                try:
                    usb.drive_select(drive_obj.unit_id)
                    usb.drive_motor(drive_obj.unit_id, True)
                    # Execute the wrapped function (write_track_wrapper)
                    # The error will be raised inside func()
                    func()
                # The driver's flush catches the exception, so we don't expect it here
                finally:
                     print(f"DEBUG: [with_drive_selected write error] Cleaning up motor/select for {func.__name__}")
                     usb.drive_motor(drive_obj.unit_id, False)
                     usb.drive_deselect()
            self.mock_with_drive_selected.side_effect = write_flush_error_side_effect

            # --- Execute Write & Flush ---
            self.controller.disk.write_sector(cyl, head, sect, write_data)
            self.assertIn((cyl, head), self.controller.driver.dirty_sectors)

            print(f"DEBUG: Flushing (expecting write error inside)...")
            # Execute flush. The driver should catch the CmdError, log it,
            # and *not* clear the successfully_written list for this track.
            self.controller.driver.flush()
            print(f"DEBUG: Flush completed.")

            # --- Assertions ---
            self.controller.driver._convert_to_flux.assert_called_once_with(cyl, head)
            self.mock_usb.seek.assert_called_once_with(cyl, head)
            self.mock_usb.write_track.assert_called_once() # Check write was attempted

            # **** FIX: Assert that dirty flags REMAIN after write error ****
            self.assertIn((cyl, head), self.controller.driver.dirty_sectors, "Dirty sectors should remain after write error")
            self.assertEqual(self.controller.driver.dirty_sectors[(cyl, head)][sect], write_data, "Dirty sector data should persist after write error")
            self.assertIn((cyl, head), self.controller.driver.dirty_tracks, "Dirty track flag should remain after write error")

            print("DEBUG: Manually clearing dirty flags after testing the failed flush.")
            self.controller.driver.dirty_sectors.clear()
            self.controller.driver.dirty_tracks.clear()


    @unittest.skipIf(not USE_REAL_HARDWARE, "End-to-end test requires real hardware (TEST_HW=true)")
    def test_08_physical_e2e_modify_and_verify(self):
        """
        End-to-end test: Modify disk via fatfloppy, read via 'gw', verify byte-for-byte.
        Requires disks prepared by prepare_test_disks.py.
        """
        GW_EXECUTABLE = "gw" # Ensure 'gw' is in PATH
        # --- Test Parameters ---
        drive_configs = [
            {
                "drive": REAL_HW_DRIVE_A, "size": "3.5", "format_obj": FMT_144,
                "gw_format_name": "ibm.mfm", # Adjust if your 1.44MB is called something else in gw diskdefs
                "ops": [
                    ("create_dir", "/E2EDIR"),
                    ("write_file", "/E2EDIR/E2E_A.TXT", b"Drive A E2E test " + b"A"*20),
                    ("delete_item", "/FILE_B.BIN"), # Assumes prepare script creates this
                    ("delete_item", "/DIR1/SUBDIR"), # Assumes prepare creates this (must be empty)
                    ("delete_item", "/DIR1"),        # Assumes DIR1 is now empty
                ],
                "expected_files_after": ["/E2EDIR/", "/E2EDIR/E2E_A.TXT"] # Check subset
            },
            {
                "drive": REAL_HW_DRIVE_B, "size": "5.25", "format_obj": FMT_360,
                "gw_format_name": "ibm.mfm", # Adjust if your 360KB is called something else
                "ops": [
                    ("create_dir", "/TESTB"),
                    ("write_file", "/TESTB/FILE_B.DAT", b"Drive B test " + b"B"*550), # Test multi-sector
                    ("delete_item", "/TEXT_B.TXT"), # Assumes prepare script creates this
                ],
                "expected_files_after": ["/TESTB/", "/TESTB/FILE_B.DAT"] # Check subset
            },
        ]

        temp_dir = tempfile.mkdtemp(prefix="fatfloppy_e2e_")
        print(f"Using temporary directory: {temp_dir}")

        try:
            for config in drive_configs:
                drive = config["drive"]
                size = config["size"]
                format_obj = config["format_obj"]
                gw_format_name = config["gw_format_name"] # Format name for gw tool
                ops = config["ops"]
                expected_files = config["expected_files_after"]
                gw_output_path = os.path.join(temp_dir, f"gw_read_drive_{drive}.img")

                print(f"\n----- Testing Drive {drive} ({format_obj.description}) -----")

                # 1. Open disk via fatfloppy
                print(f"Opening Drive {drive} via fatfloppy...")
                # Use explicit format matching the prepared disk
                format_info_dict = {
                    **{k: v for k, v in format_obj.geometry.__dict__.items() if not k.startswith('_')},
                    **{k: v for k, v in format_obj.physical_format.__dict__.items() if not k.startswith('_')}
                }
                success = self.controller.open_disk(None, "physical", drive, size, format_info=format_info_dict)
                self.assertTrue(success, f"Failed to open Drive {drive} for modification")
                self.assertIsNotNone(self.controller.disk, f"Controller disk object is None after opening Drive {drive}")
                self.assertIsNotNone(self.controller.filesystem, f"Filesystem not detected on Drive {drive}")

                # 2. Perform filesystem modifications
                print("Performing filesystem modifications...")
                for op_data in ops:
                    op_type = op_data[0]
                    op_args = op_data[1:]
                    print(f"  Op: {op_type} {op_args[0] if op_args else ''}") # Avoid printing large data
                    op_success = False
                    try:
                        if op_type == "create_dir":
                            op_success = self.controller.create_directory(*op_args)
                        elif op_type == "write_file":
                            op_success = self.controller.write_file(*op_args)
                        elif op_type == "delete_item":
                            op_success = self.controller.delete_item(*op_args)
                        else:
                            self.fail(f"Unknown operation type: {op_type}")
                        self.assertTrue(op_success, f"Operation failed: {op_type} {op_args[0] if op_args else ''}")
                    except Exception as e:
                         self.fail(f"Exception during operation {op_type} {op_args[0] if op_args else ''}: {e}")

                # 3. Close disk (triggers flush)
                print("Closing disk (flushing writes)...")
                self.controller.close_disk()
                print("Disk closed.")
                time.sleep(1) # Give drive motor time to spin down if necessary

                # 4. Read disk using external 'gw' tool
                print(f"Reading Drive {drive} using '{GW_EXECUTABLE} read' to {gw_output_path}...")
                # Using the explicit format ensures we get the exact structure we expect.
                gw_cmd = [
                    GW_EXECUTABLE, "read", f"--drive={drive}",
                    f"--format={gw_format_name}",
                    # Pass specific geometry/physical parameters if gw_format_name isn't enough
                    # e.g., f"--rate={format_obj.physical_format.rate}"
                    # Check if the format needs specific overrides
                    # For standard IBM formats, the name should suffice if gw knows it.
                    "--revs=2", # Read a couple of revs for reliability
                    gw_output_path
                ]
                try:
                    result = subprocess.run(gw_cmd, check=True, capture_output=True, text=True, timeout=120) # 2 min timeout
                    print(f"'{GW_EXECUTABLE} read' completed successfully.")
                    # print(result.stdout) # Optional: show output
                    if result.stderr:
                         print(f"'{GW_EXECUTABLE} read' stderr:\n{result.stderr}")
                except FileNotFoundError:
                    self.fail(f"'{GW_EXECUTABLE}' command not found. Is it in PATH?")
                except subprocess.TimeoutExpired:
                     self.fail(f"'{GW_EXECUTABLE} read' timed out for Drive {drive}.")
                except subprocess.CalledProcessError as e:
                    # Provide more context on failure
                    error_msg = f"'{GW_EXECUTABLE} read' failed for Drive {drive} (Return Code: {e.returncode})\nCommand: {' '.join(gw_cmd)}\nStdout:\n{e.stdout}\nStderr:\n{e.stderr}"
                    # Check for common gw errors
                    if "unformatted track" in e.stderr.lower() or "no sectors found" in e.stderr.lower():
                         error_msg += "\nPossible cause: Disk format mismatch or unformatted disk."
                    elif "No USB devices found" in e.stderr:
                         error_msg += "\nPossible cause: Greaseweazle device not connected or permissions issue."
                    self.fail(error_msg)

                self.assertTrue(os.path.exists(gw_output_path), f"Output file from 'gw read' not found: {gw_output_path}")
                with open(gw_output_path, "rb") as f:
                    gw_read_data = f.read()
                self.assertGreater(len(gw_read_data), 0, f"'{GW_EXECUTABLE} read' produced an empty file for Drive {drive}")
                expected_size = format_obj.geometry.total_bytes
                # Allow for slight size difference if gw read pads differently, but should be close
                self.assertAlmostEqual(len(gw_read_data), expected_size, delta=expected_size*0.02,
                                       msg=f"gw read size ({len(gw_read_data)}) differs significantly from expected ({expected_size}) for Drive {drive}")


                # 5. Re-open disk via fatfloppy and read all bytes
                print(f"Re-opening Drive {drive} via fatfloppy for verification reading...")
                success = self.controller.open_disk(None, "physical", drive, size, format_info=format_info_dict)
                self.assertTrue(success, f"Failed to re-open Drive {drive} for verification")

                # Verify filesystem structure looks okay after reopen (optional but good)
                print("Verifying some expected files exist after reopen...")
                fs_list = self.controller.list_directory("/")
                fs_contents = {item['name'].upper(): item for item in fs_list} # Use dict for easier lookup
                # Add recursive listing if needed for deeper checks
                if "/E2EDIR/" in expected_files:
                     self.assertIn("E2EDIR", fs_contents, "E2EDIR not found at root after reopen")
                     self.assertTrue(fs_contents["E2EDIR"]["is_dir"], "E2EDIR is not a directory")
                     subdir_list = self.controller.list_directory("/E2EDIR")
                     subdir_contents = {item['name'].upper(): item for item in subdir_list}
                     if "/E2EDIR/E2E_A.TXT" in expected_files:
                          self.assertIn("E2E_A.TXT", subdir_contents, "E2E_A.TXT not found in E2EDIR")
                          self.assertFalse(subdir_contents["E2E_A.TXT"]["is_dir"], "E2E_A.TXT should be a file")
                if "/TESTB/" in expected_files:
                     self.assertIn("TESTB", fs_contents, "TESTB not found at root after reopen")
                     self.assertTrue(fs_contents["TESTB"]["is_dir"], "TESTB is not a directory")
                     subdir_list = self.controller.list_directory("/TESTB")
                     subdir_contents = {item['name'].upper(): item for item in subdir_list}
                     if "/TESTB/FILE_B.DAT" in expected_files:
                          self.assertIn("FILE_B.DAT", subdir_contents, "FILE_B.DAT not found in TESTB")
                          self.assertFalse(subdir_contents["FILE_B.DAT"]["is_dir"], "FILE_B.DAT should be a file")


                print("Reading entire disk via fatfloppy driver...")
                app_read_data = self._read_entire_disk_bytes(self.controller)
                self.assertEqual(len(app_read_data), expected_size,
                                 f"fatfloppy read size ({len(app_read_data)}) doesn't match geometry ({expected_size}) for Drive {drive}")

                # 6. Compare byte-for-byte
                print(f"Comparing fatfloppy data ({len(app_read_data)} bytes) with 'gw read' data ({len(gw_read_data)} bytes)...")
                # If sizes differed significantly, this will fail. If they are almost equal:
                min_len = min(len(app_read_data), len(gw_read_data))
                if app_read_data[:min_len] != gw_read_data[:min_len]:
                    # Find first difference
                    diff_index = -1
                    for i in range(min_len):
                        if app_read_data[i] != gw_read_data[i]:
                            diff_index = i
                            break
                    # Save files for analysis
                    app_file = os.path.join(temp_dir, f"fatfloppy_read_drive_{drive}.img")
                    with open(app_file, "wb") as f: f.write(app_read_data)
                    self.fail(f"Data mismatch for Drive {drive} starting at offset {diff_index} (0x{diff_index:X}). "
                              f"Expected byte {gw_read_data[diff_index]:02X}, Got byte {app_read_data[diff_index]:02X}. "
                              f"Check files in {temp_dir}: {os.path.basename(gw_output_path)} vs {os.path.basename(app_file)}")

                print(f"Byte-for-byte comparison successful for Drive {drive}.")

                # 7. Close controller for the next iteration
                self.controller.close_disk()

        finally:
            # Clean up temporary directory unless KEEP_E2E_FILES is set
            if os.getenv('KEEP_E2E_FILES', 'false').lower() != 'true':
                 import shutil
                 try:
                     shutil.rmtree(temp_dir)
                     print(f"Cleaned up temporary directory: {temp_dir}")
                 except OSError as e:
                     print(f"Warning: Could not remove temporary directory {temp_dir}: {e}")
            else:
                 print(f"Test finished. Temporary files kept in: {temp_dir}")

    def test_09_physical_flush_partial_track_reads_first(self):
        """Tests that flush reads track data first if track is partially dirty."""
        cyl, head, sect = 25, 1, 4
        write_data = b'\xCC' * 512
        test_format = FMT_144 # 18 sectors per track

        if self.using_real_hardware:
            self.skipTest("Simulating partial track flush requires mocking.")
        else:
            # --- Mock Setup ---
            format_info_dict = {**test_format.geometry.__dict__, **test_format.physical_format.__dict__}
            self.controller.open_disk(None, "physical", REAL_HW_DRIVE_A, "3.5", format_info=format_info_dict)
            self.controller.driver.verify_writes = False

            # Mock convert and usb writes (similar to test_06)
            mock_flux_list = [300.0, 100.0, 250.0]
            self.controller.driver._convert_to_flux = MagicMock(return_value=mock_flux_list)
            self.mock_usb.write_track.return_value = None
            self.mock_usb.seek.return_value = None
            self.mock_usb.drive_motor.return_value = None

            # Configure with_drive_selected for flush (similar to test_06)
            write_flush_side_effect = MagicMock(side_effect=lambda func, usb, drive, motor=True: func())
            self.mock_with_drive_selected.side_effect = write_flush_side_effect

            # Mock read_with_retry to verify it gets called during flush
            # It should return some valid data for the track being read
            read_retry_side_effect = MagicMock(return_value=(create_mock_flux(), create_mock_track_data(cyl, head, test_format)))
            self.mock_read_with_retry.side_effect = read_retry_side_effect


            # --- Execute Write (only one sector) & Flush ---
            print(f"DEBUG: Writing sector C:{cyl} H:{head} S:{sect} (partial track)...")
            self.controller.disk.write_sector(cyl, head, sect, write_data)
            # Verify only one sector is marked dirty
            self.assertEqual(len(self.controller.driver.dirty_sectors.get((cyl, head), {})), 1)

            print(f"DEBUG: Flushing (expecting track read before write)...")
            self.controller.driver.flush()
            print(f"DEBUG: Flush completed.")

            # --- Assertions ---
            # Verify _read_track (via read_with_retry) was called *before* _convert_to_flux/write
            read_retry_side_effect.assert_called_once()
            # Check the track being read was correct
            call_args = read_retry_side_effect.call_args[0]
            self.assertIn(f'c={cyl}:h={head}', str(call_args[1].tracks))

            # Verify the rest of the flush process happened
            self.controller.driver._convert_to_flux.assert_called_once_with(cyl, head)
            self.mock_usb.seek.assert_called_once_with(cyl, head)
            self.mock_usb.write_track.assert_called_once_with(flux_list=mock_flux_list, cue_at_index=True, terminate_at_index=True)
            # Verify dirty cache is cleared
            self.assertNotIn((cyl, head), self.controller.driver.dirty_sectors)
            self.assertEqual(len(self.controller.driver.dirty_tracks), 0)

    def test_10_gw_cache_invalidation(self):
        """Tests that track_data cache is invalidated after a successful flush."""
        if self.using_real_hardware: self.skipTest("Mocking required")
        cyl, head, sect = 22, 0, 1
        write_data = b'\xAA' * 512
        test_format = FMT_144
        format_info_dict = {**test_format.geometry.__dict__, **test_format.physical_format.__dict__}
        self.controller.open_disk(None, "physical", REAL_HW_DRIVE_A, "3.5", format_info=format_info_dict)

        # 1. Read the sector to populate cache
        mock_read_retry_for_read1 = MagicMock(return_value=(create_mock_flux(), create_mock_track_data(cyl, head, test_format)))
        self.mock_read_with_retry.side_effect = mock_read_retry_for_read1
        print("DEBUG: Performing initial read to populate cache...")
        self.controller.disk.read_sector(cyl, head, sect)
        self.assertIn((cyl, head), self.controller.driver.track_data) # Cache populated
        mock_read_retry_for_read1.assert_called_once()
        print("DEBUG: Initial read complete, cache populated.")

        # 2. Write ONE sector (making the track partially dirty)
        print("DEBUG: Writing one sector...")
        self.controller.disk.write_sector(cyl, head, sect, write_data)

        # 3. Flush - Expecting a read call during flush due to partial write
        self.mock_read_with_retry.reset_mock() # Reset before flush
        # Create mock for the read that happens *during* flush
        mock_read_retry_during_flush = MagicMock(return_value=(create_mock_flux(), create_mock_track_data(cyl, head, test_format)))
        self.mock_read_with_retry.side_effect = mock_read_retry_during_flush

        print("DEBUG: Setting up mocks for flush...")
        # --- Setup flush mocks ---
        self.controller.driver._convert_to_flux = MagicMock(return_value=[1.0])
        self.mock_usb.write_track.return_value = None
        self.mock_usb.seek.return_value = None
        # Use a simple side effect for with_drive_selected during flush
        # This needs to correctly wrap the function call
        def flush_wrapper_side_effect(func, usb, drive, motor=True):
             # Simulate the drive select/motor logic if needed by func
             print(f"DEBUG: [with_drive_selected flush mock] Wrapping {func.__name__}")
             # usb.drive_select(drive.unit_id) # Mocked usb doesn't need this usually
             # usb.drive_motor(drive.unit_id, motor)
             result = func() # Execute the actual wrapped function
             # usb.drive_motor(drive.unit_id, False)
             # usb.drive_deselect()
             return result
        self.mock_with_drive_selected.side_effect = flush_wrapper_side_effect
        # -------------------------
        print("DEBUG: Flushing...")
        self.controller.driver.flush()
        print("DEBUG: Flush complete.")

        # *** FIX: Assert that read_with_retry WAS called ONCE during flush ***
        # This verifies the pre-read logic for partial tracks executed.
        mock_read_retry_during_flush.assert_called_once()
        self.mock_read_with_retry.assert_called_once() # Verify main mock call count since reset

        # 4. Assert track_data cache is now empty for this track AFTER flush
        #    (Flush invalidates the cache *after* writing successfully)
        self.assertNotIn((cyl, head), self.controller.driver.track_data)
        print("DEBUG: Track cache confirmed empty after flush.")

        # 5. Verify reading again triggers a new physical read
        self.mock_read_with_retry.reset_mock() # Reset call count again before the final check
        # Create a specific mock instance for the final read's side effect
        mock_read_retry_for_read2 = MagicMock(return_value=(create_mock_flux(), create_mock_track_data(cyl, head, test_format)))
        self.mock_read_with_retry.side_effect = mock_read_retry_for_read2
        # Re-apply the side effect for with_drive_selected needed by _read_track
        # For read, motor is usually False
        def read_wrapper_side_effect(func, usb, drive, motor=False):
             print(f"DEBUG: [with_drive_selected read mock] Wrapping {func.__name__}")
             return func()
        self.mock_with_drive_selected.side_effect = read_wrapper_side_effect
        print("DEBUG: Performing final read to check cache miss...")
        self.controller.disk.read_sector(cyl, head, sect)
        mock_read_retry_for_read2.assert_called_once()
        self.mock_read_with_retry.assert_called_once() # Check main mock since last reset
        print("DEBUG: Final read complete, physical read triggered as expected.")

    def test_11_gw_write_verify_success(self):
        """Tests successful write verification path."""
        if self.using_real_hardware: self.skipTest("Mocking required")
        cyl, head, sect = 23, 1, 7
        write_data = b'\xBB' * 512
        test_format = FMT_144
        format_info_dict = {**test_format.geometry.__dict__, **test_format.physical_format.__dict__}
        self.controller.open_disk(None, "physical", REAL_HW_DRIVE_A, "3.5", format_info=format_info_dict)
        self.controller.driver.verify_writes = True # Enable verification

        # Mock dependencies for write and verify read
        self.controller.driver._convert_to_flux = MagicMock(return_value=[1.0])
        self.mock_usb.seek = MagicMock()
        self.mock_usb.write_track = MagicMock(return_value=None)
        # Mock read_with_retry for the verification step
        read_retry_side_effect = MagicMock(return_value=(create_mock_flux(), create_mock_track_data(cyl, head, test_format)))
        self.mock_read_with_retry.side_effect = read_retry_side_effect
        # Mock with_drive_selected
        self.mock_with_drive_selected.side_effect = lambda func, usb, drive, motor=True: func()

        # Execute write and flush
        self.controller.disk.write_sector(cyl, head, sect, write_data)
        self.controller.driver.flush()

        # Assertions
        self.mock_usb.seek.assert_called_with(cyl, head) # Called for write
        self.mock_usb.write_track.assert_called_once()
        # Verify _read_track (via read_with_retry) was called for verification
        read_retry_side_effect.assert_called_once()
        # Verify dirty flags cleared etc.
        self.assertNotIn((cyl, head), self.controller.driver.dirty_sectors)

    def _read_entire_disk_bytes(self, controller: DiskController) -> bytes:
        """Reads all sectors sequentially using the controller and returns raw bytes."""
        if not controller.disk or not controller.disk.geometry:
            self.fail("Disk or geometry not available in controller to read all bytes.")

        geom = controller.disk.geometry
        all_data = bytearray()
        print(f"Reading entire disk via fatfloppy: {geom.cylinders}C x {geom.heads}H x {geom.sectors_per_track}S ({geom.sector_size} bytes/sec)")
        total_sectors = geom.total_sectors
        read_count = 0
        last_c, last_h = -1, -1
        try:
            for c in range(geom.cylinders):
                for h in range(geom.heads):
                    # Print progress per track
                    if c != last_c or h != last_h:
                         print(f"  Reading Track C:{c} H:{h}...", end='\r')
                         last_c, last_h = c, h
                    for s in range(1, geom.sectors_per_track + 1):
                        try:
                            sector_data = controller.disk.read_sector(c, h, s)
                        except ValueError as e:
                             # Handle potential errors during read (e.g., if mocking sector read failure)
                             print(f"\nWarning: Error reading sector C:{c} H:{h} S:{s}: {e}. Using zeros.")
                             sector_data = b'\x00' * geom.sector_size

                        if len(sector_data) != geom.sector_size:
                             print(f"\nWarning: Short read C:{c} H:{h} S:{s}, got {len(sector_data)} bytes, padding to {geom.sector_size}")
                             sector_data += b'\x00' * (geom.sector_size - len(sector_data))
                        all_data.extend(sector_data)
                        read_count += 1

            print(f"  Read {read_count}/{total_sectors} sectors... Done.                     ") # Spaces clear line
            expected_size = geom.total_bytes
            if len(all_data) != expected_size:
                 print(f"Warning: Read data size ({len(all_data)}) differs from expected geometry size ({expected_size}).")
            return bytes(all_data)
        except Exception as e:
            # Catch any other unexpected error during the read loop
            self.fail(f"Unexpected error reading sector near C:{c} H:{h} S:{s} via fatfloppy: {e}")


if __name__ == '__main__':
    # Important: Set TEST_HW environment variable to 'true' to run against real hardware
    # Example: TEST_HW=true python -m unittest tests/test_05_controller_physical.py
    # Remember to run prepare_test_disks.py first if using real hardware!
    print(f"** Hardware Test Mode: {'ENABLED' if USE_REAL_HARDWARE else 'DISABLED (Mocked)'} **")
    if USE_REAL_HARDWARE:
        print("** Ensure 'prepare_test_disks.py' has been run recently! **")
    unittest.main()
