# tests/hardware/conftest.py
import pytest
import os
import subprocess
import shutil
import sys
import time

from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / 'src'))

RESOURCE_DIR = Path(__file__).parent.parent / 'resources'
GW_EXECUTABLE = os.environ.get("GW_PATH", "gw")

EMPTY_144M_IMG = os.path.join(RESOURCE_DIR, "empty_formatted_144m.img")
POPULATED_144M_IMG = os.path.join(RESOURCE_DIR, "populated_read_test_144m.img")
EMPTY_360K_IMG = os.path.join(RESOURCE_DIR, "empty_formatted_360k.img")
POPULATED_360K_IMG = os.path.join(RESOURCE_DIR, "populated_read_test_360k.img")

GW_FORMAT_144M = "ibm.1440"
GW_FORMAT_360K = "ibm.360"

TEST_FILE_TXT_PATH = os.path.join(RESOURCE_DIR, 'TEST.TXT')
PATTERN_FILE_BIN_PATH = os.path.join(RESOURCE_DIR, 'PATTERN.BIN')

def pytest_addoption(parser):
    parser.addoption(
        "--hw-auto", action="store_true", default=False, help="Run hardware tests without interactive prompts."
    )

def pytest_configure(config):
    config.addinivalue_line("markers", "hardware: mark test as requiring hardware")

def pytest_collection_modifyitems(config, items):
    if not os.getenv('TEST_HW', 'false').lower() == 'true':
        skip_hw = pytest.mark.skip(reason="Hardware tests skipped (TEST_HW not 'true')")
        for item in items:
            if "hardware" in item.keywords:
                item.add_marker(skip_hw)

def _check_prerequisites():
    if not shutil.which(GW_EXECUTABLE):
        pytest.exit(f"ERROR: '{GW_EXECUTABLE}' command not found. Install Greaseweazle host tools and ensure it's in PATH or set GW_PATH env var.", returncode=1)
    missing_files = []
    for img_path in [EMPTY_144M_IMG, POPULATED_144M_IMG, EMPTY_360K_IMG, POPULATED_360K_IMG]:
        if not os.path.isfile(img_path):
            missing_files.append(img_path)
    if missing_files:
        pytest.exit(f"ERROR: Master image files not found: {', '.join(missing_files)}", returncode=1)
    missing_resources = []
    for res_path in [TEST_FILE_TXT_PATH, PATTERN_FILE_BIN_PATH]:
        if not os.path.isfile(res_path):
            missing_resources.append(res_path)
    if missing_resources:
        print(f"\nWARN: Resource files for content verification not found: {', '.join(missing_resources)}")

_check_prerequisites()

def _run_gw_write(drive, image_path, gw_format, description, auto_mode):
    print("\n" + "-" * 60)
    print(f" Preparing Drive {drive} ({description})")
    print("-" * 60)
    print(f" >>> Will write image: {image_path}")
    print(f" >>> Using GW format: {gw_format}")
    if not auto_mode:
        input(f" >>> Please insert the correct floppy into Drive {drive} and press Enter...")
    cmd = [GW_EXECUTABLE, "write", f"--drive={drive}", f"--format={gw_format}", image_path, "--retries=10"]
    print(f"Running command: {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=180)
        print("STDOUT:\n" + result.stdout)
        print("STDERR:\n" + result.stderr)
        print(f"Drive {drive} prepared successfully.")
        time.sleep(2)
        return True
    except FileNotFoundError:
        pytest.fail(f"ERROR: Failed to execute '{GW_EXECUTABLE}'. Is it in PATH or GW_PATH set?", pytrace=False)
        return False
    except subprocess.CalledProcessError as e:
        print(f"ERROR: 'gw write' failed for Drive {drive} (Attempt 1).")
        print("STDOUT:\n" + e.stdout)
        print("STDERR:\n" + e.stderr)
        print("Retrying after 2 seconds...")
        time.sleep(2)
        try:
            result = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=180)
            print("STDOUT:\n" + result.stdout)
            print("STDERR:\n" + result.stderr)
            print(f"Drive {drive} prepared successfully on retry.")
            time.sleep(2)
            return True
        except subprocess.CalledProcessError as e2:
            print(f"ERROR: 'gw write' failed for Drive {drive} on second attempt.")
            print("STDOUT:\n" + e2.stdout)
            print("STDERR:\n" + e2.stderr)
            pytest.skip(f"Failed to prepare Drive {drive} using 'gw write'. Aborting tests for this drive.", allow_module_level=True)
            return False
    except subprocess.TimeoutExpired:
        pytest.skip(f"'gw write' timed out for Drive {drive}. Check drive/connection.", allow_module_level=True)
        return False

@pytest.fixture(scope="session")
def expected_file_content():
    content = {}
    try:
        with open(TEST_FILE_TXT_PATH, "rb") as f:
            content["test_txt"] = f.read()
        with open(PATTERN_FILE_BIN_PATH, "rb") as f:
            content["pattern_bin"] = f.read()
    except FileNotFoundError as e:
        print(f"\nWARN: Could not load resource file for verification: {e}")
    return content
