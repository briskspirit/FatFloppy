# tests/hardware/test_hw_drive_b_write_pytest.py
import pytest
import os

# Ensure src is in path if running module directly
# sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src')))

from fatfloppy.core.controller import DiskController
from fatfloppy.core.format_definitions import FLOPPY_FORMATS

# Import helpers/data from conftest
from .conftest import (
    _run_gw_write, EMPTY_360K_IMG, GW_FORMAT_360K
)

# --- Test Configuration ---
TARGET_DRIVE = 'B'
TARGET_FORMAT_KEY = '360K'
TARGET_DRIVE_SIZE = "5.25"
FMT_PROFILE = FLOPPY_FORMATS['ibm_5.25_360k']


# --- Conditional Skipping ---
pytestmark = [
    pytest.mark.hardware,
    pytest.mark.skipif(
        not (os.getenv('TEST_DRIVE') == TARGET_DRIVE and os.getenv('TEST_FORMAT') == TARGET_FORMAT_KEY),
        reason=f"Requires TEST_DRIVE={TARGET_DRIVE} and TEST_FORMAT={TARGET_FORMAT_KEY}"
    )
]

# --- Fixture for this specific test class/module ---
@pytest.fixture(scope="class")
def prepared_controller(pytestconfig):
    """
    Prepares Drive B with the empty 360K image, opens the controller with
    explicit format info, and yields it. Closes controller on teardown.
    """
    print(f"\n--- Setting up Hardware Test Class for Drive {TARGET_DRIVE} ({TARGET_FORMAT_KEY} Write) ---")
    print("INFO: Ensure the floppy in Drive B is suitable for writing.")
    print("      This disk WILL BE MODIFIED.")
    auto_mode = pytestconfig.getoption("--hw-auto")

    # Prepare the disk
    prep_success = _run_gw_write(
        TARGET_DRIVE,
        EMPTY_360K_IMG,
        GW_FORMAT_360K,
        f"Empty {TARGET_FORMAT_KEY} for Write Tests",
        auto_mode
    )
    # _run_gw_write uses pytest.skip on failure

    print(f"Attempting to open Drive {TARGET_DRIVE}...")
    controller = DiskController()
    gw_device = os.environ.get('GW_DEVICE', None)

    # Extract format info dictionary
    format_info_dict = {
        **{k: v for k, v in FMT_PROFILE.geometry.__dict__.items() if not k.startswith('_')},
        **{k: v for k, v in FMT_PROFILE.physical_format.__dict__.items() if not k.startswith('_')}
    }

    success = controller.open_disk(
        source=gw_device,
        disk_type="physical",
        drive_letter=TARGET_DRIVE,
        drive_size=TARGET_DRIVE_SIZE,
        format_info=format_info_dict # Use explicit format
    )

    if not success:
        controller.close_disk()
        pytest.skip(f"Failed to open physical drive {TARGET_DRIVE} after preparation.", allow_module_level=True)

    # if not controller.filesystem or not controller.filesystem.is_valid():
    #     controller.close_disk()
    #     pytest.skip(f"No valid filesystem detected on drive {TARGET_DRIVE} after preparation.", allow_module_level=True)

    print(f"Drive {TARGET_DRIVE} opened successfully.")

    yield controller # Provide the controller to the tests

    print(f"\n--- Tearing down Hardware Test Class for Drive {TARGET_DRIVE} (Write) ---")
    controller.close_disk()


