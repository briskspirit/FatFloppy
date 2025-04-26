# tests/test_09_driver_imd_pytest.py
import pytest
import sys
import shutil
import struct
from pathlib import Path
from unittest.mock import patch, MagicMock # Import patch for logger test

sys.path.insert(0, str(Path(__file__).parent.parent.parent / 'src'))

from fatfloppy.core.drivers import IMDImageDriver
from fatfloppy.core.physical_format import PhysicalFormat, TrackFormat # Added imports
from fatfloppy.core.format_definitions import FLOPPY_FORMATS

# --- Constants ---
RESOURCE_DIR = Path(__file__).parent / 'resources'
# Make sure this path is correct relative to this test file
TEST_IMD_SRC = RESOURCE_DIR / 'imd_720k.imd'
if not TEST_IMD_SRC.exists():
    # Try parent directory if not found in current test dir's resources
    RESOURCE_DIR = Path(__file__).parent.parent / 'resources'
    TEST_IMD_SRC = RESOURCE_DIR / 'imd_720k.imd'

FMT_720 = FLOPPY_FORMATS['ibm_3.5_720k']
FMT_144 = FLOPPY_FORMATS['ibm_3.5_1.44m']
SIMPLE_BPS = 128

# --- Helper function (build_test_imd_data) remains the same ---
# ... (Keep the existing helper function) ...
def build_test_imd_data(
    comment="Simple Test IMD", mode=5, cylinders=2, heads=1, sectors_per_track=3,
    sector_size_code=0, compressed_fill=0xE5
    ) -> bytearray:
    """Creates a simple IMD bytearray for basic testing."""
    data = bytearray()
    header = f"IMD Test: 01/01/2024 00:00:00 : {comment}"
    data.extend(header.encode('ascii'))
    data.append(0x1A)
    sector_size = 128 << sector_size_code
    for c in range(cylinders):
        for h in range(heads):
            data.extend(struct.pack("<BBBBB", mode, c, h, sectors_per_track, sector_size_code))
            data.extend(struct.pack(f"<{sectors_per_track}B", *list(range(1, sectors_per_track + 1))))
            for s in range(1, sectors_per_track + 1):
                if c == 0 and h == 0 and s == 1:
                    data.append(1)
                    data.extend(bytes([s] * sector_size))
                else:
                    data.append(2)
                    data.append(compressed_fill)
    return data


# --- Fixtures (formatted_imd_driver corrected) ---
@pytest.fixture(scope="function")
def real_imd_driver(tmp_path):
    """Loads the real imd_720k.imd file into a temporary location."""
    if not TEST_IMD_SRC or not TEST_IMD_SRC.exists():
        pytest.skip(f"Required resource not found: {TEST_IMD_SRC}")
    test_imd_path = tmp_path / TEST_IMD_SRC.name
    shutil.copy(TEST_IMD_SRC, test_imd_path)
    print(f"\n[Fixture Setup] Copied real IMD image to {test_imd_path}")
    try:
        driver = IMDImageDriver(str(test_imd_path))
        assert driver.physical_format is not None, "Failed to derive physical format from real IMD"
        yield driver, test_imd_path, FMT_720
    except Exception as e:
        pytest.fail(f"Failed to initialize IMDImageDriver with {test_imd_path}: {e}")
    print(f"[Fixture Teardown] Real IMD test image {test_imd_path} cleanup.")

@pytest.fixture(scope="function")
def simple_imd_driver(tmp_path):
    """Creates a driver instance by parsing manually constructed IMD data."""
    test_imd_path = tmp_path / "simple_test.imd"
    imd_data = build_test_imd_data()
    test_imd_path.write_bytes(imd_data)
    try:
        driver = IMDImageDriver(str(test_imd_path))
        assert driver.physical_format is not None, "Failed to derive format in simple_imd_driver"
        yield driver, test_imd_path
    except Exception as e:
         pytest.fail(f"Failed to initialize simple_imd_driver: {e}")

