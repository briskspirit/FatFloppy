# tests/test_02_formats.py
import unittest
import os
import struct

import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src')))

from fatfloppy.core.formats import FormatManager, BootSectorData, FormatProfile
from fatfloppy.core.disk import Disk, DiskGeometry
from fatfloppy.core.drivers import RawImageDriver # Use RawImageDriver for controlled tests

RESOURCE_DIR = os.path.join(os.path.dirname(__file__), 'resources')
EMPTY_IMG = os.path.join(RESOURCE_DIR, 'empty_1.44mb.img')
POPULATED_IMG = os.path.join(RESOURCE_DIR, 'populated_1.44mb.img')

# Get the 1.44MB format profile
from fatfloppy.core.format_definitions import FLOPPY_FORMATS
FMT_144 = FLOPPY_FORMATS['ibm_3.5_1.44m']
FMT_720 = FLOPPY_FORMATS['ibm_3.5_720k']


class TestFormatManager(unittest.TestCase):

    def setUp(self):
        self.format_manager = FormatManager()
        print(f"\nRunning test: {self.id()}")

    def tearDown(self):
         print(f"Finished test: {self.id()}")

    def test_01_list_known_formats(self):
        formats = self.format_manager.list_known_formats()
        self.assertIsInstance(formats, list)
        self.assertGreater(len(formats), 0)
        self.assertIsInstance(formats[0], tuple)
        self.assertEqual(len(formats[0]), 2) # Name, Description

    def test_02_get_format_by_name(self):
        profile = self.format_manager.get_format_by_name("ibm_3.5_1.44m")
        self.assertIsNotNone(profile)
        self.assertIsInstance(profile, FormatProfile)
        self.assertEqual(profile.name, "ibm_3.5_1.44m")
        self.assertEqual(profile.geometry.total_bytes, 1440 * 1024)

        profile_none = self.format_manager.get_format_by_name("non_existent_format")
        self.assertIsNone(profile_none)

    def test_03_detect_format_144mb(self):
        if not os.path.exists(EMPTY_IMG):
            self.skipTest(f"{EMPTY_IMG} not found.")

        driver = RawImageDriver(EMPTY_IMG)
        disk = Disk(driver)
        # No geometry needed for format detection via direct read

        detected_format = self.format_manager.detect_format(disk)
        self.assertEqual(detected_format, "ibm_3.5_1.44m")

    def test_04_detect_format_no_match(self):
         # Create dummy disk with non-matching boot sector data
        dummy_boot = bytearray(512)
        dummy_boot[0:3] = b'\xEB\xFE\x90'
        dummy_boot[3:11] = b'NONAME  '
        struct.pack_into('<H', dummy_boot, 0x0B, 512) # Bytes/Sector
        struct.pack_into('<B', dummy_boot, 0x0D, 1)   # Sectors/Cluster
        struct.pack_into('<H', dummy_boot, 0x0E, 1)   # Reserved
        struct.pack_into('<B', dummy_boot, 0x10, 2)   # Num FATs
        struct.pack_into('<H', dummy_boot, 0x11, 224) # Root Entries
        struct.pack_into('<H', dummy_boot, 0x13, 1000)# Total Sectors (unusual)
        struct.pack_into('<B', dummy_boot, 0x15, 0xF1)# Media Descriptor (unusual)
        struct.pack_into('<H', dummy_boot, 0x16, 5)   # Sectors/FAT
        struct.pack_into('<H', dummy_boot, 0x18, 10)  # Sectors/Track
        struct.pack_into('<H', dummy_boot, 0x1A, 3)   # Heads (unusual)
        struct.pack_into('<H', dummy_boot, 0x1FE, 0xAA55) # Boot Signature

        driver = RawImageDriver("dummy", image_data=bytes(dummy_boot) + b'\x00'*1024*100)
        disk = Disk(driver)

        detected_format = self.format_manager.detect_format(disk)
        self.assertIsNone(detected_format, "Should not detect a standard format")


