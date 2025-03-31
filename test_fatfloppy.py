import os
import sys
import json
import unittest
import tempfile
import shutil
import filecmp
import datetime
from typing import Dict, List, Tuple, Any

# Add the parent directory to the path for importing the application modules
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from floppybpb import FloppyBPB
from fat import FAT12FileSystem
from diskmanager import FloppyDiskManager, ImageFileManager


class TestUtilities:
    """Utility methods for test setup and verification"""

    @staticmethod
    def setup_test_environment():
        """Create test directories if they don't exist"""
        test_dirs = [
            'test_data',
            'test_data/master_360kb',
            'test_data/master_360kb/files',
            'test_data/master_1440kb',
            'test_data/master_1440kb/files',
            'test_data/temp'
        ]

        for directory in test_dirs:
            os.makedirs(directory, exist_ok=True)

    @staticmethod
    def create_temp_image_copy(source_image_path: str) -> str:
        """Create a temporary copy of a disk image for testing"""
        filename = os.path.basename(source_image_path)
        temp_path = os.path.join('test_data/temp', f"temp_{filename}")

        shutil.copy2(source_image_path, temp_path)
        return temp_path

    @staticmethod
    def compare_files(file1: str, file2: str) -> bool:
        """Compare two files to check if they're identical"""
        return filecmp.cmp(file1, file2, shallow=False)

    @staticmethod
    def compare_directories(dir1: str, dir2: str) -> Tuple[bool, List[str]]:
        """Compare two directories recursively"""
        dcmp = filecmp.dircmp(dir1, dir2)

        # Gather the differences
        differences = []

        if dcmp.left_only:
            differences.append(f"Files only in {dir1}: {dcmp.left_only}")

        if dcmp.right_only:
            differences.append(f"Files only in {dir2}: {dcmp.right_only}")

        if dcmp.diff_files:
            differences.append(f"Files that differ: {dcmp.diff_files}")

        # Check subdirectories recursively
        for sub_dir in dcmp.common_dirs:
            sub_equal, sub_diffs = TestUtilities.compare_directories(
                os.path.join(dir1, sub_dir),
                os.path.join(dir2, sub_dir)
            )
            if not sub_equal:
                differences.extend(sub_diffs)

        return len(differences) == 0, differences

    @staticmethod
    def extract_all_files(filesystem: FAT12FileSystem, output_dir: str) -> List[str]:
        """Extract all files from a filesystem to a directory"""
        extracted_files = []

        # Create output directory if it doesn't exist
        os.makedirs(output_dir, exist_ok=True)

        # Get the list of all files
        files = filesystem.list_files()

        # Extract each file
        for file_info in files:
            if not file_info['is_dir']:
                try:
                    # Get the file data
                    file_path = file_info['name']
                    file_data = filesystem.extract_file(file_path)

                    # Determine the output path
                    local_path = file_path.lstrip('/')
                    output_path = os.path.join(output_dir, local_path)

                    # Create directory if needed
                    os.makedirs(os.path.dirname(output_path), exist_ok=True)

                    # Write the file
                    with open(output_path, 'wb') as f:
                        f.write(file_data)

                    extracted_files.append(output_path)
                except Exception as e:
                    print(f"Error extracting {file_path}: {e}")

        return extracted_files

    @staticmethod
    def get_file_list(directory: str) -> List[str]:
        """Get a list of all files in a directory recursively"""
        file_list = []

        for root, _, files in os.walk(directory):
            for file in files:
                file_path = os.path.join(root, file)
                relative_path = os.path.relpath(file_path, directory)
                file_list.append(relative_path)

        return sorted(file_list)

    @staticmethod
    def save_bpb_info(bpb: FloppyBPB, output_path: str) -> None:
        """Save BPB information to a JSON file"""
        info = {
            'bytes_per_sector': bpb.bytes_per_sector,
            'sectors_per_cluster': bpb.sectors_per_cluster,
            'reserved_sectors': bpb.reserved_sectors,
            'num_fats': bpb.num_fats,
            'root_entries': bpb.root_entries,
            'total_sectors': bpb.total_sectors,
            'media_descriptor': bpb.media_descriptor,
            'sectors_per_fat': bpb.sectors_per_fat,
            'sectors_per_track': bpb.sectors_per_track,
            'num_heads': bpb.num_heads,
            'hidden_sectors': bpb.hidden_sectors,
            'total_sectors_large': bpb.total_sectors_large,
            'drive_number': bpb.drive_number,
            'volume_label': bpb.volume_label,
            'fs_type': bpb.fs_type,
            'oem_id': bpb.oem_id
        }

        with open(output_path, 'w') as f:
            json.dump(info, f, indent=2)

    @staticmethod
    def load_bpb_info(input_path: str) -> Dict[str, Any]:
        """Load BPB information from a JSON file"""
        with open(input_path, 'r') as f:
            return json.load(f)


