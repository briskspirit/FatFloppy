# tests/test_09_driver_imd_pytest.py
import pytest
import sys
import shutil
import struct
from pathlib import Path
from unittest.mock import patch, MagicMock # Import patch for logger test

sys.path.insert(0, str(Path(__file__).parent.parent.parent / 'src'))

from fatfloppy.core.drivers import IMDImageDriver
from fatfloppy.core.physical_format import PhysicalFormat, TrackFormat
from fatfloppy.core.format_definitions import FLOPPY_FORMATS
from fatfloppy.core.format_profile import FormatProfile # Import FormatProfile

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
SIMPLE_BPS = 128 # For build_test_imd_data default

# --- FIX: Define IMD constants at module level for helper ---
IMD_SECTOR_SIZE_MAP = {
    0: 128, 1: 256, 2: 512, 3: 1024, 4: 2048, 5: 4096, 6: 8192,
}
# --- End Fix ---


# --- Helper function ---
def build_test_imd_data(
    comment="Simple Test IMD", mode=5, cylinders=2, heads=1, sectors_per_track=3,
    sector_size_code=0, compressed_fill=0xE5, sector_map=None,
    has_cyl_map=False, has_head_map=False, has_size_map=False, size_map_list=None
    ) -> bytearray:
    """Creates a simple IMD bytearray for basic testing."""
    data = bytearray()
    header = f"IMD Test: 01/01/2024 00:00:00 : {comment}"
    data.extend(header.encode('ascii'))
    data.append(0x1A) # EOF Mark

    if sector_map is None:
        sector_map = list(range(1, sectors_per_track + 1))
    elif len(sector_map) != sectors_per_track:
         raise ValueError("Length of sector_map must equal sectors_per_track")

    sector_size = IMD_SECTOR_SIZE_MAP.get(sector_size_code, -1) if not has_size_map else -1
    if sector_size == -1 and not has_size_map:
         raise ValueError("Sector size code invalid or size map required but not specified")

    for c in range(cylinders):
        for h in range(heads):
            head_flags = h & 1
            if has_cyl_map: head_flags |= 0x80
            if has_head_map: head_flags |= 0x40
            current_size_code = 0xFF if has_size_map else sector_size_code

            data.extend(struct.pack("<BBBBB", mode, c, head_flags, sectors_per_track, current_size_code))
            data.extend(struct.pack(f"<{sectors_per_track}B", *sector_map))

            if has_cyl_map:
                data.extend(struct.pack(f"<{sectors_per_track}B", *([c] * sectors_per_track)))
            if has_head_map:
                data.extend(struct.pack(f"<{sectors_per_track}B", *([h] * sectors_per_track)))
            if has_size_map:
                 if size_map_list is None or len(size_map_list) != sectors_per_track:
                     raise ValueError("size_map_list required and must match sectors_per_track")
                 data.extend(struct.pack(f"<{sectors_per_track}H", *size_map_list))

            for idx, s_num in enumerate(sector_map):
                 current_sector_size = size_map_list[idx] if has_size_map else sector_size
                 if current_sector_size <= 0:
                     raise ValueError(f"Invalid sector size {current_sector_size} derived for C={c}, H={h}, S={s_num}")

                 if c == 0 and h == 0 and s_num == sector_map[0]:
                    data_type = 1 # Normal
                    sector_content = bytes([s_num] * current_sector_size)
                    data.append(data_type)
                    data.extend(sector_content)
                 else:
                    data_type = 2 # Compressed
                    data.append(data_type)
                    data.append(compressed_fill)
    return data


# --- Fixtures ---
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
        reloaded_driver = IMDImageDriver(str(test_imd_path))
        assert reloaded_driver.file_loaded, "Failed to mark reloaded driver as loaded"
        assert reloaded_driver.physical_format is not None, \
            f"Reloaded driver failed to derive format (check parsing of file created by flush - size {file_size})"
        pf = reloaded_driver.physical_format
        assert pf.cylinders == test_format.physical_format.cylinders
        assert pf.heads == test_format.physical_format.heads
        assert pf.bytes_per_sector == test_format.physical_format.bytes_per_sector

        yield reloaded_driver, test_imd_path, test_format
    except Exception as e:
         pytest.fail(f"Failed to reload driver from flushed file in fixture: {e}")