@pytest.fixture(scope="function")
def formatted_imd_driver(tmp_path):
    """Creates a driver by formatting in memory and flushing to a file."""
    test_imd_path = tmp_path / "formatted_test.imd"
    test_format = FMT_144

    try:
        driver = IMDImageDriver(str(test_imd_path))
        driver.format_imd(test_format, fill_byte=0xF6)
        driver.flush()
    except Exception as e:
        pytest.fail(f"format_imd or flush failed in fixture setup: {e}")

    assert test_imd_path.exists(), "Flush did not create the IMD file in the fixture"
    file_size = test_imd_path.stat().st_size
    assert file_size > 0, "Flushed IMD file is empty in fixture"
    print(f"\n[Fixture Debug] Flushed IMD file '{test_imd_path.name}' size: {file_size} bytes.")
    try:
        file_head = test_imd_path.read_bytes()[:100]
        print(f"[Fixture Debug] Flushed IMD head: {file_head!r}")
    except Exception as read_err:
        print(f"[Fixture Debug] Error reading head of flushed file: {read_err}")

    try:
        reloaded_driver = IMDImageDriver(str(test_imd_path))
        assert reloaded_driver.file_loaded, "Failed to mark reloaded driver as loaded"
        # --- Check physical format derivation after reload ---
        assert reloaded_driver.physical_format is not None, \
            f"Reloaded driver failed to derive format (check parsing of file created by flush - size {file_size})"
        # --- Optionally add more specific format checks ---
        pf = reloaded_driver.physical_format
        assert pf.cylinders == test_format.physical_format.cylinders
        assert pf.heads == test_format.physical_format.heads
        assert pf.bytes_per_sector == test_format.physical_format.bytes_per_sector

        yield reloaded_driver, test_imd_path, test_format
    except Exception as e:
         pytest.fail(f"Failed to reload driver from flushed file in fixture: {e}")


# --- Tests ---

# Corrected test_01
def test_01_init_parse_simple(simple_imd_driver):
    driver, path = simple_imd_driver
    assert path.exists()
    assert driver.file_loaded
    # Comment parsing should now correctly exclude the timestamp
    assert driver.comment == "Simple Test IMD", f"Parsed comment mismatch. Got: '{driver.comment}'"
    assert driver.imd_version == "IMD Test", f"Parsed version mismatch. Got: '{driver.imd_version}'"
    # ... (rest of the assertions remain the same) ...
    assert len(driver.tracks) == 2 * 1 # Cyls * Heads from build_test_imd_data
    assert (0, 0) in driver.tracks
    assert (1, 0) in driver.tracks
    pf = driver.physical_format
    assert pf is not None
    assert pf.cylinders == 2
    # ... (rest of assertions)


# Corrected test_02
def test_02_init_file_not_found(tmp_path): # Use tmp_path for clean test
    non_existent_path = tmp_path / "non_existent_file.imd"
    # __init__ should now succeed even if file doesn't exist
    try:
        driver = IMDImageDriver(str(non_existent_path))
        assert not driver.file_loaded, "Driver should indicate file not loaded"
        assert driver.physical_format is None, "Physical format should be None for non-existent file"
        assert driver.dirty is False, "Driver should not be dirty initially"
    except Exception as e:
        pytest.fail(f"IMDImageDriver init raised unexpected exception for non-existent file: {e}")

def test_03_init_corrupt_header_no_eof():
    bad_data = b"IMD Corrupt Header String Without EOF Mark"
    with pytest.raises(ValueError, match="IMD header terminator .* not found"):
        # Need to write to file first for current __init__
        dummy_path = Path("./dummy_corrupt.imd")
        try:
            dummy_path.write_bytes(bad_data)
            IMDImageDriver(str(dummy_path))
        finally:
            if dummy_path.exists(): dummy_path.unlink()

def test_04_init_corrupt_track_header_incomplete(tmp_path):
    test_imd_path = tmp_path / "corrupt_track.imd"
    # Build valid header + start of track header
    imd_data = build_test_imd_data(cylinders=1, heads=1, sectors_per_track=1)
    # Truncate after Mode byte of first track header
    valid_header_len = imd_data.index(0x1A) + 1
    corrupt_data = imd_data[:valid_header_len + 1]
    test_imd_path.write_bytes(corrupt_data)

    with pytest.raises(ValueError, match="Incomplete track header"):
        IMDImageDriver(str(test_imd_path))

