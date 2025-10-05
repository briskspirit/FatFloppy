"""
Hardware tests for write operations on Drive A using a 1.44M formatted disk.

This test suite verifies the functionality of the DiskController for writing
and deleting files and directories on a physical 3.5" floppy drive.

WARNING: These tests are DESTRUCTIVE and will modify the contents of the
         floppy disk in the target drive.
"""

import os
from collections.abc import Generator

import pytest
from _pytest.config import Config

from fatfloppy.core.controller import DiskController

from .conftest import EMPTY_144M_IMG, GW_FORMAT_144M, _run_gw_write

TARGET_DRIVE: str = "A"
TARGET_FORMAT_KEY: str = "1.44M"
TARGET_DRIVE_SIZE: str = "3.5"
PROFILE_NAME: str = "ibm_3.5_1.44m"

pytestmark = [
    pytest.mark.hardware,
    pytest.mark.skipif(
        os.getenv("TEST_DRIVE_A", "false").lower() != "true",
        reason=(
            "Requires TEST_DRIVE_A='true' environment variable to be set. "
            f"These tests target Drive {TARGET_DRIVE} with a {TARGET_FORMAT_KEY} disk."
        ),
    ),
]


@pytest.fixture(scope="class")
def prepared_controller(
    pytestconfig: Config,
) -> Generator[DiskController, None, None]:
    """
    Class-scoped fixture to prepare a physical floppy disk for write tests.

    This fixture writes a blank, formatted 1.44M disk image to Drive A,
    initializes a DiskController with the correct format profile, and verifies
    that the filesystem is valid and ready for write operations.

    Args:
        pytestconfig: The pytest configuration object.

    Yields:
        An initialized and ready-to-use DiskController instance.
    """
    print(
        f"\n--- Setting up Hardware Test Class for Drive {TARGET_DRIVE} "
        f"({TARGET_FORMAT_KEY} Write) ---"
    )
    print(
        "INFO: Ensure the floppy in Drive A is suitable for writing "
        "(e.g., not write-protected)."
    )
    print("      This disk WILL BE MODIFIED.")
    auto_mode: bool = pytestconfig.getoption("--hw-auto")

    write_success = _run_gw_write(
        TARGET_DRIVE,
        EMPTY_144M_IMG,
        GW_FORMAT_144M,
        f"Empty {TARGET_FORMAT_KEY} for Write Tests",
        auto_mode,
    )
    if not write_success:
        pytest.skip(
            f"Skipping tests for Drive {TARGET_DRIVE} due to write failure.",
            allow_module_level=True,
        )

    print(f"Attempting to open Drive {TARGET_DRIVE}...")
    controller = DiskController()
    gw_device: str | None = os.environ.get("GW_DEVICE", None)
    format_info_to_pass = {"format_name": PROFILE_NAME}

    success = controller.open_disk(
        source=gw_device,
        disk_type="physical",
        drive_letter=TARGET_DRIVE,
        drive_size=TARGET_DRIVE_SIZE,
        format_info=format_info_to_pass,
    )

    if not success:
        controller.close_disk()
        pytest.skip(
            f"Failed to open physical drive {TARGET_DRIVE} after preparation. "
            "Check connection/disk.",
            allow_module_level=True,
        )

    if (
        not controller.filesystem
        or controller.filesystem.get_validity_score()
        < controller.filesystem.validity_threshold
    ):
        controller.close_disk()
        pytest.skip(
            f"Filesystem not valid after opening Drive {TARGET_DRIVE} with "
            f"format {PROFILE_NAME}",
            allow_module_level=True,
        )

    print(f"Drive {TARGET_DRIVE} opened successfully with format '{PROFILE_NAME}'.")
    yield controller
    print(
        f"\n--- Tearing down Hardware Test Class for Drive {TARGET_DRIVE} (Write) ---"
    )
    controller.close_disk()
    print(f"Drive {TARGET_DRIVE} connection closed.")