# --- Tests ---
# ... (keep existing tests 01-17) ...
def test_01_init_parse_simple(simple_imd_driver):
    driver, path = simple_imd_driver
    assert path.exists()
    assert driver.file_loaded
    assert driver.comment == "Simple Test IMD", f"Parsed comment mismatch. Got: '{driver.comment}'"
    assert driver.imd_version == "IMD Test", f"Parsed version mismatch. Got: '{driver.imd_version}'"
    assert len(driver.tracks) == 2 # Cyls * Heads from build_test_imd_data (2*1)
    assert (0, 0) in driver.tracks
    assert (1, 0) in driver.tracks
    pf = driver.physical_format
    assert pf is not None
    assert pf.cylinders == 2
    assert pf.heads == 1
    assert pf.bytes_per_sector == 128
    assert pf.track_formats[0].sectors_per_track == 3

def test_02_init_file_not_found(tmp_path):
    non_existent_path = tmp_path / "non_existent_file.imd"
    try:
        driver = IMDImageDriver(str(non_existent_path))
        assert not driver.file_loaded
        assert driver.physical_format is None
        assert not driver.dirty
    except Exception as e:
        pytest.fail(f"IMDImageDriver init raised unexpected exception for non-existent file: {e}")

def test_03_init_corrupt_header_no_eof(tmp_path):
    bad_data = b"IMD Corrupt Header String Without EOF Mark"
    test_path = tmp_path / "corrupt_header.imd"
    test_path.write_bytes(bad_data)
    with pytest.raises(ValueError, match="IMD header terminator .* not found"):
        IMDImageDriver(str(test_path))

def test_04_init_corrupt_track_header_incomplete(tmp_path):
    test_imd_path = tmp_path / "corrupt_track.imd"
    imd_data = build_test_imd_data(cylinders=1, heads=1, sectors_per_track=1)
    valid_header_len = imd_data.index(0x1A) + 1
    corrupt_data = imd_data[:valid_header_len + 3] # Too short for full 5-byte header
    test_imd_path.write_bytes(corrupt_data)
    with pytest.raises(ValueError, match="Incomplete track header"):
        IMDImageDriver(str(test_imd_path))

def test_05_read_sector_normal(simple_imd_driver):
    driver, _ = simple_imd_driver
    data = driver.read_sector(0, 0, 1)
    assert len(data) == SIMPLE_BPS
    assert data == bytes([1] * SIMPLE_BPS)

def test_06_read_sector_compressed(simple_imd_driver):
    driver, _ = simple_imd_driver
    data = driver.read_sector(0, 0, 2)
    assert len(data) == SIMPLE_BPS
    assert data == bytes([0xE5] * SIMPLE_BPS)

def test_07_read_sector_out_of_bounds(simple_imd_driver):
    driver, _ = simple_imd_driver
    expected_size = driver.physical_format.bytes_per_sector
    data = driver.read_sector(0, 0, 99)
    assert data == bytes(expected_size)
    data = driver.read_sector(99, 0, 1)
    assert data == bytes(expected_size)

def test_08_set_physical_format_warning(simple_imd_driver):
    driver, _ = simple_imd_driver
    logger_name = driver.logger.name
    with patch(f'fatfloppy.core.drivers.imd.logger.warning') as mock_log_warning:
        driver.set_physical_format(FMT_720.physical_format)
    mock_log_warning.assert_called_once()
    args, _ = mock_log_warning.call_args
    assert "External physical format set, may conflict with IMD data" in args[0]
    assert driver.physical_format is not None
    assert driver.physical_format.cylinders == FMT_720.physical_format.cylinders

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

    assert driver.dirty is True
    assert len(driver.image_data) > 0
    assert driver.imd_version == "IMD 1.18"
    assert "FatFloppy" in driver.comment
    assert driver.physical_format == test_format.physical_format

    try:
        header_end_idx = driver.image_data.index(0x1A)
        assert header_end_idx > 0
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
    if first_track_header_offset + 5 > len(driver.image_data):
        pytest.fail(f"image_data too short ({len(driver.image_data)})")
    assert driver.image_data[first_track_header_offset : first_track_header_offset+5] == expected_track_header

    first_map_len = 18
    first_data_record_offset = first_track_header_offset + 5 + first_map_len
    if first_data_record_offset + 2 > len(driver.image_data):
         pytest.fail(f"image_data too short ({len(driver.image_data)})")
    assert driver.image_data[first_data_record_offset] == 2 # Compressed
    assert driver.image_data[first_data_record_offset + 1] == 0xAA # Fill byte