# --- Test Class ---
@pytest.mark.usefixtures("prepared_controller")
class TestHardwareDriveBWrite:

    # Re-use cleanup helper logic (could be moved to conftest if truly generic)
    def _cleanup_item(self, controller, path):
        print(f"Attempting cleanup: delete '{path}'")
        try:
            parent_path, name = controller.filesystem._split_path(path)
            parent_cluster = controller.filesystem._get_directory_cluster(parent_path)
            try:
                 entry, _ = controller.filesystem._find_entry_in_directory(parent_cluster, name)
                 deleted = controller.delete_item(path)
                 if deleted: print(f"Cleanup: Successfully deleted '{path}'")
                 else: print(f"WARN: Cleanup delete command failed for {path}")
            except FileNotFoundError:
                 print(f"Cleanup: Item '{path}' not found.")
            except Exception as e:
                 print(f"WARN: Exception during _find_entry_in_directory for cleanup of {path}: {e}")
        except AttributeError:
             print(f"WARN: Filesystem object not available during cleanup of {path}, skipping.")
        except Exception as e:
            print(f"WARN: Exception during cleanup path processing for {path}: {e}")


    def test_01_hw_B_create_write_read_delete_file(self, prepared_controller):
        """HW Write: Create, write, read, delete file on Drive B"""
        print(f"\nRunning: {self.test_01_hw_B_create_write_read_delete_file.__doc__}")
        controller = prepared_controller
        filepath = "/WRITE_B.TMP"
        content = b"360KB hardware test " * 10 # Approx 200 bytes
        try:
            print(f"  Writing {filepath}...")
            write_ok = controller.write_file(filepath, content)
            assert write_ok, f"write_file failed for {filepath}"

            print(f"  Reading back {filepath}...")
            read_content = controller.read_file(filepath)
            assert read_content == content, f"Read content mismatch for {filepath}"
        finally:
            # Delete (using helper within finally)
            self._cleanup_item(controller, filepath)
            # Verify deletion by checking if read_file now returns None
            read_after_delete = controller.read_file(filepath)
            assert read_after_delete is None, f"File '{filepath}' still readable after deletion attempt."
            print(f"Verified deletion of {filepath} (read returned None)")

    def test_02_hw_B_create_delete_dir(self, prepared_controller):
        """HW Write: Create and delete directory on Drive B"""
        print(f"\nRunning: {self.test_02_hw_B_create_delete_dir.__doc__}")
        controller = prepared_controller
        dirpath = "/B_DIR"
        try:
            print(f"  Creating {dirpath}...")
            create_ok = controller.create_directory(dirpath)
            assert create_ok, f"create_directory failed for {dirpath}"

            root_list = controller.list_directory("/")
            assert any(e['name'] == 'B_DIR' and e['is_dir'] for e in root_list), f"{dirpath} not found after creation"
            print(f"Verified creation of {dirpath}")
        finally:
            # Delete
            self._cleanup_item(controller, dirpath)
             # Verify deletion by listing
            root_list_after = controller.list_directory("/")
            assert not any(e['name'] == 'B_DIR' for e in root_list_after), f"{dirpath} still found after deletion attempt"
            print(f"Verified deletion of {dirpath}")

    def test_03_hw_B_write_multicluster_file(self, prepared_controller):
        """HW Write: Create file spanning multiple clusters on Drive B"""
        print(f"\nRunning: {self.test_03_hw_B_write_multicluster_file.__doc__}")
        controller = prepared_controller
        filepath = "/MULTI_B.BIN"
        content = b"360K Cluster " * 160 # Approx 2080 bytes -> 3 clusters
        assert len(content) > 1024 * 2, "Content length should be > 2 clusters"
        try:
            # Write
            print(f"  Writing {filepath} ({len(content)} bytes)...")
            write_ok = controller.write_file(filepath, content)
            assert write_ok, f"write_file failed for {filepath}"

            # Read back
            print(f"  Reading back {filepath}...")
            read_content = controller.read_file(filepath)
            assert read_content == content, f"Read content mismatch for {filepath}"

            # --- Revised Allocation Check ---
            fs = controller.filesystem
            assert fs is not None, "Filesystem object is missing"
            found_entry = None
            list_dir_result = controller.list_directory("/")
            print(f"DEBUG: list_directory('/') returned: {list_dir_result}") # Add debug print
            for item in list_dir_result:
                 # Use .get() for safer access and handle potential case issues
                 if item.get('name', '').upper() == 'MULTI_B.BIN':
                     found_entry = item
                     break
            assert found_entry is not None, f"'{filepath}' not found in listing after write"
            print(f"DEBUG: Found entry for '{filepath}': {found_entry}") # Add debug print

            file_size = found_entry.get('size', -1) # Get size safely
            assert file_size == len(content), \
                f"Reported file size ({file_size}) does not match written content size ({len(content)})"

            # Check if starting_cluster key exists and handle based on size
            if 'starting_cluster' in found_entry:
                start_cluster = found_entry['starting_cluster']

                if start_cluster == 0:
                    # This means a zero-byte file according to FAT rules
                    assert file_size == 0, \
                        f"File '{filepath}' has starting_cluster 0 but reported size is {file_size} (expected 0)"
                    # Since we wrote content, this is an error condition for this test
                    pytest.fail(f"File '{filepath}' unexpectedly has starting_cluster 0 despite writing {len(content)} bytes.")
                else:
                    # Non-zero starting cluster: Proceed with chain check
                    print(f"DEBUG: Found starting cluster {start_cluster} for '{filepath}'")
                    assert fs.cluster_size > 0, "Cluster size is zero"
                    expected_clusters = (len(content) + fs.cluster_size - 1) // fs.cluster_size
                    try:
                        cluster_chain = fs._get_cluster_chain(start_cluster)
                        assert len(cluster_chain) == expected_clusters, \
                            f"Expected {expected_clusters} clusters (size={len(content)}), found chain length {len(cluster_chain)} starting at {start_cluster}"
                        print(f"Verified file uses {len(cluster_chain)} clusters.")
                    except Exception as e:
                        pytest.fail(f"Error getting cluster chain for cluster {start_cluster}: {e}")

            else:
                # 'starting_cluster' key is MISSING
                # This should only happen for zero-byte files
                assert file_size == 0, \
                    f"File '{filepath}' is missing 'starting_cluster' key, but reported size is {file_size} (expected 0 if key is missing)"
                # Since we wrote content, this is an error condition for this test
                pytest.fail(f"File '{filepath}' is missing 'starting_cluster' key. Content length was {len(content)}, expected clusters.")
            # --- End Revised Allocation Check ---

        finally:
            # Delete
            self._cleanup_item(controller, filepath)
            # Verify deletion
            read_after_delete = controller.read_file(filepath)
            assert read_after_delete is None, f"File '{filepath}' still readable after deletion attempt."
            print(f"Verified deletion of {filepath} (read returned None)")
