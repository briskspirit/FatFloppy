# tests/hardware/test_hw_drive_a_write_pytest.py
import pytest
import os

from fatfloppy.core.controller import DiskController
from fatfloppy.core.format_definitions import FLOPPY_FORMATS
from .conftest import _run_gw_write, EMPTY_144M_IMG, GW_FORMAT_144M, TEST_FILE_TXT_PATH # Added TEST_FILE_TXT_PATH

TARGET_DRIVE = 'A'
TARGET_FORMAT_KEY = '1.44M'
TARGET_DRIVE_SIZE = "3.5"
PROFILE_NAME = 'ibm_3.5_1.44m' # Define profile name

pytestmark = [
    pytest.mark.hardware,
    pytest.mark.skipif(not os.getenv('TEST_DRIVE_A', 'false').lower() == 'true',
        reason=f"Requires TEST_DRIVE={TARGET_DRIVE} and TEST_FORMAT={TARGET_FORMAT_KEY}"
    )
]

@pytest.fixture(scope="class")
def prepared_controller(pytestconfig):
    print(f"\n--- Setting up Hardware Test Class for Drive {TARGET_DRIVE} ({TARGET_FORMAT_KEY} Write) ---")
    print("INFO: Ensure the floppy in Drive A is suitable for writing (e.g., not write-protected).")
    print("      This disk WILL BE MODIFIED.")
    auto_mode = pytestconfig.getoption("--hw-auto")
    _run_gw_write(TARGET_DRIVE, EMPTY_144M_IMG, GW_FORMAT_144M, f"Empty {TARGET_FORMAT_KEY} for Write Tests", auto_mode)
    print(f"Attempting to open Drive {TARGET_DRIVE}...")
    controller = DiskController()
    gw_device = os.environ.get('GW_DEVICE', None)

    # --- FIX: Pass profile name instead of manual dict ---
    format_info_to_pass = {"format_name": PROFILE_NAME}
    # --- End Fix ---

    success = controller.open_disk(
        source=gw_device,
        disk_type="physical",
        drive_letter=TARGET_DRIVE,
        drive_size=TARGET_DRIVE_SIZE,
        format_info=format_info_to_pass # Pass the corrected info
    )
    if not success:
        controller.close_disk()
        pytest.skip(f"Failed to open physical drive {TARGET_DRIVE} after preparation. Check connection/disk.", allow_module_level=True)
    if not controller.filesystem or not controller.filesystem.is_valid():
         controller.close_disk()
         pytest.skip(f"Filesystem not valid after opening Drive {TARGET_DRIVE} with format {PROFILE_NAME}", allow_module_level=True)

    print(f"Drive {TARGET_DRIVE} opened successfully with format '{PROFILE_NAME}'.")
    yield controller
    print(f"\n--- Tearing down Hardware Test Class for Drive {TARGET_DRIVE} (Write) ---")
    controller.close_disk()

