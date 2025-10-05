"""
Configuration and fixtures for hardware-level floppy drive tests.

This module sets up the pytest environment for running tests that interact
directly with floppy drive hardware via the Greaseweazle tool. It includes
command-line options, markers, prerequisite checks, and fixtures for drive
preparation.
"""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from _pytest.config import Config
from _pytest.config.argparsing import Parser
from _pytest.nodes import Item

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

RESOURCE_DIR: Path = Path(__file__).parent.parent / "resources"
GW_EXECUTABLE: str = os.environ.get("GW_PATH", "gw")

EMPTY_144M_IMG: str = str(RESOURCE_DIR / "empty_formatted_144m.img")
POPULATED_144M_IMG: str = str(RESOURCE_DIR / "populated_read_test_144m.img")
EMPTY_360K_IMG: str = str(RESOURCE_DIR / "empty_formatted_360k.img")
POPULATED_360K_IMG: str = str(RESOURCE_DIR / "populated_read_test_360k.img")

GW_FORMAT_144M: str = "ibm.1440"
GW_FORMAT_360K: str = "ibm.360"

TEST_FILE_TXT_PATH: str = str(RESOURCE_DIR / "TEST.TXT")
PATTERN_FILE_BIN_PATH: str = str(RESOURCE_DIR / "PATTERN.BIN")


def _check_prerequisites() -> None:
    """
    Verifies that required host tools and test files are present.

    Exits pytest with an error message if the Greaseweazle executable or
    essential disk images are not found. Warns if supplemental resource
    files are missing.
    """
    if not shutil.which(GW_EXECUTABLE):
        pytest.exit(
            f"ERROR: '{GW_EXECUTABLE}' command not found. Install Greaseweazle "
            "host tools and ensure it's in PATH or set GW_PATH env var.",
            returncode=1,
        )

    missing_files: list[str] = []
    image_paths: list[str] = [
        EMPTY_144M_IMG,
        POPULATED_144M_IMG,
        EMPTY_360K_IMG,
        POPULATED_360K_IMG,
    ]
    for img_path in image_paths:
        if not os.path.isfile(img_path):
            missing_files.append(img_path)

    if missing_files:
        pytest.exit(
            f"ERROR: Master image files not found: {', '.join(missing_files)}",
            returncode=1,
        )

    missing_resources: list[str] = []
    resource_paths: list[str] = [TEST_FILE_TXT_PATH, PATTERN_FILE_BIN_PATH]
    for res_path in resource_paths:
        if not os.path.isfile(res_path):
            missing_resources.append(res_path)

    if missing_resources:
        print(
            "\nWARN: Resource files for content verification not found: "
            f"{', '.join(missing_resources)}"
        )


def _run_gw_write(
    drive: str, image_path: str, gw_format: str, description: str, auto_mode: bool
) -> bool:
    """
    Writes a disk image to a specified floppy drive using Greaseweazle.

    This function prompts the user to insert a floppy (unless in auto_mode)
    and executes the 'gw write' command with retry logic. If writing fails
    twice or times out, it skips the tests for that drive.

    Args:
        drive: The drive identifier to use (e.g., 'A').
        image_path: The local path to the disk image file to write.
        gw_format: The format string for Greaseweazle (e.g., 'ibm.1440').
        description: A human-readable description of the write operation.
        auto_mode: If True, skips the interactive prompt to insert a disk.

    Returns:
        True if the write operation was successful, False otherwise.
    """
    print("\n" + "-" * 60)
    print(f" Preparing Drive {drive} ({description})")
    print("-" * 60)
    print(f" >>> Will write image: {image_path}")
    print(f" >>> Using GW format: {gw_format}")

    if not auto_mode:
        input(
            f" >>> Please insert the correct floppy into Drive {drive} and press "
            "Enter..."
        )

    cmd: list[str] = [
        GW_EXECUTABLE,
        "write",
        f"--drive={drive}",
        f"--format={gw_format}",
        image_path,
        "--retries=10",
    ]
    print(f"Running command: {' '.join(cmd)}")

    for attempt in range(1, 3):
        try:
            result = subprocess.run(
                cmd, check=True, capture_output=True, text=True, timeout=180
            )
            print("STDOUT:\n" + result.stdout)
            print("STDERR:\n" + result.stderr)
            print(f"Drive {drive} prepared successfully.")
            time.sleep(2)
            return True
        except FileNotFoundError:
            pytest.fail(
                f"ERROR: Failed to execute '{GW_EXECUTABLE}'. Is it in PATH or "
                "is GW_PATH set?",
                pytrace=False,
            )
            return False
        except subprocess.CalledProcessError as e:
            print(f"ERROR: 'gw write' failed for Drive {drive} (Attempt {attempt}).")
            print("STDOUT:\n" + e.stdout)
            print("STDERR:\n" + e.stderr)
            if attempt == 2:
                pytest.skip(
                    f"Failed to prepare Drive {drive} using 'gw write' on second "
                    "attempt. Aborting tests for this drive.",
                    allow_module_level=True,
                )
                return False
            print("Retrying after 2 seconds...")
            time.sleep(2)
        except subprocess.TimeoutExpired:
            pytest.skip(
                f"'gw write' timed out for Drive {drive}. Check drive/connection.",
                allow_module_level=True,
            )
            return False

    return False


def pytest_addoption(parser: Parser) -> None:
    """
    Adds a custom command-line option to pytest.

    Args:
        parser: The pytest argument parser.
    """
    parser.addoption(
        "--hw-auto",
        action="store_true",
        default=False,
        help="Run hardware tests without interactive prompts.",
    )


def pytest_configure(config: Config) -> None:
    """
    Adds a custom marker for hardware tests.

    Args:
        config: The pytest configuration object.
    """
    config.addinivalue_line("markers", "hardware: mark test as requiring hardware")


def pytest_collection_modifyitems(config: Config, items: list[Item]) -> None:
    """
    Skips tests marked with 'hardware' if TEST_HW is not set to 'true'.

    Args:
        config: The pytest configuration object.
        items: List of collected test items.
    """
    if os.getenv("TEST_HW", "false").lower() != "true":
        skip_hw = pytest.mark.skip(
            reason="Hardware tests skipped (TEST_HW not 'true')"
        )
        for item in items:
            if "hardware" in item.keywords:
                item.add_marker(skip_hw)


@pytest.fixture(scope="session")
def expected_file_content() -> dict[str, bytes]:
    """
    A session-scoped fixture that loads the expected content of test files.

    This fixture reads 'TEST.TXT' and 'PATTERN.BIN' from the resources
    directory into memory for verification in tests.

    Returns:
        A dictionary where keys are file identifiers ('test_txt', 'pattern_bin')
        and values are the file content as bytes. Returns an empty dictionary
        if files are not found.
    """
    content: dict[str, bytes] = {}
    try:
        with open(TEST_FILE_TXT_PATH, "rb") as f:
            content["test_txt"] = f.read()
        with open(PATTERN_FILE_BIN_PATH, "rb") as f:
            content["pattern_bin"] = f.read()
    except FileNotFoundError as e:
        print(f"\nWARN: Could not load resource file for verification: {e}")
    return content


_check_prerequisites()
