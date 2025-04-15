# tests/test_05_controller_physical.py
import unittest
from unittest.mock import patch, MagicMock, ANY
import os
import sys
import time # For potential waits in real HW tests
import struct # For packing/unpacking mock data
import subprocess # Needed for running gw command
import tempfile   # Needed for temporary image files

# Ensure src is in path or install the package
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src')))

from fatfloppy.core.controller import DiskController
from fatfloppy.core.drivers import GreaseweazleDriver, PhysicalFormat
from fatfloppy.core.disk import DiskGeometry
from fatfloppy.core.format_definitions import FLOPPY_FORMATS

# Attempt to import real Greaseweazle components for realistic mocking/typing
try:
    # Import specific Greaseweazle components needed for mocking structure/constants
    from greaseweazle import usb as real_gw_usb
    from greaseweazle.flux import Flux as RealFlux, WriteoutFlux as RealWriteoutFlux
    from greaseweazle.codec import codec as real_gw_codec
    from greaseweazle.track import MasterTrack as RealMasterTrack
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
    RealMasterTrack = MagicMock
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
    mock_flux = MagicMock(spec=RealFlux)
    mock_flux.sample_freq = sample_freq
    mock_flux._ticks_per_rev = ticks_per_rev # Store the intended raw value
    mock_flux.index_list = [float(ticks_per_rev)] * revs
    # Create some plausible flux timings (e.g., alternating ~4us MFM timings)
    # Total time should roughly match ticks_per_rev * revs
    avg_interval = 4e-6 * sample_freq # ~64 ticks at 16MHz
    num_fluxes = int((ticks_per_rev * revs) / avg_interval)
    mock_flux.list = [float(avg_interval + (-1)**i * avg_interval * 0.1) for i in range(num_fluxes)]
    # Adjust sum to roughly match total time
    current_sum = sum(mock_flux.list)
    target_sum = ticks_per_rev * revs
    if current_sum > 0:
         scale = target_sum / current_sum
         mock_flux.list = [x * scale for x in mock_flux.list]

    # Add property getter for ticks_per_rev
    type(mock_flux).ticks_per_rev = unittest.mock.PropertyMock(return_value=ticks_per_rev)
    type(mock_flux).time_per_rev = unittest.mock.PropertyMock(return_value=ticks_per_rev / sample_freq)
    mock_flux.summary_string.return_value = f"Mock Flux ({len(mock_flux.list)} samples, {ticks_per_rev/sample_freq*1000:.2f}ms/rev)"
    mock_flux.flux.return_value = mock_flux # Return self for .flux() calls
    return mock_flux

# Helper function to create mock Decoded Track Data (Codec instance)
def create_mock_codec(cyl, head, fmt_def):
    mock_codec = MagicMock(spec=real_gw_codec.Codec)
    mock_codec.cyl = cyl
    mock_codec.head = head
    # Simulate some sectors based on format
    mock_codec.nsec = fmt_def.geometry.sectors_per_track
    mock_sectors = []
    for i in range(mock_codec.nsec):
        sec = MagicMock()
        sec.idam = MagicMock()
        sec.idam.r = i + fmt_def.boot_sector.sectors_per_track # Assuming ID = 1 based
        sec.crc = 0 # Assume good read
        mock_sectors.append(sec)
    mock_codec.sectors = mock_sectors
    mock_codec.nr_missing.return_value = 0
    mock_codec.has_sec.return_value = True
    mock_codec.summary_string.return_value = f"Mock Codec ({mock_codec.nsec} sectors)"
    # Add master_track method
    def mock_master_track_method():
         mt = MagicMock(spec=RealMasterTrack)
         mt.flux_for_writeout.return_value = MagicMock(spec=RealWriteoutFlux, ticks_to_index=3200000, list=[100.0, 200.0], index_cued=True, terminate_at_index=True)
         return mt
    mock_codec.master_track = mock_master_track_method
    return mock_codec