def test_05_read_sector_normal(simple_imd_driver):
    driver, _ = simple_imd_driver
    # Sector (0,0,1) was created as Normal with value 1
    data = driver.read_sector(0, 0, 1)
    assert len(data) == SIMPLE_BPS
    assert data == bytes([1] * SIMPLE_BPS)

def test_06_read_sector_compressed(simple_imd_driver):
    driver, _ = simple_imd_driver
    # Sector (0,0,2) was created as Compressed with value 0xE5
    data = driver.read_sector(0, 0, 2)
    assert len(data) == SIMPLE_BPS
    assert data == bytes([0xE5] * SIMPLE_BPS)

# Skipping unavailable/error types for now, requires more complex setup

def test_07_read_sector_out_of_bounds(simple_imd_driver):
    driver, _ = simple_imd_driver
    # Read non-existent sector
    data = driver.read_sector(0, 0, 99)
    assert data == bytes(SIMPLE_BPS), "Reading non-existent sector should return zeroed data"
    # Read non-existent track
    data = driver.read_sector(99, 0, 1)
    assert data == bytes(SIMPLE_BPS), "Reading non-existent track should return zeroed data"

# Corrected test_08
def test_08_set_physical_format_warning(simple_imd_driver):
    driver, _ = simple_imd_driver
    # --- Patch the logger used by the driver ---
    logger_name = driver.logger.name # Get the specific logger name
    with patch(f'fatfloppy.core.drivers.imd.logger.warning') as mock_log_warning:
        driver.set_physical_format(FMT_720.physical_format) # Use 720k for test
    # --- End Patch ---
    # Verify the logger's warning method was called
    mock_log_warning.assert_called_once()
    # Check the content of the warning message if needed
    args, _ = mock_log_warning.call_args
    assert "External physical format set, may conflict with IMD data" in args[0]
    # Verify the format was actually set
    assert driver.physical_format is not None
    assert driver.physical_format.cylinders == FMT_720.physical_format.cylinders


# Corrected test_09
def test_09_format_imd(tmp_path):
    test_imd_path = tmp_path / "format_imd_test.imd"
    try:
        driver = IMDImageDriver(str(test_imd_path))
    except Exception as e:
         pytest.fail(f"IMDImageDriver init failed even for non-existent file: {e}")

    test_format = FMT_144

    try:
        driver.format_imd(test_format, fill_byte=0xAA)
    except Exception as e:
        pytest.fail(f"format_imd failed: {e}")

    # --- Assertions BEFORE flush ---
    assert driver.dirty is True, "Driver should be dirty after format_imd"
    assert len(driver.image_data) > 0, "image_data should be populated after format_imd"
    assert driver.imd_version == "IMD 1.18", "IMD version mismatch after format_imd"
    assert "FatFloppy" in driver.comment, "Comment mismatch after format_imd"
    assert driver.physical_format == test_format.physical_format, "Physical format mismatch after format_imd"

    try:
        header_end_idx = driver.image_data.index(0x1A)
        assert header_end_idx > 0, "Header terminator 0x1A not found"
    except ValueError:
        pytest.fail("Header terminator 0x1A not found in image_data after format_imd")

    first_track_header_offset = header_end_idx + 1
    expected_track_header = bytes([
        3, # Mode=3 (500k MFM)
        0, # Cyl=0
        0, # Head=0 (Flags=0)
        18,# SPT=18
        2  # SizeCode=2 (512)
    ])
    # --- Check bounds before slicing ---
    if first_track_header_offset + 5 > len(driver.image_data):
        pytest.fail(f"image_data too short ({len(driver.image_data)}) to check first track header at offset {first_track_header_offset}")
    # --- End check ---
    assert driver.image_data[first_track_header_offset : first_track_header_offset+5] == expected_track_header, "First track header mismatch after format_imd"

    # Check first data record type and fill byte
    first_map_len = 18 # SectorNumMap
    first_data_record_offset = first_track_header_offset + 5 + first_map_len # Header + SectorNumMap
    # --- Check bounds before slicing ---
    if first_data_record_offset + 2 > len(driver.image_data):
         pytest.fail(f"image_data too short ({len(driver.image_data)}) to check first sector data at offset {first_data_record_offset}")
    # --- End check ---
    assert driver.image_data[first_data_record_offset] == 2, "First sector type should be Compressed (2)"
    assert driver.image_data[first_data_record_offset + 1] == 0xAA, "First sector fill byte mismatch"
    print("test_09_format_imd: PASSED (pre-flush checks)")
    # --- End Assertions ---

