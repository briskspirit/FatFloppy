# tests/test_01_drivers.py
import unittest
import os
import tempfile
import shutil

# Ensure src is in path or install the package
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src')))

from fatfloppy.core.drivers import RawImageDriver, PhysicalFormat
from fatfloppy.core.format_definitions import FLOPPY_FORMATS

RESOURCE_DIR = os.path.join(os.path.dirname(__file__), 'resources')
EMPTY_IMG = os.path.join(RESOURCE_DIR, 'empty_1.44mb.img')
TEST_FILE_TXT = os.path.join(RESOURCE_DIR, 'test_file.txt')

# Get the 1.44MB format profile
FMT_144 = FLOPPY_FORMATS['ibm_3.5_1.44m']

class TestRawImageDriver(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="fatfloppy_test_")
        self.test_img_path = os.path.join(self.temp_dir, "test_1.44mb.img")
        # Copy a fresh image for each test
        if os.path.exists(EMPTY_IMG):
            shutil.copy(EMPTY_IMG, self.test_img_path)
        else:
            # Create a dummy zeroed file if base image is missing
            print(f"Warning: {EMPTY_IMG} not found. Creating zeroed file.")
            with open(self.test_img_path, "wb") as f:
                f.write(b'\x00' * FMT_144.geometry.total_bytes)

        self.driver = RawImageDriver(self.test_img_path)
        self.driver.set_physical_format(FMT_144.physical_format)
        print(f"\nRunning test: {self.id()}") # Print test name

    def tearDown(self):
        # Explicitly release file handles if necessary (driver doesn't hold open)
        del self.driver
        shutil.rmtree(self.temp_dir)
        print(f"Finished test: {self.id()}")

    def test_01_initialization_from_file(self):
        self.assertTrue(os.path.exists(self.test_img_path))
        self.assertGreater(len(self.driver.image_data), 0)
        self.assertEqual(len(self.driver.image_data), FMT_144.geometry.total_bytes)

    def test_02_initialization_from_bytes(self):
        initial_data = b'\xAA' * 512 * 10 # 10 sectors worth
        driver_bytes = RawImageDriver("dummy_path_not_used.img", image_data=initial_data)
        driver_bytes.set_physical_format(FMT_144.physical_format)
        self.assertEqual(driver_bytes.image_data, bytearray(initial_data))
        self.assertTrue(driver_bytes.dirty) # Should be dirty if initialized from data

    def test_03_set_physical_format(self):
        self.assertIsNotNone(self.driver.physical_format)
        self.assertEqual(self.driver.physical_format.sectors_per_track, FMT_144.geometry.sectors_per_track)
        self.assertEqual(self.driver.physical_format.heads, FMT_144.geometry.heads)
        self.assertEqual(self.driver.physical_format.sector_size, FMT_144.geometry.sector_size)
        self.assertTrue(self.driver.geometry_set)

    def test_04_read_sector(self):
        # Read boot sector (assuming it's formatted)
        boot_sector = self.driver.read_sector(0, 0, 1)
        self.assertEqual(len(boot_sector), 512)
        # Check boot signature if image is properly formatted
        if os.path.exists(EMPTY_IMG):
             self.assertEqual(boot_sector[510:512], b'\x55\xAA')

    def test_05_write_sector_and_flush(self):
        test_data = b'TEST' + b'\xEE' * (FMT_144.geometry.sector_size - 4)
        cyl, head, sect = 5, 1, 3

        # Write data
        self.driver.write_sector(cyl, head, sect, test_data)
        self.assertTrue(self.driver.dirty)

        # Verify data is in memory buffer
        offset = self.driver._calculate_sector_offset(cyl, head, sect)
        self.assertEqual(self.driver.image_data[offset:offset+len(test_data)], test_data)

        # Flush to disk
        self.driver.flush()
        self.assertFalse(self.driver.dirty)

        # Create a new driver instance to read from the flushed file
        driver2 = RawImageDriver(self.test_img_path)
        driver2.set_physical_format(FMT_144.physical_format)
        read_data = driver2.read_sector(cyl, head, sect)
        self.assertEqual(read_data, test_data)

    def test_06_read_beyond_image_size(self):
        # Use geometry to find a sector just outside the image
        geom = FMT_144.geometry
        cyl, head, sect = geom.cylinders, 0, 1 # Invalid cylinder

        with self.assertRaises(ValueError): # Disk class should raise this based on geometry
             # Need Disk object to enforce geometry check before driver read
             from fatfloppy.core.disk import Disk
             disk = Disk(self.driver)
             disk.set_geometry(geom)
             disk.read_sector(cyl, head, sect)

        # Test driver's behavior directly (returns zeros for reads past end)
        driver_offset = self.driver._calculate_sector_offset(geom.cylinders-1, geom.heads-1, geom.sectors_per_track)
        last_sector_data = self.driver.read_sector(geom.cylinders-1, geom.heads-1, geom.sectors_per_track)
        self.assertEqual(len(last_sector_data), geom.sector_size)
        # Now try reading past this offset directly
        read_data = self.driver.read_bytes_direct(len(self.driver.image_data), geom.sector_size)
        self.assertEqual(read_data, b'\x00' * geom.sector_size)


    def test_07_write_extends_image(self):
        initial_size = len(self.driver.image_data)
        test_data = b'EXTEND' * 100 # More than one sector
        sector_size = FMT_144.geometry.sector_size

        # Calculate offset near the end to force extension
        cyl, head, sect = FMT_144.geometry.cylinders -1, FMT_144.geometry.heads -1, FMT_144.geometry.sectors_per_track
        offset = self.driver._calculate_sector_offset(cyl, head, sect)

        # Write data that goes past the end
        self.driver.write_sector(cyl, head, sect, test_data[:sector_size]) # Write first part
        # Write second part to next logical sector (which needs extending)
        self.driver.write_sector(cyl+1, 0, 1, test_data[sector_size:])

        expected_size = offset + len(test_data) # Approximate, depends on exact sector calc
        self.assertGreater(len(self.driver.image_data), initial_size)
        # More precise check: size should be at least offset + data length
        self.assertGreaterEqual(len(self.driver.image_data), offset + len(test_data))

        self.assertTrue(self.driver.dirty)
        self.driver.flush()
        self.assertFalse(self.driver.dirty)

        # Verify file size on disk
        self.assertGreaterEqual(os.path.getsize(self.test_img_path), offset + len(test_data))

        # Verify data can be read back
        driver2 = RawImageDriver(self.test_img_path)
        driver2.set_physical_format(FMT_144.physical_format)
        read_data1 = driver2.read_sector(cyl, head, sect)
        read_data2 = driver2.read_sector(cyl+1, 0, 1) # Assumes geometry allows cyl+1
        self.assertEqual(read_data1 + read_data2[:len(test_data)-sector_size], test_data)


    def test_08_read_bytes_direct(self):
        # Read boot sector signature directly
        data = self.driver.read_bytes_direct(510, 2)
        if os.path.exists(EMPTY_IMG):
             self.assertEqual(data, b'\x55\xAA')

        # Read past end of file
        read_data = self.driver.read_bytes_direct(len(self.driver.image_data) - 10, 20)
        self.assertEqual(len(read_data), 20)
        self.assertTrue(read_data.endswith(b'\x00' * 10)) # Check padding


# Note: GreaseweazleDriver tests are harder to automate without hardware or
# extensive mocking. They are omitted here but would follow a similar pattern,
# mocking the 'greaseweazle' library interactions.

if __name__ == '__main__':
    unittest.main()