class TestFloppyBPB(unittest.TestCase):
    """Tests for the FloppyBPB class"""

    def setUp(self):
        TestUtilities.setup_test_environment()

        # Paths to master images
        self.image_360kb = "test_data/master_360kb/disk_360kb.img"
        self.image_1440kb = "test_data/master_1440kb/disk_1440kb.img"

        # Create temporary image copies for testing
        self.temp_360kb = TestUtilities.create_temp_image_copy(self.image_360kb)
        self.temp_1440kb = TestUtilities.create_temp_image_copy(self.image_1440kb)

        # Create disk managers for each image
        self.disk_manager_360kb = ImageFileManager(self.temp_360kb)
        self.disk_manager_1440kb = ImageFileManager(self.temp_1440kb)

        # Create BPB instances
        self.bpb_360kb = FloppyBPB(self.disk_manager_360kb)
        self.bpb_1440kb = FloppyBPB(self.disk_manager_1440kb)

    def test_bpb_detection_360kb(self):
        """Test BPB detection for 360KB floppy"""
        # Load expected BPB values
        expected_bpb = TestUtilities.load_bpb_info("test_data/master_360kb/bpb_info.json")

        # Check key BPB parameters
        self.assertEqual(self.bpb_360kb.bytes_per_sector, expected_bpb['bytes_per_sector'])
        self.assertEqual(self.bpb_360kb.sectors_per_cluster, expected_bpb['sectors_per_cluster'])
        self.assertEqual(self.bpb_360kb.reserved_sectors, expected_bpb['reserved_sectors'])
        self.assertEqual(self.bpb_360kb.num_fats, expected_bpb['num_fats'])
        self.assertEqual(self.bpb_360kb.root_entries, expected_bpb['root_entries'])
        self.assertEqual(self.bpb_360kb.total_sectors, expected_bpb['total_sectors'])
        self.assertEqual(self.bpb_360kb.sectors_per_fat, expected_bpb['sectors_per_fat'])
        self.assertEqual(self.bpb_360kb.sectors_per_track, expected_bpb['sectors_per_track'])
        self.assertEqual(self.bpb_360kb.num_heads, expected_bpb['num_heads'])

        # Verify disk type
        disk_type = self.bpb_360kb.get_disk_type()
        self.assertIn("5.25\"", disk_type)
        self.assertIn("360", disk_type)

    def test_bpb_detection_1440kb(self):
        """Test BPB detection for 1.44MB floppy"""
        # Load expected BPB values
        expected_bpb = TestUtilities.load_bpb_info("test_data/master_1440kb/bpb_info.json")

        # Check key BPB parameters
        self.assertEqual(self.bpb_1440kb.bytes_per_sector, expected_bpb['bytes_per_sector'])
        self.assertEqual(self.bpb_1440kb.sectors_per_cluster, expected_bpb['sectors_per_cluster'])
        self.assertEqual(self.bpb_1440kb.reserved_sectors, expected_bpb['reserved_sectors'])
        self.assertEqual(self.bpb_1440kb.num_fats, expected_bpb['num_fats'])
        self.assertEqual(self.bpb_1440kb.root_entries, expected_bpb['root_entries'])
        self.assertEqual(self.bpb_1440kb.total_sectors, expected_bpb['total_sectors'])
        self.assertEqual(self.bpb_1440kb.sectors_per_fat, expected_bpb['sectors_per_fat'])
        self.assertEqual(self.bpb_1440kb.sectors_per_track, expected_bpb['sectors_per_track'])
        self.assertEqual(self.bpb_1440kb.num_heads, expected_bpb['num_heads'])

        # Verify disk type
        disk_type = self.bpb_1440kb.get_disk_type()
        self.assertIn("3.5\"", disk_type)
        self.assertIn("1.44", disk_type)

    def test_create_boot_sector(self):
        """Test creation of a boot sector with specified parameters"""
        # Define parameters for a 1.44MB floppy
        params = {
            'bytes_per_sector': 512,
            'sectors_per_cluster': 1,
            'reserved_sectors': 1,
            'num_fats': 2,
            'root_entries': 224,
            'total_sectors': 2880,
            'media_descriptor': 0xF0,
            'sectors_per_fat': 9,
            'sectors_per_track': 18,
            'num_heads': 2,
            'hidden_sectors': 0,
            'total_sectors_large': 0,
            'drive_number': 0,
            'volume_label': 'TEST FLOPPY',
            'fs_type': 'FAT12',
            'oem_id': 'TESTDISK'
        }

        # Create boot sector
        boot_sector = FloppyBPB.create_boot_sector(params)

        # Write to a temporary file
        temp_file = os.path.join("test_data/temp", "test_boot_sector.bin")
        with open(temp_file, 'wb') as f:
            f.write(boot_sector)

        # Create a disk manager for the temporary file
        temp_manager = ImageFileManager(temp_file)

        # Parse the boot sector
        bpb = FloppyBPB(temp_manager)

        # Verify key parameters
        self.assertEqual(bpb.bytes_per_sector, params['bytes_per_sector'])
        self.assertEqual(bpb.sectors_per_cluster, params['sectors_per_cluster'])
        self.assertEqual(bpb.reserved_sectors, params['reserved_sectors'])
        self.assertEqual(bpb.num_fats, params['num_fats'])
        self.assertEqual(bpb.root_entries, params['root_entries'])
        self.assertEqual(bpb.total_sectors, params['total_sectors'])
        self.assertEqual(bpb.sectors_per_fat, params['sectors_per_fat'])
        self.assertEqual(bpb.sectors_per_track, params['sectors_per_track'])
        self.assertEqual(bpb.num_heads, params['num_heads'])
        self.assertEqual(bpb.oem_id, params['oem_id'])
        self.assertEqual(bpb.volume_label.strip(), params['volume_label'])
        self.assertEqual(bpb.fs_type.strip(), params['fs_type'])


