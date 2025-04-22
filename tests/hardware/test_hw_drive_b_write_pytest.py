# tests/hardware/test_hw_drive_b_write_pytest.py
import pytest
import os

from fatfloppy.core.controller import DiskController
from fatfloppy.core.format_definitions import FLOPPY_FORMATS
from .conftest import _run_gw_write, EMPTY_360K_IMG, GW_FORMAT_360K

TARGET_DRIVE = 'B'
TARGET_FORMAT_KEY = '360K'
TARGET_DRIVE_SIZE = "5.25"
FMT_PROFILE = FLOPPY_FORMATS['ibm_5.25_360k']

pytestmark = [
    pytest.mark.hardware,
    pytest.mark.hardware,
    pytest.mark.skipif(not os.getenv('TEST_DRIVE_B', 'false').lower() == 'true',
        reason=f"Requires TEST_DRIVE={TARGET_DRIVE} and TEST_FORMAT={TARGET_FORMAT_KEY}"
    )
]

@pytest.fixture(scope="class")
def prepared_controller(pytestconfig):
    print(f"\n--- Setting up Hardware Test Class for Drive {TARGET_DRIVE} ({TARGET_FORMAT_KEY} Write) ---")
    print("INFO: Ensure the floppy in Drive B is suitable for writing.")
    print("      This disk WILL BE MODIFIED.")
    auto_mode = pytestconfig.getoption("--hw-auto")
    _run_gw_write(TARGET_DRIVE, EMPTY_360K_IMG, GW_FORMAT_360K, f"Empty {TARGET_FORMAT_KEY} for Write Tests", auto_mode)
    print(f"Attempting to open Drive {TARGET_DRIVE}...")
    controller = DiskController()
    gw_device = os.environ.get('GW_DEVICE', None)
    format_info_dict = {
        **{k: v for k, v in FMT_PROFILE.geometry.__dict__.items() if not k.startswith('_')},
        **{k: v for k, v in FMT_PROFILE.physical_format.__dict__.items() if not k.startswith('_')}
    }
    success = controller.open_disk(
        source=gw_device,
        disk_type="physical",
        drive_letter=TARGET_DRIVE,
        drive_size=TARGET_DRIVE_SIZE,
        format_info=format_info_dict
    )
    if not success:
        controller.close_disk()
        pytest.skip(f"Failed to open physical drive {TARGET_DRIVE} after preparation.", allow_module_level=True)
    print(f"Drive {TARGET_DRIVE} opened successfully.")
    yield controller
    print(f"\n--- Tearing down Hardware Test Class for Drive {TARGET_DRIVE} (Write) ---")
    controller.close_disk()

@pytest.mark.usefixtures("prepared_controller")
class TestHardwareDriveBWrite:
    def _cleanup_item(self, controller, path):
        print(f"Attempting cleanup: delete '{path}'")
        try:
            parent_path, name = controller.filesystem._split_path(path)
            parent_cluster = controller.filesystem._get_directory_cluster(parent_path)
            try:
                entry, _ = controller.filesystem._find_entry_in_directory(parent_cluster, name)
                deleted = controller.delete_item(path)
                if deleted:
                    print(f"Cleanup: Successfully deleted '{path}'")
                else:
                    print(f"WARN: Cleanup delete command failed for {path}")
            except FileNotFoundError:
                print(f"Cleanup: Item '{path}' not found.")
            except Exception as e:
                print(f"WARN: Exception during _find_entry_in_directory for cleanup of {path}: {e}")
        except AttributeError:
            print(f"WARN: Filesystem object not available during cleanup of {path}, skipping.")
        except Exception as e:
            print(f"WARN: Exception during cleanup path processing for {path}: {e}")

    def test_01_hw_B_create_write_read_delete_file(self, prepared_controller):
        print(f"\nRunning: HW Write: Create, write, read, delete file on Drive B")
        controller = prepared_controller
        filepath = "/WRITE_B.TMP"
        content = b"360KB hardware test " * 10
        try:
            print(f"  Writing {filepath}...")
            write_ok = controller.write_file(filepath, content)
            assert write_ok, f"write_file failed for {filepath}"
            print(f"  Reading back {filepath}...")
            read_content = controller.read_file(filepath)
            assert read_content == content, f"Read content mismatch for {filepath}"
        finally:
            self._cleanup_item(controller, filepath)
            read_after_delete = controller.read_file(filepath)
            assert read_after_delete is None, f"File '{filepath}' still readable after deletion attempt."
            print(f"Verified deletion of {filepath} (read returned None)")

    def test_02_hw_B_create_delete_dir(self, prepared_controller):
        print(f"\nRunning: HW Write: Create and delete directory on Drive B")
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
            self._cleanup_item(controller, dirpath)
            root_list_after = controller.list_directory("/")
            assert not any(e['name'] == 'B_DIR' for e in root_list_after), f"{dirpath} still found after deletion attempt"
            print(f"Verified deletion of {dirpath}")

    def test_03_hw_B_write_multicluster_file(self, prepared_controller):
        print(f"\nRunning: HW Write: Create file spanning multiple clusters on Drive B")
        controller = prepared_controller
        filepath = "/MULTI_B.BIN"
        content = b"360K Cluster " * 160
        assert len(content) > 1024 * 2, "Content length should be > 2 clusters"
        try:
            print(f"  Writing {filepath} ({len(content)} bytes)...")
            write_ok = controller.write_file(filepath, content)
            assert write_ok, f"write_file failed for {filepath}"
            print(f"  Reading back {filepath}...")
            read_content = controller.read_file(filepath)
            assert read_content == content, f"Read content mismatch for {filepath}"
            fs = controller.filesystem
            assert fs is not None, "Filesystem object is missing"
            list_dir_result = controller.list_directory("/")
            print(f"DEBUG: list_directory('/') returned: {list_dir_result}")
            found_entry = next((item for item in list_dir_result if item.get('name', '').upper() == 'MULTI_B.BIN'), None)
            assert found_entry is not None, f"'{filepath}' not found in listing after write"
            print(f"DEBUG: Found entry for '{filepath}': {found_entry}")
            file_size = found_entry.get('size', -1)
            assert file_size == len(content), f"Reported file size ({file_size}) does not match written content size ({len(content)})"
            if 'starting_cluster' in found_entry:
                start_cluster = found_entry['starting_cluster']
                if start_cluster == 0:
                    assert file_size == 0, f"File '{filepath}' has starting_cluster 0 but reported size is {file_size} (expected 0)"
                    pytest.fail(f"File '{filepath}' unexpectedly has starting_cluster 0 despite writing {len(content)} bytes.")
                else:
                    print(f"DEBUG: Found starting cluster {start_cluster} for '{filepath}'")
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