@pytest.mark.usefixtures("prepared_controller")
class TestHardwareDriveAWrite:
    def _cleanup_item(self, controller, path):
        # Check filesystem before accessing methods
        if not controller or not controller.filesystem:
             print(f"WARN: Controller or Filesystem not available during cleanup of {path}, skipping.")
             return

        print(f"Attempting cleanup: delete '{path}'")
        try:
            # Use controller's list method to check existence first if possible
            parent_path, name = controller.filesystem._split_path(path) # Assuming filesystem is valid now
            parent_content = controller.list_directory(parent_path)
            item_exists = any(item['name'].upper() == name.upper() for item in parent_content)

            if item_exists:
                deleted = controller.delete_item(path)
                if deleted:
                    print(f"Cleanup: Successfully deleted '{path}'")
                else:
                    print(f"WARN: Cleanup delete command failed for {path}")
            else:
                 print(f"Cleanup: Item '{path}' not found in listing, assuming already deleted.")

        except Exception as e:
            print(f"WARN: Exception during cleanup for {path}: {e}")

    def test_01_hw_A_create_write_read_delete_file(self, prepared_controller):
        print(f"\nRunning: Create, write, read, delete file on Drive A")
        controller = prepared_controller
        filepath = "/WRITE_A.TMP"
        content = b"Test data written to hardware " * 5
        try:
            print(f"  Writing {filepath}...")
            write_ok = controller.write_file(filepath, content)
            assert write_ok, f"write_file failed for {filepath}"
            print(f"  Reading back {filepath}...")
            read_content = controller.read_file(filepath)
            assert read_content is not None, f"read_file returned None for {filepath}"
            assert read_content == content, f"Read content mismatch for {filepath}"
        finally:
            self._cleanup_item(controller, filepath)
            read_after_delete = controller.read_file(filepath)
            assert read_after_delete is None, f"File '{filepath}' still readable after deletion attempt."
            print(f"Verified deletion of {filepath} (read returned None)")

    def test_02_hw_A_create_delete_dir(self, prepared_controller):
        print(f"\nRunning: Create and delete directory on Drive A")
        controller = prepared_controller
        dirpath = "/A_DIR"
        try:
            print(f"  Creating {dirpath}...")
            create_ok = controller.create_directory(dirpath)
            assert create_ok, f"create_directory failed for {dirpath}"
            root_list = controller.list_directory("/")
            assert any(e['name'] == 'A_DIR' and e['is_dir'] for e in root_list), f"{dirpath} not found after creation"
            print(f"Verified creation of {dirpath}")
        finally:
            self._cleanup_item(controller, dirpath)
            root_list_after = controller.list_directory("/")
            assert not any(e['name'] == 'A_DIR' for e in root_list_after), f"{dirpath} still found after deletion attempt"
            print(f"Verified deletion of {dirpath}")

    def test_03_hw_B_write_multicluster_file(self, prepared_controller):
        # Renaming test as it applies to Drive A now
        print(f"\nRunning: Create file spanning multiple clusters on Drive A")
        controller = prepared_controller
        filepath = "/MULTI_A.BIN"
        # Adjust content size if needed for 1.44M cluster size (512 bytes)
        # Need more than 3 clusters -> more than 1536 bytes
        content = b"ClusterData 1.44M " * 100 # Approx 1700 bytes
        assert len(content) > 512 * 3, "Content length check failed (1.44M)"
        try:
            print(f"  Writing {filepath} ({len(content)} bytes)...")
            write_ok = controller.write_file(filepath, content)
            assert write_ok, f"write_file failed for {filepath}"
            print(f"  Reading back {filepath}...")
            read_content = controller.read_file(filepath)
            assert read_content == content, f"Read content mismatch for {filepath}"
            fs = controller.filesystem
            assert fs is not None, "Filesystem object is missing"
            found_entry = next((item for item in controller.list_directory("/") if item.get('name', '').upper() == 'MULTI_A.BIN'), None)
            assert found_entry is not None, f"'{filepath}' not found in listing after write"
            file_size = found_entry.get('size', -1)
            assert file_size == len(content), f"Reported file size ({file_size}) does not match written content size ({len(content)})"
            if 'starting_cluster' in found_entry:
                start_cluster = found_entry['starting_cluster']
                if start_cluster == 0:
                    assert file_size == 0, f"File '{filepath}' has starting_cluster 0 but reported size is {file_size} (expected 0)"
                    pytest.fail(f"File '{filepath}' unexpectedly has starting_cluster 0 despite writing {len(content)} bytes.")
                else:
                    assert fs.cluster_size > 0, "Cluster size is zero"
                    expected_clusters = (len(content) + fs.cluster_size - 1) // fs.cluster_size
                    try:
                        cluster_chain = fs._get_cluster_chain(start_cluster)
                        assert len(cluster_chain) == expected_clusters, f"Expected {expected_clusters} clusters, found {len(cluster_chain)}"
                        print(f"Verified file uses {len(cluster_chain)} clusters.")
                    except Exception as e:
                        pytest.fail(f"Error getting cluster chain for cluster {start_cluster}: {e}")
            else:
                assert file_size == 0, f"File '{filepath}' is missing 'starting_cluster' key, but reported size is {file_size}"
                pytest.fail(f"File '{filepath}' is missing 'starting_cluster' key despite content length {len(content)}.")
        finally:
            self._cleanup_item(controller, filepath)
            read_after_delete = controller.read_file(filepath)
            assert read_after_delete is None, f"File '{filepath}' still readable after deletion attempt."
            print(f"Verified deletion of {filepath} (read returned None)")