class TestFAT12FileSystem(unittest.TestCase):
    """Tests for the FAT12FileSystem class"""

    def setUp(self):
        TestUtilities.setup_test_environment()

        # Paths to master images
        self.image_360kb = "test_data/master_360kb/disk_360kb.img"
        self.image_1440kb = "test_data/master_1440kb/disk_1440kb.img"

        # Create temporary image copies for testing
        self.temp_360kb = TestUtilities.create_temp_image_copy(self.image_360kb)
        self.temp_1440kb = TestUtilities.create_temp_image_copy(self.image_1440kb)

        # Create disk managers for each image
        self.disk_manager_360kb = ImageFileManager(self.temp_360kb)
        self.disk_manager_1440kb = ImageFileManager(self.temp_1440kb)

        # Create BPB instances
        self.bpb_360kb = FloppyBPB(self.disk_manager_360kb)
        self.bpb_1440kb = FloppyBPB(self.disk_manager_1440kb)

        # Create filesystem instances
        self.fs_360kb = FAT12FileSystem(
            self.disk_manager_360kb.read_bytes,
            self.disk_manager_360kb.write_bytes,
            self.disk_manager_360kb.flush,
            self.bpb_360kb.get_fat12_params()
        )

        self.fs_1440kb = FAT12FileSystem(
            self.disk_manager_1440kb.read_bytes,
            self.disk_manager_1440kb.write_bytes,
            self.disk_manager_1440kb.flush,
            self.bpb_1440kb.get_fat12_params()
        )

        # Create working directories
        self.working_dir_360kb = os.path.join("test_data/temp", "working_360kb")
        self.working_dir_1440kb = os.path.join("test_data/temp", "working_1440kb")
        os.makedirs(self.working_dir_360kb, exist_ok=True)
        os.makedirs(self.working_dir_1440kb, exist_ok=True)

    def test_list_files_360kb(self):
        """Test listing files from 360KB floppy"""
        files = self.fs_360kb.list_files()
        self.assertGreater(len(files), 0)

        # Verify we have a mix of files and directories
        has_files = any(not f['is_dir'] for f in files)
        has_dirs = any(f['is_dir'] for f in files)

        self.assertTrue(has_files, "No files found in filesystem")

        # Print file list for debugging
        for f in files:
            file_type = "DIR" if f['is_dir'] else "FILE"
            print(f"{file_type}: {f['name']} - {f['size']} bytes")

    def test_extract_file_360kb(self):
        """Test extracting files from 360KB floppy"""
        files = self.fs_360kb.list_files()

        # Find a non-directory file to extract
        test_file = next((f for f in files if not f['is_dir']), None)
        self.assertIsNotNone(test_file, "No files found for extraction test")

        # Extract the file
        file_path = test_file['name']
        file_data = self.fs_360kb.extract_file(file_path)

        # Verify file was extracted
        self.assertIsNotNone(file_data)
        self.assertEqual(len(file_data), test_file['size'])

        # Write to a temporary file for further testing
        output_path = os.path.join(self.working_dir_360kb, os.path.basename(file_path))
        with open(output_path, 'wb') as f:
            f.write(file_data)

        self.assertTrue(os.path.exists(output_path))

    def test_create_directory_360kb(self):
        """Test creating directories on 360KB floppy"""
        # Create a test directory in the root
        test_dir_name = "TESTDIR"
        self.fs_360kb.create_directory("/", test_dir_name, datetime.datetime.now())

        # Verify directory was created
        files = self.fs_360kb.list_files()
        test_dir = next((f for f in files if f['is_dir'] and f['name'] == test_dir_name), None)

        self.assertIsNotNone(test_dir, f"Directory '{test_dir_name}' not found after creation")

        # Create a subdirectory
        subdir_name = "SUBDIR"
        self.fs_360kb.create_directory(f"/{test_dir_name}", subdir_name, datetime.datetime.now())

        # Verify subdirectory was created
        files = self.fs_360kb.list_files()
        subdir = next((f for f in files if f['is_dir'] and f['name'] == f"{test_dir_name}/{subdir_name}"), None)

        self.assertIsNotNone(subdir, f"Subdirectory '{subdir_name}' not found after creation")

    def test_delete_file_360kb(self):
        """Test deleting files from 360KB floppy"""
        # Find a file to delete
        files = self.fs_360kb.list_files()
        test_file = next((f for f in files if not f['is_dir']), None)
        self.assertIsNotNone(test_file, "No files found for deletion test")

        # Get the file path
        file_path = test_file['name']

        # Delete the file
        self.fs_360kb.delete_item(file_path)

        # Verify file was deleted
        files_after = self.fs_360kb.list_files()
        deleted_file = next((f for f in files_after if f['name'] == file_path), None)

        self.assertIsNone(deleted_file, f"File {file_path} still exists after deletion")

    def test_insert_file_360kb(self):
        """Test inserting files to 360KB floppy"""
        # Create test data
        test_data = b"This is a test file created for FAT12FileSystem testing."

        # Insert the file to the root directory
        file_name = "TESTFILE.TXT"
        file_datetime = datetime.datetime.now()
        self.fs_360kb.insert_file("/", file_name, test_data, file_datetime)

        # Verify file was inserted
        files = self.fs_360kb.list_files()
        test_file = next((f for f in files if f['name'] == file_name), None)

        self.assertIsNotNone(test_file, f"File '{file_name}' not found after insertion")
        self.assertEqual(test_file['size'], len(test_data))

        # Extract the file and verify contents
        file_data = self.fs_360kb.extract_file(f"/{file_name}")
        self.assertEqual(file_data, test_data)

    def test_file_operations_workflow_360kb(self):
        """Test a complete workflow of file operations on 360KB floppy"""
        # 1. List initial files
        initial_files = self.fs_360kb.list_files()
        initial_file_count = len(initial_files)

        # 2. Create directories
        test_dir = "TESTDIR"
        sub_dir = "SUBDIR"

        self.fs_360kb.create_directory("/", test_dir, datetime.datetime.now())
        self.fs_360kb.create_directory(f"/{test_dir}", sub_dir, datetime.datetime.now())

        # 3. Insert files at different levels
        root_file_data = b"This is a test file in the root directory."
        dir_file_data = b"This is a test file in the test directory."
        subdir_file_data = b"This is a test file in the subdirectory."

        self.fs_360kb.insert_file("/", "ROOT.TXT", root_file_data, datetime.datetime.now())
        self.fs_360kb.insert_file(f"/{test_dir}", "DIR.TXT", dir_file_data, datetime.datetime.now())
        self.fs_360kb.insert_file(f"/{test_dir}/{sub_dir}", "SUB.TXT", subdir_file_data, datetime.datetime.now())

        # 4. List files again to verify additions
        files_after_addition = self.fs_360kb.list_files()
        self.assertEqual(len(files_after_addition), initial_file_count + 5)  # +2 dirs, +3 files

        # 5. Extract and verify file contents
        root_data = self.fs_360kb.extract_file("/ROOT.TXT")
        dir_data = self.fs_360kb.extract_file(f"/{test_dir}/DIR.TXT")
        subdir_data = self.fs_360kb.extract_file(f"/{test_dir}/{sub_dir}/SUB.TXT")

        self.assertEqual(root_data, root_file_data)
        self.assertEqual(dir_data, dir_file_data)
        self.assertEqual(subdir_data, subdir_file_data)

        # 6. Delete files and verify deletion
        self.fs_360kb.delete_item("/ROOT.TXT")

        files_after_deletion = self.fs_360kb.list_files()
        deleted_file = next((f for f in files_after_deletion if f['name'] == "/ROOT.TXT"), None)
        self.assertIsNone(deleted_file)

        # 7. Delete directory with contents (should fail)
        with self.assertRaises(ValueError):
            self.fs_360kb.delete_item(f"/{test_dir}")

        # 8. Delete files in directory, then the directory
        self.fs_360kb.delete_item(f"/{test_dir}/DIR.TXT")
        self.fs_360kb.delete_item(f"/{test_dir}/{sub_dir}/SUB.TXT")
        self.fs_360kb.delete_item(f"/{test_dir}/{sub_dir}")
        self.fs_360kb.delete_item(f"/{test_dir}")

        # 9. Verify all test items are gone
        final_files = self.fs_360kb.list_files()
        self.assertEqual(len(final_files), initial_file_count)

    def test_list_files_1440kb(self):
        """Test listing files from 1.44MB floppy"""
        files = self.fs_1440kb.list_files()
        self.assertGreater(len(files), 0)

        # Verify we have a mix of files and directories
        has_files = any(not f['is_dir'] for f in files)
        has_dirs = any(f['is_dir'] for f in files)

        self.assertTrue(has_files, "No files found in filesystem")

        # Print file list for debugging
        for f in files:
            file_type = "DIR" if f['is_dir'] else "FILE"
            print(f"{file_type}: {f['name']} - {f['size']} bytes")

    def test_extract_file_1440kb(self):
        """Test extracting files from 1.44MB floppy"""
        files = self.fs_1440kb.list_files()

        # Find a non-directory file to extract
        test_file = next((f for f in files if not f['is_dir']), None)
        self.assertIsNotNone(test_file, "No files found for extraction test")

        # Extract the file
        file_path = test_file['name']
        file_data = self.fs_1440kb.extract_file(f"/{file_path}" if not file_path.startswith('/') else file_path)

        # Verify file was extracted
        self.assertIsNotNone(file_data)
        self.assertEqual(len(file_data), test_file['size'])

        # Write to a temporary file for further testing
        output_path = os.path.join(self.working_dir_1440kb, os.path.basename(file_path))
        with open(output_path, 'wb') as f:
            f.write(file_data)

        self.assertTrue(os.path.exists(output_path))

    def test_create_directory_1440kb(self):
        """Test creating directories on 1.44MB floppy"""
        # Create a test directory in the root
        test_dir_name = "TESTDIR"
        self.fs_1440kb.create_directory("/", test_dir_name, datetime.datetime.now())

        # Verify directory was created
        files = self.fs_1440kb.list_files()
        # Fix: Look for the directory without a leading slash to match what list_files() returns
        test_dir = next((f for f in files if f['is_dir'] and f['name'] == test_dir_name), None)

        self.assertIsNotNone(test_dir, f"Directory '{test_dir_name}' not found after creation")

        # Create a subdirectory
        subdir_name = "SUBDIR"
        self.fs_1440kb.create_directory(f"/{test_dir_name}", subdir_name, datetime.datetime.now())

        # Verify subdirectory was created
        files = self.fs_1440kb.list_files()
        # Fix: Look for the subdirectory without leading slash
        subdir = next((f for f in files if f['is_dir'] and f['name'] == f"{test_dir_name}/{subdir_name}"), None)

        self.assertIsNotNone(subdir, f"Subdirectory '{subdir_name}' not found after creation")

    def test_delete_file_1440kb(self):
        """Test deleting files from 1.44MB floppy"""
        # Find a file to delete
        files = self.fs_1440kb.list_files()
        test_file = next((f for f in files if not f['is_dir']), None)
        self.assertIsNotNone(test_file, "No files found for deletion test")

        # Get the file path
        file_path = test_file['name']

        # Delete the file - add leading slash if needed
        delete_path = f"/{file_path}" if not file_path.startswith('/') else file_path
        self.fs_1440kb.delete_item(delete_path)

        # Verify file was deleted
        files_after = self.fs_1440kb.list_files()
        deleted_file = next((f for f in files_after if f['name'] == file_path), None)

        self.assertIsNone(deleted_file, f"File {file_path} still exists after deletion")

    def test_insert_file_1440kb(self):
        """Test inserting files to 1.44MB floppy"""
        # Create test data
        test_data = b"This is a test file created for FAT12FileSystem testing."

        # Insert the file to the root directory
        file_name = "TESTFILE.TXT"
        file_datetime = datetime.datetime.now()
        self.fs_1440kb.insert_file("/", file_name, test_data, file_datetime)

        # Verify file was inserted
        files = self.fs_1440kb.list_files()
        # Fix: Look for file without leading slash
        test_file = next((f for f in files if f['name'] == file_name), None)

        self.assertIsNotNone(test_file, f"File '{file_name}' not found after insertion")
        self.assertEqual(test_file['size'], len(test_data))

        # Extract the file and verify contents
        file_data = self.fs_1440kb.extract_file(f"/{file_name}")
        self.assertEqual(file_data, test_data)

    def test_file_operations_workflow_1440kb(self):
        """Test a complete workflow of file operations on 1.44MB floppy"""
        # 1. List initial files
        initial_files = self.fs_1440kb.list_files()
        initial_file_count = len(initial_files)

        # 2. Create directories
        test_dir = "TESTDIR"
        sub_dir = "SUBDIR"

        self.fs_1440kb.create_directory("/", test_dir, datetime.datetime.now())
        self.fs_1440kb.create_directory(f"/{test_dir}", sub_dir, datetime.datetime.now())

        # 3. Insert files at different levels
        root_file_data = b"This is a test file in the root directory."
        dir_file_data = b"This is a test file in the test directory."
        subdir_file_data = b"This is a test file in the subdirectory."

        self.fs_1440kb.insert_file("/", "ROOT.TXT", root_file_data, datetime.datetime.now())
        self.fs_1440kb.insert_file(f"/{test_dir}", "DIR.TXT", dir_file_data, datetime.datetime.now())
        self.fs_1440kb.insert_file(f"/{test_dir}/{sub_dir}", "SUB.TXT", subdir_file_data, datetime.datetime.now())

        # 4. List files again to verify additions
        files_after_addition = self.fs_1440kb.list_files()

        # Count new directories and files by looking for the specific added items
        added_items = [
            f for f in files_after_addition
            if f['name'] == test_dir or
            f['name'] == f"{test_dir}/{sub_dir}" or
            f['name'] == "ROOT.TXT" or
            f['name'] == f"{test_dir}/DIR.TXT" or
            f['name'] == f"{test_dir}/{sub_dir}/SUB.TXT"
        ]

        self.assertEqual(len(added_items), 5, "Not all test items were added correctly")

        # 5. Extract and verify file contents
        root_data = self.fs_1440kb.extract_file("/ROOT.TXT")
        dir_data = self.fs_1440kb.extract_file(f"/{test_dir}/DIR.TXT")
        subdir_data = self.fs_1440kb.extract_file(f"/{test_dir}/{sub_dir}/SUB.TXT")

        self.assertEqual(root_data, root_file_data)
        self.assertEqual(dir_data, dir_file_data)
        self.assertEqual(subdir_data, subdir_file_data)

        # 6. Delete files and verify deletion
        self.fs_1440kb.delete_item("/ROOT.TXT")

        files_after_deletion = self.fs_1440kb.list_files()
        deleted_file = next((f for f in files_after_deletion if f['name'] == "ROOT.TXT"), None)
        self.assertIsNone(deleted_file)

        # 7. Delete directory with contents (should fail)
        with self.assertRaises(ValueError):
            self.fs_1440kb.delete_item(f"/{test_dir}")

        # 8. Delete files in directory, then the directory
        self.fs_1440kb.delete_item(f"/{test_dir}/DIR.TXT")
        self.fs_1440kb.delete_item(f"/{test_dir}/{sub_dir}/SUB.TXT")
        self.fs_1440kb.delete_item(f"/{test_dir}/{sub_dir}")
        self.fs_1440kb.delete_item(f"/{test_dir}")

        # 9. Verify all test items are gone
        final_files = self.fs_1440kb.list_files()
        remaining_test_items = [
            f for f in final_files
            if f['name'] == test_dir or
            f['name'] == f"{test_dir}/{sub_dir}" or
            f['name'] == "ROOT.TXT" or
            f['name'] == f"{test_dir}/DIR.TXT" or
            f['name'] == f"{test_dir}/{sub_dir}/SUB.TXT"
        ]

        self.assertEqual(len(remaining_test_items), 0, "Not all test items were cleaned up correctly")