def test_10_write_normal_flush_rebuild(formatted_imd_driver):
    driver, path, fmt = formatted_imd_driver
    bps = fmt.physical_format.bytes_per_sector
    test_data = bytes([i % 256 for i in range(bps)]) # Normal, non-compressible
    cyl, head, sect = 0, 0, 1

    # Initial read (should be compressed 0xF6 as used in fixture)
    initial_read = driver.read_sector(cyl, head, sect)
    # --- FIX: Expect 0xF6 ---
    assert initial_read == bytes([0xF6] * bps), "Initial read mismatch - expected formatted fill byte 0xF6"
    # --- End Fix ---

    driver.write_sector(cyl, head, sect, test_data)
    assert driver.dirty is True
    assert (cyl, head, sect) in driver.modified_sector_data
    read_before_flush = driver.read_sector(cyl, head, sect)
    assert read_before_flush == test_data

    driver.flush()
    assert driver.dirty is False
    assert not driver.modified_sector_data

    driver2 = IMDImageDriver(str(path))
    read_after_flush = driver2.read_sector(cyl, head, sect)
    assert read_after_flush == test_data
    ti = driver2.tracks[(cyl, head)]
    _, type_after, size_after = ti.sector_data_info[sect]
    assert type_after == 1 # Should now be Normal
    assert size_after == bps

# Corrected test_11
def test_11_write_compressed_flush_rebuild(formatted_imd_driver):
    driver, path, fmt = formatted_imd_driver
    bps = fmt.physical_format.bytes_per_sector
    test_data = bytes([0xBB] * bps) # Compressible
    cyl, head, sect = 0, 1, 5

    initial_read = driver.read_sector(cyl, head, sect)
    # --- FIX: Expect 0xF6 ---
    assert initial_read == bytes([0xF6] * bps), "Initial read mismatch - expected formatted fill byte 0xF6"
    # --- End Fix ---

    driver.write_sector(cyl, head, sect, test_data)
    assert driver.dirty is True
    assert (cyl, head, sect) in driver.modified_sector_data
    read_before_flush = driver.read_sector(cyl, head, sect)
    assert read_before_flush == test_data

    driver.flush()
    assert driver.dirty is False
    assert not driver.modified_sector_data

    driver2 = IMDImageDriver(str(path))
    read_after_flush = driver2.read_sector(cyl, head, sect)
    assert read_after_flush == test_data
    ti = driver2.tracks[(cyl, head)]
    _, type_after, size_after = ti.sector_data_info[sect]
    assert type_after == 2 # Should be Compressed
    assert size_after == 1 # Size of compressed data record

def test_12_write_changes_type_flush_rebuild(formatted_imd_driver):
    driver, path, fmt = formatted_imd_driver
    bps = fmt.physical_format.bytes_per_sector
    # Write normal data first to make sector 1 Normal
    normal_data = bytes([i % 256 for i in range(bps)])
    cyl, head, sect = 1, 0, 2
    driver.write_sector(cyl, head, sect, normal_data)
    driver.flush()

    # Reload to confirm Normal state
    driver_reloaded1 = IMDImageDriver(str(path))
    ti1 = driver_reloaded1.tracks[(cyl, head)]
    _, type1, size1 = ti1.sector_data_info[sect]
    assert type1 == 1 and size1 == bps, "Sector should be Normal after first write/flush"
    assert driver_reloaded1.read_sector(cyl, head, sect) == normal_data

    # Now write compressible data to the same sector
    compressed_data = bytes([0x77] * bps)
    driver_reloaded1.write_sector(cyl, head, sect, compressed_data)
    assert driver_reloaded1.dirty is True
    assert driver_reloaded1.read_sector(cyl, head, sect) == compressed_data # Read from cache

    # Flush again (triggers rebuild because type/size changed)
    driver_reloaded1.flush()
    assert driver_reloaded1.dirty is False

    # Reload again and verify Compressed state
    driver_reloaded2 = IMDImageDriver(str(path))
    ti2 = driver_reloaded2.tracks[(cyl, head)]
    _, type2, size2 = ti2.sector_data_info[sect]
    assert type2 == 2, f"Sector should be Compressed after second write/flush, but type is {type2}"
    assert size2 == 1, f"Compressed sector data size should be 1, but is {size2}"
    read_data_final = driver_reloaded2.read_sector(cyl, head, sect)
    assert read_data_final == compressed_data