@pytest.mark.usefixtures("prepared_controller")
class TestHardwareDriveAWrite:
    """
    A collection of write and delete tests for a 1.44M floppy in Drive A.

    These tests assume the drive has been prepared by the `prepared_controller`
    fixture with a clean, formatted filesystem. Each test is responsible for
    creating and cleaning up its own files/directories.
    """

    def _cleanup_item(self, controller: DiskController, path: str) -> None:
        """
        Robustly attempts to delete a file or directory for test cleanup.

        Checks for the existence of the item before attempting deletion to
        avoid unnecessary errors. Logs the outcome of the cleanup attempt.

        Args:
            controller: The active DiskController instance.
            path: The full path of the item to delete (e.g., "/MYFILE.TXT").
        """
        if not controller or not controller.filesystem:
            print(
                f"WARN: Controller or Filesystem not available during cleanup of "
                f"{path}, skipping."
            )
            return

        print(f"Attempting cleanup: delete '{path}'")
        try:
            parent_path, name = controller.filesystem._split_path(path)
            parent_content = controller.list_directory(parent_path)
            if parent_content is None:
                print(
                    f"WARN: Cleanup could not list parent directory '{parent_path}' "
                    f"for '{path}'."
                )
                return

            item_exists = any(
                item["name"].upper() == name.upper() for item in parent_content
            )

            if item_exists:
                deleted = controller.delete_item(path)
                if deleted:
                    print(f"Cleanup: Successfully deleted '{path}'")
                else:
                    print(f"WARN: Cleanup delete command failed for {path}")
            else:
                print(
                    f"Cleanup: Item '{path}' not found in listing, assuming already "
                    "deleted."
                )

        except Exception as e:
            print(f"WARN: Exception during cleanup for {path}: {e}")

    def test_01_hw_a_create_write_read_delete_file(
        self, prepared_controller: DiskController
    ) -> None:
        """Verify the full lifecycle of a file: create, write, read, and delete."""
        print("\nRunning: Create, write, read, delete file on Drive A")
        controller = prepared_controller
        filepath: str = "/WRITE_A.TMP"
        content: bytes = b"Test data written to hardware " * 5

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
            assert read_after_delete is None, (
                f"File '{filepath}' still readable after deletion attempt."
            )
            print(f"Verified deletion of {filepath} (read returned None)")

    def test_02_hw_a_create_delete_dir(
        self, prepared_controller: DiskController
    ) -> None:
        """Verify the lifecycle of a directory: create and delete."""
        print("\nRunning: Create and delete directory on Drive A")
        controller = prepared_controller
        dirpath: str = "/A_DIR"

        try:
            print(f"  Creating {dirpath}...")
            create_ok = controller.create_directory(dirpath)
            assert create_ok, f"create_directory failed for {dirpath}"

            root_list = controller.list_directory("/")
            assert root_list is not None, (
                "Failed to list root directory after creation."
            )
            assert any(e["name"] == "A_DIR" and e["is_dir"] for e in root_list), (
                f"{dirpath} not found after creation"
            )
            print(f"Verified creation of {dirpath}")
        finally:
            self._cleanup_item(controller, dirpath)
            root_list_after = controller.list_directory("/")
            assert root_list_after is not None, (
                "Failed to list root directory after deletion."
            )
            assert not any(e["name"] == "A_DIR" for e in root_list_after), (
                f"{dirpath} still found after deletion attempt"
            )
            print(f"Verified deletion of {dirpath}")

    def test_03_hw_a_write_multicluster_file(
        self, prepared_controller: DiskController
    ) -> None:
        """Verify writing a file large enough to span multiple clusters."""
        print("\nRunning: Create file spanning multiple clusters on Drive A")
        controller = prepared_controller
        filepath: str = "/MULTI_A.BIN"
        content: bytes = b"ClusterData 1.44M " * 100
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

            dir_listing = controller.list_directory("/")
            assert dir_listing is not None, "Failed to list directory after write."
            found_entry = next(
                (
                    item
                    for item in dir_listing
                    if item.get("name", "").upper() == "MULTI_A.BIN"
                ),
                None,
            )

            assert found_entry is not None, (
                f"'{filepath}' not found in listing after write"
            )
            file_size = found_entry.get("size", -1)
            assert file_size == len(content), (
                f"Reported file size ({file_size}) does not match written "
                f"content size ({len(content)})"
            )

            if "starting_cluster" in found_entry:
                start_cluster = found_entry["starting_cluster"]
                if start_cluster == 0:
                    pytest.fail(
                        f"File '{filepath}' has starting_cluster 0 despite writing "
                        f"{len(content)} bytes."
                    )

                assert fs.allocation_unit_size > 0, "Cluster size is zero"
                expected_clusters = (
                    len(content) + fs.allocation_unit_size - 1
                ) // fs.allocation_unit_size

                try:
                    cluster_chain = fs._get_cluster_chain(start_cluster)
                    assert len(cluster_chain) == expected_clusters, (
                        f"Expected {expected_clusters} clusters, "
                        f"found {len(cluster_chain)}"
                    )
                    print(f"Verified file uses {len(cluster_chain)} clusters.")
                except Exception as e:
                    pytest.fail(
                        f"Error getting cluster chain for cluster {start_cluster}: {e}"
                    )
            else:
                pytest.fail(
                    f"File '{filepath}' is missing 'starting_cluster' key despite "
                    f"content length {len(content)}."
                )
        finally:
            self._cleanup_item(controller, filepath)
            read_after_delete = controller.read_file(filepath)
            assert read_after_delete is None, (
                f"File '{filepath}' still readable after deletion attempt."
            )
            print(f"Verified deletion of {filepath} (read returned None)")