class TestImageFileManager(unittest.TestCase):
    """Tests for the ImageFileManager class"""

    def setUp(self):
        TestUtilities.setup_test_environment()

        # Paths to master images
        self.image_360kb = "test_data/master_360kb/disk_360kb.img"
        self.image_1440kb = "test_data/master_1440kb/disk_1440kb.img"

        # Create temporary image copies for testing
        self.temp_360kb = TestUtilities.create_temp_image_copy(self.image_360kb)

        # Create working directories
        self.working_dir = os.path.join("test_data/temp", "image_manager_test")
        os.makedirs(self.working_dir, exist_ok=True)

        # Reference directories
        self.ref_dir_360kb = "test_data/master_360kb/files"

    def test_image_file_manager_read_write(self):
        """Test basic read and write operations with ImageFileManager"""
        # Initialize manager
        manager = ImageFileManager(self.temp_360kb)

        # Read first sector (boot sector)
        boot_sector = manager.read_bytes(0, 512)
        self.assertEqual(len(boot_sector), 512)

        # Check BPB signature
        signature = int.from_bytes(boot_sector[510:512], byteorder='little')
        self.assertEqual(signature, 0xAA55)

        # Modify a non-critical part of the disk (create a test area)
        # Find a free sector first by checking the FAT
        bpb = FloppyBPB(manager)
        fs = FAT12FileSystem(
            manager.read_bytes,
            manager.write_bytes,
            manager.flush,
            bpb.get_fat12_params()
        )

        # Create a test file to test write operations
        test_data = b"This is test data written by ImageFileManager test"
        fs.insert_file("/", "IMGTEST.TXT", test_data, datetime.datetime.now())

        # Flush changes
        manager.flush()

        # Reopen the image to verify persistence
        manager2 = ImageFileManager(self.temp_360kb)
        bpb2 = FloppyBPB(manager2)
        fs2 = FAT12FileSystem(
            manager2.read_bytes,
            manager2.write_bytes,
            manager2.flush,
            bpb2.get_fat12_params()
        )

        # Extract the test file
        file_data = fs2.extract_file("/IMGTEST.TXT")

        # Verify the data matches
        self.assertEqual(file_data, test_data)

    def test_complete_workflow(self):
        """Test a complete workflow with ImageFileManager"""
        # Initialize manager and filesystem
        manager = ImageFileManager(self.temp_360kb)
        bpb = FloppyBPB(manager)
        fs = FAT12FileSystem(
            manager.read_bytes,
            manager.write_bytes,
            manager.flush,
            bpb.get_fat12_params()
        )

        # 1. Extract initial files to a temporary directory
        initial_extract_dir = os.path.join(self.working_dir, "initial")
        TestUtilities.extract_all_files(fs, initial_extract_dir)

        # 2. Delete a few files
        files = fs.list_files()
        files_to_delete = [f for f in files if not f['is_dir']][:2]  # Delete first two files

        for file_info in files_to_delete:
            fs.delete_item(file_info['name'])

        # 3. Create directories
        test_dir = "TESTDIR"
        test_subdir = "SUBDIR"

        fs.create_directory("/", test_dir, datetime.datetime.now())
        fs.create_directory(f"/{test_dir}", test_subdir, datetime.datetime.now())

        # 4. Copy deleted files back into the new directories
        for i, file_info in enumerate(files_to_delete):
            original_path = os.path.join(initial_extract_dir, file_info['name'].lstrip('/'))
            with open(original_path, 'rb') as f:
                file_data = f.read()

            dest_dir = "/" if i == 0 else f"/{test_dir}"
            fs.insert_file(dest_dir, os.path.basename(file_info['name']), file_data, datetime.datetime.now())

        # 5. Create a new test file in the subdirectory
        test_data = b"This is a new test file created during the ImageFileManager workflow test."
        fs.insert_file(f"/{test_dir}/{test_subdir}", "TEST.TXT", test_data, datetime.datetime.now())

        # 6. Flush changes to disk
        fs.flush_func()

        # 7. Extract all files to a new directory
        modified_extract_dir = os.path.join(self.working_dir, "modified")
        TestUtilities.extract_all_files(fs, modified_extract_dir)

        # 8. Verify created directories exist
        self.assertTrue(os.path.isdir(os.path.join(modified_extract_dir, test_dir)))
        self.assertTrue(os.path.isdir(os.path.join(modified_extract_dir, test_dir, test_subdir)))

        # 9. Verify test file in subdirectory
        test_file_path = os.path.join(modified_extract_dir, test_dir, test_subdir, "TEST.TXT")
        self.assertTrue(os.path.exists(test_file_path))

        with open(test_file_path, 'rb') as f:
            self.assertEqual(f.read(), test_data)