def test_13_flush_no_changes(formatted_imd_driver):
    driver, path, _ = formatted_imd_driver
    initial_mtime = path.stat().st_mtime
    assert driver.dirty is False
    driver.flush() # Should do nothing
    assert driver.dirty is False
    assert path.stat().st_mtime == initial_mtime # File timestamp shouldn't change

def test_14_real_imd_parse_basic(real_imd_driver):
    driver, path, fmt_720 = real_imd_driver
    assert driver.file_loaded
    # Add checks for comment/version if known, otherwise check they are strings
    assert isinstance(driver.comment, str)
    assert isinstance(driver.imd_version, str)
    assert len(driver.tracks) == fmt_720.physical_format.cylinders * fmt_720.physical_format.heads

    # Check derived physical format against the expected 720k format
    pf = driver.physical_format
    assert pf is not None
    assert pf.cylinders == fmt_720.physical_format.cylinders
    assert pf.heads == fmt_720.physical_format.heads
    assert pf.bytes_per_sector == fmt_720.physical_format.bytes_per_sector
    assert len(pf.track_formats) == 1 # Assuming uniform format in the IMD
    tf = pf.track_formats[0]
    tf_expected = fmt_720.physical_format.track_formats[0]
    assert tf.sectors_per_track == tf_expected.sectors_per_track
    assert tf.encoding == tf_expected.encoding
    assert tf.rate == tf_expected.rate
    print("test_14_real_imd_parse_basic: PASSED")

def test_15_real_imd_read_boot_sector(real_imd_driver):
    driver, path, fmt_720 = real_imd_driver
    bps = fmt_720.physical_format.bytes_per_sector
    # Try reading the boot sector (C=0, H=0, S=1)
    boot_sector_data = driver.read_sector(0, 0, 1)
    assert len(boot_sector_data) == bps
    # Add specific checks if you know the content, e.g., boot signature
    assert boot_sector_data.endswith(b'\x55\xAA'), "Boot sector signature missing"
    # Check OEM ID or other fields if known
    # Example: oem_id = boot_sector_data[3:11].strip()
    # assert oem_id == b'MSDOS5.0'
    print("test_15_real_imd_read_boot_sector: PASSED")

def test_16_real_imd_read_known_sector(real_imd_driver):
    driver, path, fmt_720 = real_imd_driver
    # Choose a sector you know the state of (e.g., part of FAT or root dir)
    # Let's assume sector 2 on track 0, head 0 (often part of FAT)
    c, h, s = 0, 0, 2
    try:
        sector_data = driver.read_sector(c, h, s)
        assert len(sector_data) == fmt_720.physical_format.bytes_per_sector
        # Add more specific content checks if possible
        print(f"test_16_real_imd_read_known_sector: Read C={c}, H={h}, S={s} - OK")
    except ValueError as e:
         pytest.fail(f"Failed to read known sector C={c}, H={h}, S={s}: {e}")

def test_17_real_imd_read_last_sector(real_imd_driver):
    driver, path, fmt_720 = real_imd_driver
    pf = fmt_720.physical_format
    last_c = pf.cylinders - 1
    last_h = pf.heads - 1
    last_s = pf.track_formats[0].sectors_per_track # Assuming uniform

    try:
        sector_data = driver.read_sector(last_c, last_h, last_s)
        assert len(sector_data) == pf.bytes_per_sector
        print(f"test_17_real_imd_read_last_sector: Read C={last_c}, H={last_h}, S={last_s} - OK")
    except ValueError as e:
         pytest.fail(f"Failed to read last sector C={last_c}, H={last_h}, S={last_s}: {e}")