def test_10_write_normal_flush_rebuild(formatted_imd_driver):
    driver, path, fmt = formatted_imd_driver
    bps = fmt.physical_format.bytes_per_sector
    test_data = bytes([i % 256 for i in range(bps)])
    cyl, head, sect = 0, 0, 1

    initial_read = driver.read_sector(cyl, head, sect)
    assert initial_read == bytes([0xF6] * bps)

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
    assert type_after == 1 # Normal
    assert size_after == bps

def test_11_write_compressed_flush_rebuild(formatted_imd_driver):
    driver, path, fmt = formatted_imd_driver
    bps = fmt.physical_format.bytes_per_sector
    test_data = bytes([0xBB] * bps)
    cyl, head, sect = 0, 1, 5

    initial_read = driver.read_sector(cyl, head, sect)
    assert initial_read == bytes([0xF6] * bps)

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
    assert type_after == 2 # Compressed
    assert size_after == 1

def test_12_write_changes_type_flush_rebuild(formatted_imd_driver):
    driver, path, fmt = formatted_imd_driver
    bps = fmt.physical_format.bytes_per_sector
    normal_data = bytes([i % 256 for i in range(bps)])
    cyl, head, sect = 1, 0, 2
    driver.write_sector(cyl, head, sect, normal_data)
    driver.flush()

    driver_reloaded1 = IMDImageDriver(str(path))
    ti1 = driver_reloaded1.tracks[(cyl, head)]
    _, type1, size1 = ti1.sector_data_info[sect]
    assert type1 == 1 and size1 == bps
    assert driver_reloaded1.read_sector(cyl, head, sect) == normal_data

    compressed_data = bytes([0x77] * bps)
    driver_reloaded1.write_sector(cyl, head, sect, compressed_data)
    assert driver_reloaded1.dirty is True
    assert driver_reloaded1.read_sector(cyl, head, sect) == compressed_data

    driver_reloaded1.flush()
    assert driver_reloaded1.dirty is False

    driver_reloaded2 = IMDImageDriver(str(path))
    ti2 = driver_reloaded2.tracks[(cyl, head)]
    _, type2, size2 = ti2.sector_data_info[sect]
    assert type2 == 2
    assert size2 == 1
    read_data_final = driver_reloaded2.read_sector(cyl, head, sect)
    assert read_data_final == compressed_data

def test_13_flush_no_changes(formatted_imd_driver):
    driver, path, _ = formatted_imd_driver
    initial_mtime = path.stat().st_mtime
    assert driver.dirty is False
    driver.flush()
    assert driver.dirty is False
    assert path.stat().st_mtime == initial_mtime

def test_14_real_imd_parse_basic(real_imd_driver):
    driver, path, fmt_720 = real_imd_driver
    assert driver.file_loaded
    assert isinstance(driver.comment, str)
    assert isinstance(driver.imd_version, str)
    assert len(driver.tracks) == fmt_720.physical_format.cylinders * fmt_720.physical_format.heads

    pf = driver.physical_format
    assert pf is not None
    assert pf.cylinders == fmt_720.physical_format.cylinders
    assert pf.heads == fmt_720.physical_format.heads
    assert pf.bytes_per_sector == fmt_720.physical_format.bytes_per_sector
    assert len(pf.track_formats) == 1
    tf = pf.track_formats[0]
    tf_expected = fmt_720.physical_format.track_formats[0]
    assert tf.sectors_per_track == tf_expected.sectors_per_track
    assert tf.encoding == tf_expected.encoding
    assert tf.rate == tf_expected.rate

def test_15_real_imd_read_boot_sector(real_imd_driver):
    driver, path, fmt_720 = real_imd_driver
    bps = fmt_720.physical_format.bytes_per_sector
    boot_sector_data = driver.read_sector(0, 0, 1)
    assert len(boot_sector_data) == bps
    assert boot_sector_data.endswith(b'\x55\xAA')

def test_16_real_imd_read_known_sector(real_imd_driver):
    driver, path, fmt_720 = real_imd_driver
    c, h, s = 0, 0, 2
    try:
        sector_data = driver.read_sector(c, h, s)
        assert len(sector_data) == fmt_720.physical_format.bytes_per_sector
    except ValueError as e:
         pytest.fail(f"Failed to read known sector C={c}, H={h}, S={s}: {e}")

def test_17_real_imd_read_last_sector(real_imd_driver):
    driver, path, fmt_720 = real_imd_driver
    pf = fmt_720.physical_format
    last_c = pf.cylinders - 1
    last_h = pf.heads - 1
    last_s = pf.track_formats[0].sectors_per_track

    try:
        sector_data = driver.read_sector(last_c, last_h, last_s)
        assert len(sector_data) == pf.bytes_per_sector
    except ValueError as e:
         pytest.fail(f"Failed to read last sector C={last_c}, H={last_h}, S={last_s}: {e}")