class TestFloppyDiskManager(unittest.TestCase):
    """Tests for the FloppyDiskManager class"""

    def setUp(self):
        TestUtilities.setup_test_environment()

        # Define drive parameters
        self.drive_a = 'A'  # 3.5" drive
        self.drive_b = 'B'  # 5.25" drive

        # Reference directories
        self.ref_dir_360kb = "test_data/master_360kb/files"
        self.ref_dir_1440kb = "test_data/master_1440kb/files"

        # Create working directories
        self.working_dir_a = os.path.join("test_data/temp", "floppy_a")
        self.working_dir_b = os.path.join("test_data/temp", "floppy_b")
        os.makedirs(self.working_dir_a, exist_ok=True)
        os.makedirs(self.working_dir_b, exist_ok=True)

    def test_physical_floppy_operations(self):
        """Test operations on physical floppy disks"""
        # Check if testing on physical disks is enabled
        # This is a safety check to prevent the test from running automatically
        # when no floppy disks are available
        test_physical = os.environ.get('TEST_PHYSICAL_FLOPPY', '0') == '1'

        if not test_physical:
            self.skipTest("Physical floppy testing disabled. Set TEST_PHYSICAL_FLOPPY=1 to enable.")

        try:
            # Initialize FloppyDiskManager for drive A (3.5")
            manager_a = FloppyDiskManager(device_name=None, drive=self.drive_a, format_name='ibm.1440')
            bpb_a = FloppyBPB(manager_a)
            fs_a = FAT12FileSystem(
                manager_a.read_bytes,
                manager_a.write_bytes,
                manager_a.flush,
                bpb_a.get_fat12_params()
            )

            # Test basic operations

            # 1. List files
            files_a = fs_a.list_files()
            print(f"Found {len(files_a)} items on floppy drive A:")
            for f in files_a:
                file_type = "DIR" if f['is_dir'] else "FILE"
                print(f"  {file_type}: {f['name']} - {f['size']} bytes")

            # 2. Extract a few files for verification
            files_to_extract = [f for f in files_a if not f['is_dir']][:3]  # First three files
            for file_info in files_to_extract:
                file_data = fs_a.extract_file(file_info['name'])
                output_path = os.path.join(self.working_dir_a, os.path.basename(file_info['name']))
                with open(output_path, 'wb') as f:
                    f.write(file_data)
                print(f"Extracted {file_info['name']} to {output_path}")

            # 3. Delete a file
            if files_to_extract:
                delete_file = files_to_extract[0]
                fs_a.delete_item(delete_file['name'])
                print(f"Deleted {delete_file['name']}")

            # 4. Create directory and subdirectory
            test_dir = "TESTDIR"
            test_subdir = "SUBDIR"

            fs_a.create_directory("/", test_dir, datetime.datetime.now())
            fs_a.create_directory(f"/{test_dir}", test_subdir, datetime.datetime.now())
            print(f"Created directories /{test_dir} and /{test_dir}/{test_subdir}")

            # 5. Create a test file in the subdirectory
            test_data = b"This is a test file created by the FloppyDiskManager test."
            fs_a.insert_file(f"/{test_dir}/{test_subdir}", "TEST.TXT", test_data, datetime.datetime.now())
            print(f"Created file /{test_dir}/{test_subdir}/TEST.TXT")

            # 6. Flush changes to disk
            fs_a.flush_func()
            print("Flushed changes to disk")

            # 7. Extract the test file and verify
            test_file_data = fs_a.extract_file(f"/{test_dir}/{test_subdir}/TEST.TXT")
            self.assertEqual(test_file_data, test_data)
            print("Verified test file contents")

            # 8. Verify BPB information
            print("BPB Information:")
            print(f"  Bytes per sector: {bpb_a.bytes_per_sector}")
            print(f"  Sectors per cluster: {bpb_a.sectors_per_cluster}")
            print(f"  Reserved sectors: {bpb_a.reserved_sectors}")
            print(f"  Number of FATs: {bpb_a.num_fats}")
            print(f"  Root entries: {bpb_a.root_entries}")
            print(f"  Total sectors: {bpb_a.total_sectors}")
            print(f"  Sectors per FAT: {bpb_a.sectors_per_fat}")
            print(f"  Sectors per track: {bpb_a.sectors_per_track}")
            print(f"  Number of heads: {bpb_a.num_heads}")
            print(f"  Disk type: {bpb_a.get_disk_type()}")

            # If you have a 5.25" drive as B:, you can test it as well
            if os.environ.get('TEST_FLOPPY_B', '0') == '1':
                # Test similar operations on drive B
                manager_b = FloppyDiskManager(device_name=None, drive=self.drive_b, format_name='ibm.360')
                bpb_b = FloppyBPB(manager_b)
                fs_b = FAT12FileSystem(
                    manager_b.read_bytes,
                    manager_b.write_bytes,
                    manager_b.flush,
                    bpb_b.get_fat12_params()
                )

                # Similar tests as for drive A...
                print("\nTesting floppy drive B...")
                files_b = fs_b.list_files()
                print(f"Found {len(files_b)} items on floppy drive B")

        except Exception as e:
            print(f"Error testing physical floppy: {e}")
            import traceback
            traceback.print_exc()
            self.fail(f"Physical floppy test failed: {e}")