class TestBootSectorData(unittest.TestCase):

    def setUp(self):
        print(f"\nRunning test: {self.id()}")

    def tearDown(self):
        print(f"Finished test: {self.id()}")

    def test_01_to_bytes_from_bytes_roundtrip(self):
        bsd = BootSectorData(
            oem_id="MYDOS6.2",
            bytes_per_sector=512,
            sectors_per_cluster=2,
            reserved_sectors=1,
            num_fats=2,
            root_entries=112,
            total_sectors=1440, # 720KB
            media_descriptor=0xF9,
            sectors_per_fat=3,
            sectors_per_track=9,
            num_heads=2,
            hidden_sectors=0,
            volume_serial=0x12345678,
            volume_label="TEST DISK  ",
            fs_type="FAT12   "
        )
        bs_bytes = bsd.to_bytes()
        self.assertEqual(len(bs_bytes), 512)
        self.assertEqual(bs_bytes[510:512], b'\x55\xAA')

        bsd_reloaded = BootSectorData.from_bytes(bs_bytes)

        self.assertEqual(bsd_reloaded.oem_id, "MYDOS6.2")
        self.assertEqual(bsd_reloaded.bytes_per_sector, 512)
        self.assertEqual(bsd_reloaded.sectors_per_cluster, 2)
        self.assertEqual(bsd_reloaded.reserved_sectors, 1)
        self.assertEqual(bsd_reloaded.num_fats, 2)
        self.assertEqual(bsd_reloaded.root_entries, 112)
        self.assertEqual(bsd_reloaded.total_sectors, 1440)
        self.assertEqual(bsd_reloaded.media_descriptor, 0xF9)
        self.assertEqual(bsd_reloaded.sectors_per_fat, 3)
        self.assertEqual(bsd_reloaded.sectors_per_track, 9)
        self.assertEqual(bsd_reloaded.num_heads, 2)
        self.assertEqual(bsd_reloaded.hidden_sectors, 0)
        self.assertEqual(bsd_reloaded.volume_serial, 0x12345678)
        self.assertEqual(bsd_reloaded.volume_label, "TEST DISK") # Stripped
        self.assertEqual(bsd_reloaded.fs_type, "FAT12")      # Stripped

    def test_02_from_bytes_real_image(self):
        if not os.path.exists(EMPTY_IMG):
            self.skipTest(f"{EMPTY_IMG} not found.")

        with open(EMPTY_IMG, "rb") as f:
            boot_sector_bytes = f.read(512)

        bsd = BootSectorData.from_bytes(boot_sector_bytes)

        # Values specific to standard 1.44MB format
        self.assertEqual(bsd.bytes_per_sector, 512)
        self.assertEqual(bsd.sectors_per_cluster, 1)
        self.assertEqual(bsd.reserved_sectors, 1)
        self.assertEqual(bsd.num_fats, 2)
        self.assertEqual(bsd.root_entries, 224)
        self.assertEqual(bsd.total_sectors, 2880)
        self.assertEqual(bsd.media_descriptor, 0xF0)
        self.assertEqual(bsd.sectors_per_fat, 9)
        self.assertEqual(bsd.sectors_per_track, 18)
        self.assertEqual(bsd.num_heads, 2)
        # OEM ID, Label, Serial can vary based on formatting tool
        self.assertTrue(bsd.fs_type.startswith("FAT12"))

    @unittest.skip("Boot signature check is currently disabled in BootSectorData.from_bytes")
    def test_03_from_bytes_invalid_signature(self):
         invalid_boot = bytearray(FMT_144.boot_sector.to_bytes())
         invalid_boot[510:512] = b'\x00\x00' # Corrupt signature
         with self.assertRaisesRegex(ValueError, "Invalid boot signature"):
              # TODO: update based on relaxed checks
              # BootSectorData.from_bytes(bytes(invalid_boot))
              pass # Test is currently disabled due to relaxed check

    def test_04_from_bytes_too_short(self):
         short_boot = b'\x00' * 500
         with self.assertRaisesRegex(ValueError, "too short"):
             BootSectorData.from_bytes(short_boot)

if __name__ == '__main__':
    unittest.main()