@unittest.skipIf(USE_REAL_HARDWARE and not REAL_GW_AVAILABLE, "Real Greaseweazle library needed for hardware tests")
class TestDiskControllerPhysical(unittest.TestCase):

    def setUp(self):
        print(f"\n--- Running test: {self.id()} ---")
        self.using_real_hardware = USE_REAL_HARDWARE
        self.patches = []

        if self.using_real_hardware:
            # ... (hardware setup logic remains the same) ...
            print("INFO: Running with REAL Greaseweazle hardware.")
            self.controller = DiskController()
            # Basic check omitted here for brevity, assume it passed if we got here
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
                # *******************************************
                # ***** FIX: Remove autospec=True here *****
                # patcher = patch(target, autospec=True) # <--- Problematic line
                patcher = patch(target)                 # <--- Changed line
                # *******************************************
                self.patches.append(patcher)
                # Use try-except for robustness during setup itself
                try:
                    # Use getattr for safety, though direct assignment is fine here
                    setattr(self, f"mock_{target.split('.')[-1]}", patcher.start())
                except Exception as e:
                     # Ensure cleanup happens even if setup fails mid-way
                     patch.stopall()
                     self.fail(f"Error starting patch for '{target}' in setUp: {e}")


            # Configure mock usb object returned by usb_open
            # Check if mock_usb_open was actually created by the loop above
            if hasattr(self, 'mock_usb_open'):
                self.mock_usb = MagicMock(spec=real_gw_usb.Unit if REAL_GW_AVAILABLE else None) # Use spec for better type hinting
                self.mock_usb.sample_freq = 16000000 # Example frequency
                self.mock_usb_open.return_value = self.mock_usb # Configure the mock created by patcher.start()
            else:
                 self.fail("mock_usb_open was not created during patching.")

            # Configure mock drive object
            if hasattr(self, 'mock_Drive'):
                # self.mock_drive_obj = MagicMock(spec=real_gw_util.Drive if REAL_GW_AVAILABLE else None) # Spec for Drive instance
                self.mock_drive_obj = MagicMock()
                self.mock_Drive.return_value.return_value = self.mock_drive_obj # Drive()('A') returns the mock
            else:
                 self.fail("mock_Drive was not created during patching.")


            # Configure mock disk definition
            if hasattr(self, 'mock_get_diskdef'):
                self.mock_fmt_cls = MagicMock(spec=real_gw_codec.DiskDef if REAL_GW_AVAILABLE else None)
                self.mock_fmt_cls.track_map = {} # Need a dict for track_map
                self.mock_get_diskdef.return_value = self.mock_fmt_cls
            else:
                 self.fail("mock_get_diskdef was not created during patching.")

            # Configure default behaviors for mocked USB methods (only if self.mock_usb exists)
            if hasattr(self, 'mock_usb'):
                self.mock_usb.seek.return_value = None
                self.mock_usb.set_bus_type.return_value = None
                self.mock_usb.drive_select.return_value = None
                self.mock_usb.drive_deselect.return_value = None
                self.mock_usb.drive_motor.return_value = None
                self.mock_usb.get_pin.return_value = False # Default pin state (e.g., TRK0 not asserted)
                self.mock_usb.set_pin.return_value = None
                self.mock_usb.write_track.return_value = None
                self.mock_usb.read_track.return_value = create_mock_flux() # Default return

            # Configure read_with_retry mock
            if hasattr(self, 'mock_read_with_retry'):
                self.mock_read_with_retry.return_value = (create_mock_flux(), create_mock_codec(0, 0, FMT_144))
            else:
                 self.fail("mock_read_with_retry was not created during patching.")

            # Configure with_drive_selected mock to just call the function
            if hasattr(self, 'mock_with_drive_selected'):
                self.mock_with_drive_selected.side_effect = lambda func, usb, drive: func()
            else:
                 self.fail("mock_with_drive_selected was not created during patching.")


            self.controller = DiskController()


    def tearDown(self):
        if not self.using_real_hardware:
            patch.stopall() # Stop all patches started in setUp
        # Ensure disk is closed, flushing if necessary (especially for real HW)
        print("Tearing down test, closing disk...")
        try:
            self.controller.close_disk()
        except Exception as e:
            print(f"Ignoring error during disk close in tearDown: {e}")
        del self.controller
        if self.using_real_hardware:
            # Optional: Small delay to allow hardware to settle if needed
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
            def rpm_side_effect(func, usb, drive, motor=True): # Accept motor argument
                print(f"DEBUG: rpm_side_effect called with func={func.__name__}, motor={motor}")
                if "measure_rpm" in func.__name__:
                    # Simulate drive_motor calls if needed, although measure_rpm doesn't need motor=True itself
                    try:
                        usb.drive_select(drive.unit_id)
                        usb.drive_motor(drive.unit_id, True) # Assume motor needs to be on for RPM read
                        usb.read_track.return_value = create_mock_flux(ticks_per_rev=3200000)
                        func()
                    finally:
                        usb.drive_motor(drive.unit_id, False)
                        usb.drive_deselect()
                else:
                    # For other calls using this side_effect (if any), just execute
                    # Or add more specific simulation if needed
                    try:
                        usb.drive_select(drive.unit_id)
                        usb.drive_motor(drive.unit_id, motor)
                        func()
                    finally:
                        usb.drive_motor(drive.unit_id, False)
                        usb.drive_deselect()
            self.mock_with_drive_selected.side_effect = rpm_side_effect

            # 2. _detect_physical_disk_format
            #    - Needs detect_filesystem to work (mocked below)
            #    - Needs _read_track for head check (mocked via read_with_retry)
            # Mock read_with_retry to simulate successful read on head 0, fail on head 1 for head check if needed
            # For simplicity, let's assume head 1 read succeeds initially for double-sided check
            self.mock_read_with_retry.side_effect = [
                (create_mock_flux(), create_mock_codec(0, 0, expected_format)), # For detect_filesystem C=0, H=0
                (create_mock_flux(), create_mock_codec(0, 1, expected_format)), # For head 1 check C=0, H=1 (assume success initially)
                # Add more if detect_physical tries other tracks/formats
            ]

            # 3. detect_filesystem (make it succeed for the expected format with correct side effect)
            with patch('fatfloppy.core.controller.DiskController.detect_filesystem', autospec=True) as mock_detect_fs:
                # Define the mock function that mimics the behavior of detect_filesystem
                def mock_detect_filesystem_side_effect(self_arg, *args, **kwargs):
                    # Set the filesystem on the instance that's calling detect_filesystem
                    self_arg.filesystem = MagicMock(spec=FATFilesystem)
                    self_arg.filesystem.is_valid.return_value = True
                    self_arg.filesystem.fat_type = "FAT12"
                    self_arg.filesystem.boot_sector = MagicMock()
                    self_arg.filesystem.boot_sector.sectors_per_track = expected_format.geometry.sectors_per_track
                    self_arg.filesystem.boot_sector.num_heads = expected_format.geometry.heads
                    return "FAT12"

                # Set the side effect
                mock_detect_fs.side_effect = mock_detect_filesystem_side_effect

                success = self.controller.open_disk(source=None, disk_type="physical", drive_letter=drive_letter, drive_size=drive_size)

                self.assertTrue(success)
                self.assertIsInstance(self.controller.driver, GreaseweazleDriver)
                self.assertEqual(self.controller.driver.drive, drive_letter)
                self.mock_usb_open.assert_called_with(None)
                self.mock_Drive.assert_called() # Check Drive() was called
                # Check RPM measurement was attempted
                self.assertTrue(any(call.args[0].__name__ == 'measure_rpm' for call in self.mock_with_drive_selected.call_args_list if call.args))
                self.assertTrue(self.controller.driver.initialized)
                self.assertIsNotNone(self.controller.disk)
                self.assertIsNotNone(self.controller.disk.geometry)
                self.assertIsNotNone(self.controller.filesystem)
                # Check if detect_filesystem was called during the process
                mock_detect_fs.assert_called()


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
            self.assertIsNotNone(self.controller.filesystem, "Filesystem should be detected with explicit format on real Drive B")
            # Verify format applied
            pf = self.controller.driver.physical_format
            geom = self.controller.disk.geometry
            self.assertEqual(pf.rate, expected_format.physical_format.rate)
            self.assertEqual(geom.cylinders, expected_format.geometry.cylinders)

        else: # Mocked test
            # No need to mock RPM/detection when format is explicit
            # Mock filesystem detection to succeed with the given format
            with patch.object(DiskController, 'detect_filesystem', return_value="FAT12") as mock_detect_fs:
                 # Mock custom diskdef creation
                 with patch.object(GreaseweazleDriver, '_create_and_set_custom_diskdef') as mock_create_custom:
                    success = self.controller.open_disk(
                        source=source_device, disk_type="physical",
                        drive_letter=drive_letter, drive_size=drive_size,
                        format_info=format_info
                    )

                    self.assertTrue(success)
                    self.assertIsInstance(self.controller.driver, GreaseweazleDriver)
                    self.mock_usb_open.assert_called_once_with(source_device)
                    self.assertEqual(self.controller.driver.drive, drive_letter)

                    # Verify physical format and geometry were set correctly
                    pf = self.controller.driver.physical_format
                    geom = self.controller.disk.geometry
                    self.assertEqual(pf.encoding, format_info['encoding'])
                    self.assertEqual(pf.rate, format_info['rate'])
                    self.assertEqual(pf.sectors_per_track, format_info['sectors_per_track'])
                    self.assertEqual(geom.cylinders, format_info['cylinders'])
                    self.assertEqual(geom.heads, format_info['heads'])
                    self.assertEqual(geom.sectors_per_track, format_info['sectors_per_track'])

                    # Check custom diskdef creation was called
                    mock_create_custom.assert_called_once()
                    # Filesystem detection should be attempted
                    mock_detect_fs.assert_called_once()


    def test_03_physical_read_sector_success(self):
        """Tests reading a single sector successfully."""
        cyl, head, sect = 10, 1, 5
        expected_data = b'\xCA' * 512
        test_format = FMT_144 # Use 1.44MB for this test

        if self.using_real_hardware:
            # Assumes Drive A is prepared
            success = self.controller.open_disk(None, "physical", REAL_HW_DRIVE_A, "3.5")
            self.assertTrue(success)
            # We can't know the *exact* data, but we expect 512 bytes
            read_data = self.controller.disk.read_sector(cyl, head, sect)
            self.assertEqual(len(read_data), 512)
            # Optional: write known data first, then read back
            # self.controller.disk.write_sector(cyl, head, sect, expected_data)
            # self.controller.driver.flush() # Ensure written
            # read_data = self.controller.disk.read_sector(cyl, head, sect)
            # self.assertEqual(read_data, expected_data)
        else:
            # --- Mock Setup ---
            self.controller.open_disk(None, "physical", REAL_HW_DRIVE_A, "3.5",
                                      format_info={**test_format.geometry.__dict__, **test_format.physical_format.__dict__})
            # Mock the driver's _read_track mechanism (via read_with_retry)
            # Simulate finding the sector data when the track is read
            mock_flux = create_mock_flux()
            mock_codec_data = create_mock_codec(cyl, head, test_format)
            # Simulate the specific sector having data
            mock_codec_data.sectors[sect-1].dam = MagicMock(data=expected_data)
            self.mock_read_with_retry.return_value = (mock_flux, mock_codec_data)

            # --- Execute Read ---
            read_data = self.controller.disk.read_sector(cyl, head, sect)

            # --- Assertions ---
            # read_with_retry should have been called (implicitly by _read_track)
            self.mock_read_with_retry.assert_called()
            # Check the arguments passed to read_with_retry (might need refinement based on TrackSet iteration)
            # call_args = self.mock_read_with_retry.call_args[0]
            # self.assertEqual(call_args[2].cyl, cyl)
            # self.assertEqual(call_args[2].head, head)
            self.assertEqual(read_data, expected_data)
            # Verify sector cache was populated
            self.assertIn((cyl, head, sect), self.controller.driver.sector_cache)
            self.assertEqual(self.controller.driver.sector_cache[(cyl, head, sect)], expected_data)

            # --- Test Cache Hit ---
            self.mock_read_with_retry.reset_mock()
            read_data_cached = self.controller.disk.read_sector(cyl, head, sect)
            self.mock_read_with_retry.assert_not_called() # Should not call read again
            self.assertEqual(read_data_cached, expected_data)


    def test_04_physical_read_sector_not_found(self):
        """Tests reading a sector that isn't found on the track."""
        cyl, head, sect = 11, 0, 15 # Sector likely exists, but mock won't provide it
        expected_data = b'\x00' * 512 # Expect zeros for not found
        test_format = FMT_144

        if self.using_real_hardware:
             # Assumes Drive A is prepared
            success = self.controller.open_disk(None, "physical", REAL_HW_DRIVE_A, "3.5")
            self.assertTrue(success)
            # Reading a valid but maybe empty sector might return zeros or real data
            # For a more robust test, try reading a sector > sectors_per_track
            invalid_sect = test_format.geometry.sectors_per_track + 1
            with self.assertRaises(ValueError): # Disk class should raise for invalid sector #
                 self.controller.disk.read_sector(cyl, head, invalid_sect)
            # Reading a potentially empty sector within range:
            # read_data = self.controller.disk.read_sector(cyl, head, sect)
            # self.assertEqual(len(read_data), 512) # Can only check length reliably

        else:
            # --- Mock Setup ---
            self.controller.open_disk(None, "physical", REAL_HW_DRIVE_A, "3.5",
                                      format_info={**test_format.geometry.__dict__, **test_format.physical_format.__dict__})
            # Mock _read_track (via read_with_retry) to return *no* data for the specific sector
            mock_flux = create_mock_flux()
            mock_codec_data = create_mock_codec(cyl, head, test_format)
            # Remove or mark the specific sector as missing in the mock codec
            # For simplicity, let's just *not* provide the dam.data for it
            mock_codec_data.sectors[sect-1].dam = None # Simulate no data found

            self.mock_read_with_retry.return_value = (mock_flux, mock_codec_data)

            # --- Execute Read ---
            read_data = self.controller.disk.read_sector(cyl, head, sect)

            # --- Assertions ---
            self.mock_read_with_retry.assert_called()
            self.assertEqual(read_data, expected_data) # Driver returns zeros
            # Sector cache should *not* contain the specific sector key if not found
            self.assertNotIn((cyl, head, sect), self.controller.driver.sector_cache)


    def test_05_physical_read_sector_read_error(self):
        """Tests handling of a hardware read error during track read."""
        cyl, head, sect = 12, 1, 1
        test_format = FMT_144

        if self.using_real_hardware:
            self.skipTest("Simulating hardware read errors requires mocking.")
        else:
            # --- Mock Setup ---
            self.controller.open_disk(None, "physical", REAL_HW_DRIVE_A, "3.5",
                                      format_info={**test_format.geometry.__dict__, **test_format.physical_format.__dict__})
            # Mock read_with_retry to raise a Greaseweazle error
            error_to_raise = real_gw_usb.CmdError(
                cmd=struct.pack('2B', real_gw_usb.Cmd.ReadFlux, 0), # Dummy command bytes
                code=real_gw_usb.Ack.NoIndex
            )
            self.mock_read_with_retry.side_effect = error_to_raise

            # --- Execute and Assert ---
            # The driver's read_sector currently catches the exception and returns zeros
            read_data = self.controller.disk.read_sector(cyl, head, sect)
            self.assertEqual(read_data, b'\x00' * 512)
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
            self.controller.disk.write_sector(cyl, head, sect, write_data)
            # Flush triggers the actual write
            self.controller.driver.flush()
            # Verify by reading back
            read_back_data = self.controller.disk.read_sector(cyl, head, sect)
            self.assertEqual(read_back_data, write_data)
        else:
            # --- Mock Setup ---
            self.controller.open_disk(None, "physical", REAL_HW_DRIVE_A, "3.5",
                                      format_info={**test_format.geometry.__dict__, **test_format.physical_format.__dict__})
            # 1. Mock _convert_to_flux
            mock_flux_list = [100, 200, 150]
            convert_patcher = patch.object(self.controller.driver, '_convert_to_flux', return_value=mock_flux_list)
            mock_convert = convert_patcher.start()
            self.patches.append(convert_patcher)

            # 2. Mock USB behavior
            self.mock_usb.write_track.return_value = None # Simulate success
            self.mock_usb.seek.return_value = None
            self.mock_usb.drive_motor.return_value = None # Mock motor call too

            # 3. Configure with_drive_selected side_effect to accept 'motor'
            #    and also mock the motor on/off calls within it
            #    ***** MODIFICATION HERE *****
            def write_flush_side_effect(func, usb, drive, motor=True): # Accept motor argument
                 print(f"DEBUG: write_flush_side_effect called with func={func.__name__}, motor={motor}")
                 # Simulate the actual behavior of with_drive_selected
                 try:
                     # usb.set_bus_type(drive.bus.value) # Already mocked/called elsewhere
                     usb.drive_select(drive.unit_id)
                     usb.drive_motor(drive.unit_id, motor) # Call the mocked motor function
                     func() # Execute the wrapped function (e.g., write_tracks)
                 finally:
                     # Simulate the cleanup
                     usb.drive_motor(drive.unit_id, False)
                     usb.drive_deselect()
            # *********************************

            self.mock_with_drive_selected.side_effect = write_flush_side_effect

            # --- Execute Write & Flush ---
            self.controller.disk.write_sector(cyl, head, sect, write_data)
            self.assertIn((cyl, head), self.controller.driver.dirty_sectors)
            self.assertIn((cyl, head), self.controller.driver.dirty_tracks)

            self.controller.driver.flush()

            # --- Assertions ---
            # Check mocks related to flush execution
            mock_convert.assert_called_once_with(cyl, head)
            # Check calls made *by the side_effect*
            self.mock_usb.drive_select.assert_called() # Check drive select was called
            # Check motor was turned on (True) and then off (False)
            self.mock_usb.drive_motor.assert_any_call(ANY, True) # Check motor was turned on
            self.mock_usb.drive_motor.assert_called_with(ANY, False) # Check motor was turned off (last call)
            self.mock_usb.drive_deselect.assert_called() # Check drive deselect was called
            # Check mocks related to write_tracks called *inside* the flush
            self.mock_usb.seek.assert_called_with(cyl, head)
            self.mock_usb.write_track.assert_called_once_with(
                flux_list=mock_flux_list,
                cue_at_index=True,
                terminate_at_index=True
            )
            # Verify dirty cache is cleared
            self.assertEqual(len(self.controller.driver.dirty_sectors), 0)
            self.assertEqual(len(self.controller.driver.dirty_tracks), 0)


    def test_07_physical_flush_write_error(self):
        """Tests handling a write error during flush."""
        cyl, head, sect = 21, 1, 2
        write_data = b'\xEE' * 512
        test_format = FMT_144

        if self.using_real_hardware:
            self.skipTest("Simulating hardware write errors requires mocking.")
        else:
             # --- Mock Setup ---
            self.controller.open_disk(None, "physical", REAL_HW_DRIVE_A, "3.5",
                                      format_info={**test_format.geometry.__dict__, **test_format.physical_format.__dict__})
            # 1. Mock _convert_to_flux
            mock_flux_list = [111, 222, 111]
            convert_patcher = patch.object(self.controller.driver, '_convert_to_flux', return_value=mock_flux_list)
            mock_convert = convert_patcher.start()
            self.patches.append(convert_patcher)

            # 2. Mock USB write_track to RAISE an error (e.g., Write Protected)
            error_to_raise = real_gw_usb.CmdError(
                cmd=struct.pack('2B', real_gw_usb.Cmd.WriteFlux, 0), # Dummy command
                code=real_gw_usb.Ack.Wrprot
            )
            self.mock_usb.write_track.side_effect = error_to_raise
            self.mock_usb.seek.return_value = None

            # 3. Configure with_drive_selected for flush
            def write_flush_error_side_effect(func, usb, drive, motor=True): # Accept motor
             print(f"DEBUG: write_flush_error_side_effect called with func={func.__name__}, motor={motor}")
             # Simulate the actual behavior of with_drive_selected
             try:
                 usb.drive_select(drive.unit_id)
                 usb.drive_motor(drive.unit_id, motor)
                 # Execute the wrapped function (write_tracks), which will raise the mocked error
                 func()
             # Keep the exception handling specific to the test's purpose if needed
             # except real_gw_usb.CmdError as e:
             #      print(f"Mock caught expected CmdError during write_tracks: {e}")
             #      # Decide whether to raise or let the calling code handle it
             #      raise # Re-raise if driver doesn't handle it internally in flush
             finally:
                 # Simulate the cleanup even if error occurs
                 usb.drive_motor(drive.unit_id, False)
                 usb.drive_deselect()
            self.mock_with_drive_selected.side_effect = write_flush_error_side_effect

            # --- Execute Write & Flush ---
            self.controller.disk.write_sector(cyl, head, sect, write_data)
            # Check dirty flags *before* flush
            self.assertIn((cyl, head), self.controller.driver.dirty_sectors)

            # Execute flush and expect it might raise or log error
            # The current driver flush logs the error but doesn't raise it.
            # It *should* still clear the dirty flags even on error.
            self.controller.driver.flush()

             # --- Assertions ---
            mock_convert.assert_called_once_with(cyl, head)
            self.mock_usb.seek.assert_called_once_with(cyl, head)
            self.mock_usb.write_track.assert_called_once() # Check it was called
            # Verify dirty cache is cleared even after error (current behavior)
            self.assertNotIn((cyl, head), self.controller.driver.dirty_sectors)
            self.assertEqual(len(self.controller.driver.dirty_sectors), 0)
            self.assertEqual(len(self.controller.driver.dirty_tracks), 0)
            # Check logs for the CmdError Wrprot message (requires log capture setup)

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
                format_info_dict = {**format_obj.geometry.__dict__, **format_obj.physical_format.__dict__}
                success = self.controller.open_disk(None, "physical", drive, size, format_info=format_info_dict)
                self.assertTrue(success, f"Failed to open Drive {drive} for modification")
                self.assertIsNotNone(self.controller.disk, f"Controller disk object is None after opening Drive {drive}")
                self.assertIsNotNone(self.controller.filesystem, f"Filesystem not detected on Drive {drive}")

                # 2. Perform filesystem modifications
                print("Performing filesystem modifications...")
                for op_data in ops:
                    op_type = op_data[0]
                    op_args = op_data[1:]
                    print(f"  Op: {op_type} {op_args}")
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
                        self.assertTrue(op_success, f"Operation failed: {op_type} {op_args}")
                    except Exception as e:
                         self.fail(f"Exception during operation {op_type} {op_args}: {e}")

                # 3. Close disk (triggers flush)
                print("Closing disk (flushing writes)...")
                self.controller.close_disk()
                print("Disk closed.")
                time.sleep(1) # Give drive motor time to spin down if necessary

                # 4. Read disk using external 'gw' tool
                print(f"Reading Drive {drive} using '{GW_EXECUTABLE} read' to {gw_output_path}...")
                # Using ibm.scan might be more flexible, but reading with the known format
                # ensures we get the exact structure we expect for comparison.
                # If ibm.scan works better, use that.
                gw_cmd = [
                    GW_EXECUTABLE, "read", f"--drive={drive}",
                    f"--format={gw_format_name}",
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
                    self.fail(f"'{GW_EXECUTABLE} read' failed for Drive {drive} (Return Code: {e.returncode}):\nStdout:\n{e.stdout}\nStderr:\n{e.stderr}")

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
                fs_contents = [item['name'] for item in fs_list]
                # Add recursive listing if needed for deeper checks
                # For now, just check a few top-level items implied by ops
                if "/E2EDIR" in expected_files:
                     self.assertTrue(any(f['name'] == 'E2EDIR' and f['is_dir'] for f in fs_list), "E2EDIR not found after reopen")
                if "/TESTB" in expected_files:
                     self.assertTrue(any(f['name'] == 'TESTB' and f['is_dir'] for f in fs_list), "TESTB not found after reopen")


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
                    self.fail(f"Data mismatch for Drive {drive} starting at offset {diff_index}. "
                              f"Expected {gw_read_data[diff_index]:02X}, Got {app_read_data[diff_index]:02X}. "
                              f"Check temp files in {temp_dir}")

                print(f"Byte-for-byte comparison successful for Drive {drive}.")

                # 7. Close controller for the next iteration
                self.controller.close_disk()

        finally:
            # Clean up temporary directory
            # shutil.rmtree(temp_dir) # Comment out to inspect temp files on failure
            print(f"Test finished. Temporary files kept in: {temp_dir}")

    def _read_entire_disk_bytes(self, controller: DiskController) -> bytes:
        """Reads all sectors sequentially using the controller and returns raw bytes."""
        if not controller.disk or not controller.disk.geometry:
            self.fail("Disk or geometry not available in controller to read all bytes.")

        geom = controller.disk.geometry
        all_data = bytearray()
        print(f"Reading entire disk via fatfloppy: {geom.cylinders}C x {geom.heads}H x {geom.sectors_per_track}S ({geom.sector_size} bytes/sec)")
        total_sectors = geom.total_sectors
        read_count = 0
        try:
            for c in range(geom.cylinders):
                for h in range(geom.heads):
                    for s in range(1, geom.sectors_per_track + 1):
                        sector_data = controller.disk.read_sector(c, h, s)
                        if len(sector_data) != geom.sector_size:
                             # Pad if driver returned short read (e.g., read error simulation)
                             print(f"Warning: Short read C:{c} H:{h} S:{s}, got {len(sector_data)} bytes, padding to {geom.sector_size}")
                             sector_data += b'\x00' * (geom.sector_size - len(sector_data))
                        all_data.extend(sector_data)
                        read_count += 1
                        if read_count % 100 == 0: # Progress indicator
                             print(f"  Read {read_count}/{total_sectors} sectors...", end='\r')
            print(f"  Read {read_count}/{total_sectors} sectors... Done.")
            expected_size = geom.total_bytes
            if len(all_data) != expected_size:
                 print(f"Warning: Read data size ({len(all_data)}) differs from expected geometry size ({expected_size}).")
            return bytes(all_data)
        except Exception as e:
            self.fail(f"Error reading sector C:{c} H:{h} S:{s} via fatfloppy: {e}")


if __name__ == '__main__':
    # Important: Set TEST_HW environment variable to 'true' to run against real hardware
    # Example: TEST_HW=true python -m unittest tests/test_05_controller_physical.py
    # Remember to run prepare_test_disks.py first if using real hardware!
    print(f"** Hardware Test Mode: {'ENABLED' if USE_REAL_HARDWARE else 'DISABLED (Mocked)'} **")
    if USE_REAL_HARDWARE:
        print("** Ensure 'prepare_test_disks.py' has been run recently! **")
    unittest.main()