# --- New Tests ---
def test_18_imd_parse_eof_in_sector_map(tmp_path):
    test_path = tmp_path / "eof_secmap.imd"
    imd_data = build_test_imd_data(cylinders=1, heads=1, sectors_per_track=5)
    header_len = imd_data.index(0x1A) + 1
    track_header_len = 5
    truncated_data = imd_data[: header_len + track_header_len + 3] # Only 3 bytes of 5-byte map
    test_path.write_bytes(truncated_data)
    with pytest.raises(ValueError, match="EOF in sector map"):
        IMDImageDriver(str(test_path))

def test_19_imd_parse_eof_in_sector_data(tmp_path):
    test_path = tmp_path / "eof_secdata.imd"
    imd_data = build_test_imd_data(cylinders=1, heads=1, sectors_per_track=1, sector_size_code=0) # 128 bps
    header_len = imd_data.index(0x1A) + 1
    track_header_len = 5
    sector_map_len = 1
    sector_type_len = 1
    truncated_data = imd_data[: header_len + track_header_len + sector_map_len + sector_type_len + 50] # Only 50/128 bytes data
    test_path.write_bytes(truncated_data)
    with pytest.raises(ValueError, match="EOF in sector data"):
        IMDImageDriver(str(test_path))

def test_20_imd_parse_invalid_sector_type(tmp_path):
    test_path = tmp_path / "invalid_sectype.imd"
    imd_data = build_test_imd_data(cylinders=1, heads=1, sectors_per_track=1)
    header_len = imd_data.index(0x1A) + 1
    track_header_len = 5
    sector_map_len = 1
    sector_type_offset = header_len + track_header_len + sector_map_len
    assert imd_data[sector_type_offset] == 1 # Verify it's Normal
    imd_data[sector_type_offset] = 9 # Invalid type code
    test_path.write_bytes(imd_data)
    with pytest.raises(ValueError, match="Unknown sector type 9"):
        IMDImageDriver(str(test_path))

def test_21_imd_parse_optional_maps(tmp_path):
    test_path = tmp_path / "opt_maps.imd"
    imd_data = build_test_imd_data(
        cylinders=1, heads=2, sectors_per_track=2,
        has_cyl_map=True, has_head_map=True, has_size_map=False
    )
    test_path.write_bytes(imd_data)
    driver = IMDImageDriver(str(test_path))
    assert driver.tracks[(0, 0)].has_cyl_map
    assert driver.tracks[(0, 0)].has_head_map
    assert driver.tracks[(0, 0)].sector_cyl_map is not None
    assert driver.tracks[(0, 0)].sector_head_map is not None
    assert driver.tracks[(0, 0)].sector_size_map is None
    assert driver.tracks[(0, 1)].has_cyl_map
    assert driver.tracks[(0, 1)].has_head_map

def test_22_imd_parse_size_map(tmp_path):
    test_path = tmp_path / "size_map.imd"
    sizes = [128, 256]
    spt = len(sizes)
    imd_data = build_test_imd_data(
        cylinders=1, heads=1, sectors_per_track=spt,
        has_cyl_map=False, has_head_map=False,
        has_size_map=True, size_map_list=sizes
    )
    test_path.write_bytes(imd_data)
    driver = IMDImageDriver(str(test_path))
    ti = driver.tracks[(0, 0)]
    assert ti.sector_size_code == 0xFF
    assert ti.sector_size_map is not None
    assert ti.get_sector_size(1) == 128
    assert ti.get_sector_size(2) == 256
    assert len(driver.read_sector(0, 0, 1)) == 128
    assert len(driver.read_sector(0, 0, 2)) == 256 # Compressed fill, but size matches

def test_23_imd_read_boot_sector_no_track00(tmp_path):
    test_path = tmp_path / "no_boot_track.imd"
    imd_data = build_test_imd_data(cylinders=1, heads=1, sectors_per_track=1)
    header_len = imd_data.index(0x1A) + 1
    cyl_offset = header_len + 1
    imd_data[cyl_offset] = 1 # Set cylinder to 1
    test_path.write_bytes(imd_data)
    driver = IMDImageDriver(str(test_path))
    assert driver.physical_format is not None # Should derive based on max cyl=1
    assert driver.read_boot_sector_data() is None # Expect None as (0,0) is missing
