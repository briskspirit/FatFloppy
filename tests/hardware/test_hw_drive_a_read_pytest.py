# tests/hardware/test_hw_drive_a_read_pytest.py
import pytest
import os

# Ensure src is in path if running module directly (pytest handles it better)
# sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src')))

from fatfloppy.core.controller import DiskController
from fatfloppy.core.format_definitions import FLOPPY_FORMATS

# Import helpers/data from conftest
from .conftest import (
    _run_gw_write, POPULATED_144M_IMG, GW_FORMAT_144M,
    expected_file_content # Use the session-scoped fixture
)

# --- Test Configuration ---
TARGET_DRIVE = 'A'
TARGET_FORMAT_KEY = '1.44M'
TARGET_DRIVE_SIZE = "3.5"
# FMT_PROFILE = FLOPPY_FORMATS['ibm_3.5_1.44m'] # Expected format profile


# --- Conditional Skipping ---
# Skips the whole module if TEST_HW is not true (handled by conftest)
# Skips the whole module if env vars don't match target
pytestmark = [
    pytest.mark.hardware, # Mark as hardware test for global skipping
    pytest.mark.skipif(
        not (os.getenv('TEST_DRIVE') == TARGET_DRIVE and os.getenv('TEST_FORMAT') == TARGET_FORMAT_KEY),
        reason=f"Requires TEST_DRIVE={TARGET_DRIVE} and TEST_FORMAT={TARGET_FORMAT_KEY}"
    )
]

# --- Fixture for this specific test class/module ---
@pytest.fixture(scope="class") # Use class scope if tests are in a class
def prepared_controller(pytestconfig, expected_file_content):
    """
    Prepares Drive A with the populated 1.44M image, opens the controller,
    and yields it. Closes controller on teardown.
    Also attaches expected content.
    """
    print(f"\n--- Setting up Hardware Test Class for Drive {TARGET_DRIVE} ({TARGET_FORMAT_KEY} Read) ---")
    auto_mode = pytestconfig.getoption("--hw-auto")

    # Prepare the disk
    prep_success = _run_gw_write(
        TARGET_DRIVE,
        POPULATED_144M_IMG,
        GW_FORMAT_144M,
        f"Populated {TARGET_FORMAT_KEY} for Read Tests",
        auto_mode
    )
    # _run_gw_write uses pytest.skip on failure, so we don't need to check prep_success here

    print(f"Attempting to open Drive {TARGET_DRIVE}...")
    controller = DiskController()
    gw_device = os.environ.get('GW_DEVICE', None)
    success = controller.open_disk(
        source=gw_device,
        disk_type="physical",
        drive_letter=TARGET_DRIVE,
        drive_size=TARGET_DRIVE_SIZE
        # Auto-detect format for read test
    )

    if not success:
        controller.close_disk()
        pytest.skip(f"Failed to open physical drive {TARGET_DRIVE} after preparation. Check connection/disk.", allow_module_level=True)

    if not controller.filesystem or not controller.filesystem.is_valid():
        controller.close_disk()
        pytest.skip(f"No valid filesystem detected on drive {TARGET_DRIVE} after preparation.", allow_module_level=True)

    print(f"Drive {TARGET_DRIVE} opened successfully.")

    # Attach expected content to the controller instance or return a tuple/object
    controller.expected_content = expected_file_content

    yield controller # Provide the controller to the tests

    # --- Teardown ---
    print(f"\n--- Tearing down Hardware Test Class for Drive {TARGET_DRIVE} (Read) ---")
    controller.close_disk()