class TestSetupTools(unittest.TestCase):
    """Tests for creating and setting up initial test images"""

    def setUp(self):
        TestUtilities.setup_test_environment()

    def test_create_template_images(self):
        """Create template disk images for testing if they don't exist"""
        # Check if master images already exist
        master_360kb = "test_data/master_360kb/disk_360kb.img"
        master_1440kb = "test_data/master_1440kb/disk_1440kb.img"

        # If images already exist, skip creation
        if os.path.exists(master_360kb) and os.path.exists(master_1440kb):
            self.skipTest("Master images already exist.")

        # Create 360KB disk image
        if not os.path.exists(master_360kb):
            print("Creating 360KB master image...")
            self.create_template_disk(master_360kb, "360kb")

        # Create 1.44MB disk image
        if not os.path.exists(master_1440kb):
            print("Creating 1.44MB master image...")
            self.create_template_disk(master_1440kb, "1440kb")

    def create_template_disk(self, image_path, disk_type):
        """Create a template disk image of the specified type"""
        # Create parameters based on disk type
        if disk_type == "360kb":
            params = {
                'sector_size': 512,
                'sectors_per_track': 9,
                'num_tracks': 40,
                'num_heads': 2,
                'num_fats': 2,
                'root_entries': 112,
                'sectors_per_cluster': 2,
                'media_descriptor': 0xFD,
                'reserved_sectors': 1,
                'oem_id': "MSDOS5.0"
            }
        elif disk_type == "1440kb":
            params = {
                'sector_size': 512,
                'sectors_per_track': 18,
                'num_tracks': 80,
                'num_heads': 2,
                'num_fats': 2,
                'root_entries': 224,
                'sectors_per_cluster': 1,
                'media_descriptor': 0xF0,
                'reserved_sectors': 1,
                'oem_id': "MSDOS5.0"
            }
        else:
            raise ValueError(f"Unknown disk type: {disk_type}")

        # Create the disk image
        from cli import create_image_data
        image_data = create_image_data(params)

        # Ensure directory exists
        os.makedirs(os.path.dirname(image_path), exist_ok=True)

        # Write the image
        with open(image_path, 'wb') as f:
            f.write(image_data)

        # Initialize the disk
        manager = ImageFileManager(image_path)
        bpb = FloppyBPB(manager)
        fs = FAT12FileSystem(
            manager.read_bytes,
            manager.write_bytes,
            manager.flush,
            bpb.get_fat12_params()
        )

        # Initialize FATs
        fs.initialize_fats()

        # Create some test content
        fs.create_directory("/", "TEST", datetime.datetime.now())
        fs.create_directory("/TEST", "SUBDIR", datetime.datetime.now())

        # Create test files
        test_files = [
            ("README.TXT", b"This is a test disk image for FatFloppy testing.\r\n"),
            ("TEST/FILE1.TXT", b"Test file 1 content.\r\n"),
            ("TEST/FILE2.TXT", b"Test file 2 content.\r\n"),
            ("TEST/SUBDIR/SUB.TXT", b"Subdirectory test file.\r\n")
        ]

        for file_path, content in test_files:
            parent_path = os.path.dirname("/" + file_path)
            file_name = os.path.basename(file_path)
            fs.insert_file(parent_path, file_name, content, datetime.datetime.now())

        # Flush changes
        fs.flush_func()

        # Save BPB info
        bpb_info_path = os.path.join(os.path.dirname(image_path), "bpb_info.json")
        TestUtilities.save_bpb_info(bpb, bpb_info_path)

        # Extract files to reference directory
        extract_dir = os.path.join(os.path.dirname(image_path), "files")
        TestUtilities.extract_all_files(fs, extract_dir)

        print(f"Created template disk {image_path} with {len(test_files)} test files")
        return image_path


def run_tests():
    """Run all tests"""
    # Create test suite
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()

    # Add test classes
    suite.addTest(loader.loadTestsFromTestCase(TestSetupTools))
    suite.addTest(loader.loadTestsFromTestCase(TestFloppyBPB))
    suite.addTest(loader.loadTestsFromTestCase(TestFAT12FileSystem))
    suite.addTest(loader.loadTestsFromTestCase(TestImageFileManager))

    # Add physical floppy tests only if specifically enabled
    if os.environ.get('TEST_PHYSICAL_FLOPPY', '0') == '1':
        suite.addTest(loader.loadTestsFromTestCase(TestFloppyDiskManager))

    # Run the tests
    runner = unittest.TextTestRunner(verbosity=2)
    return runner.run(suite)


if __name__ == "__main__":
    # Set up environment
    TestUtilities.setup_test_environment()

    # Run tests
    result = run_tests()

    # Exit with appropriate code
    sys.exit(not result.wasSuccessful())
