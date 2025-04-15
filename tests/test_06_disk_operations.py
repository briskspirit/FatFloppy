# tests/test_06_disk_operations.py
import unittest
from unittest.mock import MagicMock, call, PropertyMock
import os
import sys
import tempfile
import shutil # Use shutil for setup

# Ensure src is in path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src')))

from fatfloppy.core.disk import Disk, DiskGeometry
from fatfloppy.core.drivers import RawImageDriver, PhysicalFormat # Import PhysicalFormat

class TestDiskOperations(unittest.TestCase):

    def setUp(self):
        print(f"\n--- Running test: {self.id()} ---")
        # Use RawImageDriver for easier data manipulation
        # Using smaller geometry for easier testing of boundaries
        self.geom = DiskGeometry(cylinders=2, heads=2, sectors_per_track=3, sector_size=128)
        self.total_bytes = self.geom.total_bytes
        # Create initial image data with identifiable pattern (C*100 + H*10 + S)
        self.initial_data = bytearray(self.total_bytes)
        offset = 0
        for c in range(self.geom.cylinders):
            for h in range(self.geom.heads):
                for s in range(1, self.geom.sectors_per_track + 1):
                    sector_val = c * 100 + h * 10 + s
                    # Ensure sector_val fits in a byte for the pattern
                    sector_byte = sector_val % 256
                    sector_data = bytes([sector_byte] * self.geom.sector_size)
                    if offset + self.geom.sector_size <= len(self.initial_data):
                        self.initial_data[offset:offset+self.geom.sector_size] = sector_data
                    offset += self.geom.sector_size
        # Ensure the buffer is exactly the right size
        self.initial_data = self.initial_data[:self.total_bytes]

        # Create a temporary file path for the driver
        self.temp_dir = tempfile.mkdtemp(prefix="fatfloppy_disk_ops_")
        self.img_path = os.path.join(self.temp_dir, "disk_ops.img")

        self.driver = RawImageDriver(file_path=self.img_path, image_data=bytes(self.initial_data))
        self.disk = Disk(self.driver)
        self.disk.set_geometry(self.geom)
        # Directly set physical format in driver as Disk.set_geometry would
        # Create a mock PhysicalFormat or a real one
        pf = PhysicalFormat(
            encoding="MFM", rate=500, rpm=300, # Dummy values
            sectors_per_track=self.geom.sectors_per_track,
            heads=self.geom.heads,
            sector_size=self.geom.sector_size
        )
        self.driver.set_physical_format(pf)

    def tearDown(self):
        print(f"--- Tearing down test: {self.id()} ---")
        # Clean up resources
        del self.disk
        del self.driver
        if hasattr(self, 'temp_dir') and os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)
        print(f"--- Finished test: {self.id()} ---")

    def test_01_read_sectors_single_track(self):
        """Test reading multiple sectors within a single track"""
        c, h, start_s = 0, 1, 1
        num_s = 2
        expected_len = num_s * self.geom.sector_size
        expected_data = bytearray()
        # Sector 1: C=0, H=1, S=1 => val=11
        expected_data.extend(bytes([11] * self.geom.sector_size))
        # Sector 2: C=0, H=1, S=2 => val=12
        expected_data.extend(bytes([12] * self.geom.sector_size))

        read_data = self.disk.read_sectors(c, h, start_s, num_s)

        self.assertEqual(len(read_data), expected_len)
        self.assertEqual(read_data, bytes(expected_data))

    def test_02_read_sectors_span_track(self):
        """Test reading multiple sectors spanning across a track boundary"""
        c, h, start_s = 0, 0, 2 # Start at C0 H0 S2
        num_s = 3 # Read S2, S3 (track end), S1 of next head (H1)
        expected_len = num_s * self.geom.sector_size
        expected_data = bytearray()
        # Sector 1: C=0, H=0, S=2 => val=2
        expected_data.extend(bytes([2] * self.geom.sector_size))
        # Sector 2: C=0, H=0, S=3 => val=3
        expected_data.extend(bytes([3] * self.geom.sector_size))
        # Sector 3: C=0, H=1, S=1 => val=11
        expected_data.extend(bytes([11] * self.geom.sector_size))

        read_data = self.disk.read_sectors(c, h, start_s, num_s)

        self.assertEqual(len(read_data), expected_len)
        self.assertEqual(read_data, bytes(expected_data))

    def test_03_read_sectors_span_cylinder(self):
        """Test reading multiple sectors spanning across a cylinder boundary"""
        c, h, start_s = 0, 1, 3 # Start at C0 H1 S3 (last sector of cylinder 0) => val=13
        num_s = 2 # Read C0 H1 S3, C1 H0 S1
        expected_len = num_s * self.geom.sector_size
        expected_data = bytearray()
        # Sector 1: C=0, H=1, S=3 => val=13
        expected_data.extend(bytes([13] * self.geom.sector_size))
        # Sector 2: C=1, H=0, S=1 => val=101
        expected_data.extend(bytes([101] * self.geom.sector_size))

        read_data = self.disk.read_sectors(c, h, start_s, num_s)

        self.assertEqual(len(read_data), expected_len)
        self.assertEqual(read_data, bytes(expected_data))

    def test_04_write_sectors_single_track(self):
        """Test writing multiple sectors within a single track"""
        c, h, start_s = 1, 0, 1
        num_s = 2
        write_data = bytes([0xAA] * self.geom.sector_size) + bytes([0xBB] * self.geom.sector_size)

        # Mock underlying single sector write to check calls
        self.disk.write_sector = MagicMock()
        self.disk.write_sectors(c, h, start_s, write_data)

        # Verify write_sector was called correctly for each sector
        expected_calls = [
            call(1, 0, 1, bytes([0xAA] * self.geom.sector_size)),
            call(1, 0, 2, bytes([0xBB] * self.geom.sector_size)),
        ]
        self.disk.write_sector.assert_has_calls(expected_calls)
        self.assertEqual(self.disk.write_sector.call_count, 2)


    def test_05_write_sectors_span_track_cylinder(self):
        """Test writing multiple sectors spanning track and cylinder"""
        c, h, start_s = 0, 1, 2 # Start C0 H1 S2
        num_s = 3 # Write S2, S3 (C0 H1), S1 (C1 H0)
        sector_size = self.geom.sector_size
        write_data = bytes([0x11] * sector_size) + \
                     bytes([0x22] * sector_size) + \
                     bytes([0x33] * sector_size)

        # Mock underlying single sector write to check calls
        self.disk.write_sector = MagicMock()
        self.disk.write_sectors(c, h, start_s, write_data)

        # Verify write_sector was called correctly for each sector
        expected_calls = [
            call(0, 1, 2, bytes([0x11] * sector_size)),
            call(0, 1, 3, bytes([0x22] * sector_size)),
            call(1, 0, 1, bytes([0x33] * sector_size)),
        ]
        self.disk.write_sector.assert_has_calls(expected_calls)
        self.assertEqual(self.disk.write_sector.call_count, 3)


    def test_06_write_sectors_padding(self):
        """Test writing data smaller than total sectors size, requiring padding"""
        c, h, start_s = 1, 1, 1
        num_s = 2 # Write to S1, S2
        sector_size = self.geom.sector_size
        partial_data_len = sector_size + 50 # Write 1 full sector + 50 bytes
        write_data = bytes([0xCC] * partial_data_len)
        expected_s1_data = bytes([0xCC] * sector_size)
        expected_s2_data = bytes([0xCC] * 50) + bytes([0x00] * (sector_size - 50))

        # Mock underlying single sector write to check calls
        self.disk.write_sector = MagicMock()
        self.disk.write_sectors(c, h, start_s, write_data)

        # Verify write_sector was called correctly with padding
        expected_calls = [
            call(1, 1, 1, expected_s1_data),
            call(1, 1, 2, expected_s2_data),
        ]
        self.disk.write_sector.assert_has_calls(expected_calls)
        self.assertEqual(self.disk.write_sector.call_count, 2)

    def test_07_disk_error_no_geometry_read(self):
        """Test read_sector fails if geometry is not set"""
        disk_no_geom = Disk(self.driver) # Fresh disk, no geometry
        with self.assertRaisesRegex(ValueError, "Disk geometry not set"):
            disk_no_geom.read_sector(0, 0, 1)
        with self.assertRaisesRegex(ValueError, "Disk geometry not set"):
            disk_no_geom.read_sectors(0, 0, 1, 1) # Also test multi-sector read

    def test_08_disk_error_no_geometry_write(self):
        """Test write_sector fails if geometry is not set"""
        disk_no_geom = Disk(self.driver) # Fresh disk, no geometry
        with self.assertRaisesRegex(ValueError, "Disk geometry not set"):
            disk_no_geom.write_sector(0, 0, 1, b'\x00'*128)
        with self.assertRaisesRegex(ValueError, "Disk geometry not set"):
            disk_no_geom.write_sectors(0, 0, 1, b'\x00'*128) # Also test multi-sector write

    def test_09_disk_error_invalid_address_read(self):
        """Test read_sector fails on invalid C/H/S"""
        with self.assertRaisesRegex(ValueError, "Invalid sector address"):
            self.disk.read_sector(self.geom.cylinders, 0, 1) # Invalid Cylinder
        with self.assertRaisesRegex(ValueError, "Invalid sector address"):
            self.disk.read_sector(0, self.geom.heads, 1) # Invalid Head
        with self.assertRaisesRegex(ValueError, "Invalid sector address"):
            self.disk.read_sector(0, 0, 0) # Invalid Sector (too low)
        with self.assertRaisesRegex(ValueError, "Invalid sector address"):
            self.disk.read_sector(0, 0, self.geom.sectors_per_track + 1) # Invalid Sector (too high)

    def test_10_disk_error_invalid_address_write(self):
        """Test write_sector fails on invalid C/H/S"""
        data = b'\x00'*self.geom.sector_size
        with self.assertRaisesRegex(ValueError, "Invalid sector address"):
            self.disk.write_sector(self.geom.cylinders, 0, 1, data) # Invalid Cylinder
        with self.assertRaisesRegex(ValueError, "Invalid sector address"):
            self.disk.write_sector(0, self.geom.heads, 1, data) # Invalid Head
        with self.assertRaisesRegex(ValueError, "Invalid sector address"):
            self.disk.write_sector(0, 0, 0, data) # Invalid Sector (too low)
        with self.assertRaisesRegex(ValueError, "Invalid sector address"):
            self.disk.write_sector(0, 0, self.geom.sectors_per_track + 1, data) # Invalid Sector (too high)

    def test_11_disk_error_invalid_write_size(self):
        """Test write_sector fails if data size doesn't match sector size"""
        with self.assertRaisesRegex(ValueError, "Data size .* does not match sector size"):
            self.disk.write_sector(0, 0, 1, b'\x00'*(self.geom.sector_size - 1))
        with self.assertRaisesRegex(ValueError, "Data size .* does not match sector size"):
            self.disk.write_sector(0, 0, 1, b'\x00'*(self.geom.sector_size + 1))

    def test_12_disk_set_geometry_updates_driver_format(self):
        """Test that Disk.set_geometry updates the driver's physical format"""
        # Initial state check (from setUp)
        self.assertEqual(self.driver.physical_format.sectors_per_track, 3)
        self.assertEqual(self.driver.physical_format.heads, 2)
        self.assertEqual(self.driver.physical_format.sector_size, 128)

        new_geom = DiskGeometry(cylinders=10, heads=1, sectors_per_track=5, sector_size=256)
        self.disk.set_geometry(new_geom)

        # Verify disk geometry updated
        self.assertEqual(self.disk.geometry, new_geom)
        # Verify driver physical format updated
        self.assertIsNotNone(self.driver.physical_format)
        self.assertEqual(self.driver.physical_format.sectors_per_track, 5)
        self.assertEqual(self.driver.physical_format.heads, 1)
        self.assertEqual(self.driver.physical_format.sector_size, 256)
        # Check other params were set to defaults if driver had no format before
        # (In this test, it had one from setUp, so they shouldn't change unless set_geometry modifies them)
        self.assertEqual(self.driver.physical_format.encoding, "MFM") # Check default wasn't overwritten

    def test_13_disk_set_geometry_creates_driver_format(self):
        """Test Disk.set_geometry creates driver physical format if none exists"""
        # Create driver without format
        driver_no_fmt = RawImageDriver(self.img_path, image_data=bytes(self.initial_data))
        disk_no_fmt = Disk(driver_no_fmt)
        self.assertIsNone(driver_no_fmt.physical_format)

        geom_to_set = DiskGeometry(cylinders=5, heads=1, sectors_per_track=8, sector_size=512)
        disk_no_fmt.set_geometry(geom_to_set)

        self.assertIsNotNone(driver_no_fmt.physical_format)
        self.assertEqual(driver_no_fmt.physical_format.sectors_per_track, 8)
        self.assertEqual(driver_no_fmt.physical_format.heads, 1)
        self.assertEqual(driver_no_fmt.physical_format.sector_size, 512)
        # Check defaults used for other parameters
        self.assertEqual(driver_no_fmt.physical_format.encoding, "MFM")
        self.assertEqual(driver_no_fmt.physical_format.rate, 500)
        self.assertEqual(driver_no_fmt.physical_format.rpm, 300)


if __name__ == '__main__':
    unittest.main()