# --- Test Class ---
@pytest.mark.usefixtures("prepared_controller") # Apply fixture to all methods in the class
class TestHardwareDriveARead:

    # No need for setUpClass or tearDownClass, handled by the fixture

    def test_01_hw_A_list_root(self, prepared_controller):
        """HW Read: List root directory of Drive A"""
        print(f"\nRunning: {self.test_01_hw_A_list_root.__doc__}")
        controller = prepared_controller
        entries = controller.list_directory("/")
        assert len(entries) >= 2, "Root directory has fewer than expected items (TEST.TXT, DIR1)"
        root_names = {e['name'].upper() for e in entries}
        assert 'TEST.TXT' in root_names
        assert 'DIR1' in root_names
        # Check types
        assert not next(e for e in entries if e['name'].upper() == 'TEST.TXT')['is_dir']
        assert next(e for e in entries if e['name'].upper() == 'DIR1')['is_dir']
        print(f"Found {len(entries)} items in root.")

    def test_02_hw_A_list_dir1(self, prepared_controller):
        """HW Read: List /DIR1 directory on Drive A"""
        print(f"\nRunning: {self.test_02_hw_A_list_dir1.__doc__}")
        controller = prepared_controller
        entries = controller.list_directory("/DIR1")
        assert len(entries) >= 3, "/DIR1 has fewer than expected items (PATTERN.BIN, TEST.TXT, SUBDIR)"
        dir1_names = {e['name'].upper() for e in entries}
        assert 'PATTERN.BIN' in dir1_names
        assert 'TEST.TXT' in dir1_names
        assert 'SUBDIR' in dir1_names
        # Check types
        assert not next(e for e in entries if e['name'].upper() == 'PATTERN.BIN')['is_dir']
        assert not next(e for e in entries if e['name'].upper() == 'TEST.TXT')['is_dir']
        assert next(e for e in entries if e['name'].upper() == 'SUBDIR')['is_dir']
        print(f"Found {len(entries)} items in /DIR1.")

    def test_03_hw_A_list_subdir(self, prepared_controller):
        """HW Read: List /DIR1/SUBDIR directory on Drive A"""
        print(f"\nRunning: {self.test_03_hw_A_list_subdir.__doc__}")
        controller = prepared_controller
        entries = controller.list_directory("/DIR1/SUBDIR")
        assert len(entries) >= 1, "/DIR1/SUBDIR has fewer than expected items (TEST.TXT)"
        subdir_names = {e['name'].upper() for e in entries}
        assert 'TEST.TXT' in subdir_names
        # Check types
        assert not next(e for e in entries if e['name'].upper() == 'TEST.TXT')['is_dir']
        print(f"Found {len(entries)} items in /DIR1/SUBDIR.")

    def test_04_hw_A_read_root_file(self, prepared_controller):
        """HW Read: Read /TEST.TXT content on Drive A"""
        print(f"\nRunning: {self.test_04_hw_A_read_root_file.__doc__}")
        controller = prepared_controller
        filepath = "/TEST.TXT"
        content = controller.read_file(filepath)
        assert content is not None, f"Failed to read {filepath}"
        assert len(content) > 0, f"{filepath} is empty"
        # Compare with expected content if loaded
        if "test_txt" in controller.expected_content:
             assert content == controller.expected_content["test_txt"], f"Content mismatch for {filepath}"
        print(f"Read {len(content)} bytes from {filepath}")

    def test_05_hw_A_read_dir1_files(self, prepared_controller):
        """HW Read: Read files in /DIR1 on Drive A"""
        print(f"\nRunning: {self.test_05_hw_A_read_dir1_files.__doc__}")
        controller = prepared_controller
        # Read PATTERN.BIN
        filepath_bin = "/DIR1/PATTERN.BIN"
        content_bin = controller.read_file(filepath_bin)
        assert content_bin is not None, f"Failed to read {filepath_bin}"
        if "pattern_bin" in controller.expected_content:
            assert content_bin == controller.expected_content["pattern_bin"], f"Content mismatch for {filepath_bin}"
        print(f"Read {len(content_bin)} bytes from {filepath_bin}")
        # Read TEST.TXT
        filepath_txt = "/DIR1/TEST.TXT"
        content_txt = controller.read_file(filepath_txt)
        assert content_txt is not None, f"Failed to read {filepath_txt}"
        if "test_txt" in controller.expected_content:
            assert content_txt == controller.expected_content["test_txt"], f"Content mismatch for {filepath_txt}"
        print(f"Read {len(content_txt)} bytes from {filepath_txt}")

    def test_06_hw_A_read_subdir_file(self, prepared_controller):
        """HW Read: Read file in /DIR1/SUBDIR on Drive A"""
        print(f"\nRunning: {self.test_06_hw_A_read_subdir_file.__doc__}")
        controller = prepared_controller
        filepath = "/DIR1/SUBDIR/TEST.TXT"
        content = controller.read_file(filepath)
        assert content is not None, f"Failed to read {filepath}"
        if "test_txt" in controller.expected_content:
            assert content == controller.expected_content["test_txt"], f"Content mismatch for {filepath}"
        print(f"Read {len(content)} bytes from {filepath}")

    def test_07_hw_A_get_disk_info(self, prepared_controller):
        """HW Read: Get free space and allocated clusters on Drive A"""
        print(f"\nRunning: {self.test_07_hw_A_get_disk_info.__doc__}")
        controller = prepared_controller
        free, total = controller.get_free_space()
        clusters = controller.get_allocated_clusters()
        assert free is not None and total is not None and clusters is not None
        assert free < total
        # Check cluster count. Value depends *exactly* on image contents and format.
        # Let's use the value from the original unittest for consistency.
        # Clusters = Root dir clusters + TEST.TXT + DIR1 + /DIR1/PATTERN.BIN + /DIR1/TEST.TXT + /DIR1/SUBDIR + /DIR1/SUBDIR/TEST.TXT
        # 1.44M: FAT sectors=9*2=18, Root=14*32=448 entries = 32 sectors. Data starts after 1+18+32 = 51 sectors.
        # Cluster 2 starts at sector 51. Sector/cluster = 1. Total sectors = 2880.
        # Assuming TEST.TXT < 512b, PATTERN.BIN=1024b, DIR entries take clusters...
        # Root TEST.TXT (1 cl), DIR1 (1 cl), /DIR1/PATTERN.BIN (2 cl), /DIR1/TEST.TXT (1 cl), /DIR1/SUBDIR (1 cl), /DIR1/SUBDIR/TEST.TXT (1 cl) = 7 clusters
        assert len(clusters) == 7, f"Expected 7 allocated clusters for populated 1.44M disk, found {len(clusters)}"
        print(f"Disk Info: Free={free/1024:.1f}KB, Total={total/1024:.1f}KB, Clusters={len(clusters)}")
