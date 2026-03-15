"""
Tests for second-round bugs found during audit.

BUG-6:  MITS DSK checksum write/validate mismatch + wrong headers in initialize_new_image
BUG-7:  FAT12 create_directory missing _commit_fat() in error path
BUG-8:  FAT12 configs_match only compares total_sectors
BUG-9:  H17 flush logs "0 sectors flushed"
BUG-10: Controller detection cache not invalidated after format/set_format
"""

import shutil
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from fatfloppy.core.controller import DiskController
from fatfloppy.core.filesystem_registry import FilesystemRegistry
from fatfloppy.core.filesystems.fat12_fs import FATFilesystem, FATVolumeInfo
from fatfloppy.core.physical_format import PhysicalFormat

RESOURCE_DIR = Path(__file__).parent.parent / "resources"
EMPTY_IMG_SRC = RESOURCE_DIR / "empty_formatted_144m.img"

_ALL_FORMATS = FilesystemRegistry.get_all_formats()


# ---------------------------------------------------------------------------
# BUG-6: MITS DSK checksum write/validate mismatch
#
# _calculate_sector_checksums (XOR) vs _validate_sector_checksum (additive).
# Also: initialize_new_image writes byte[0]=0x00 instead of track|0x80.
# Result: newly created or modified MITS images fail to reopen.
# ---------------------------------------------------------------------------


class TestBug6MitsDskChecksumMismatch:
    """MITS DSK: sectors written by FatFloppy must pass its own validation."""

    def test_new_mits_image_passes_own_validation(self) -> None:
        """Create a new MITS image, verify _validate_sector_checksum passes."""
        from fatfloppy.core.drivers.mits_dsk import (
            MITS_PHYSICAL_SECTOR_SIZE,
            MITS_SECTORS_PER_TRACK,
            MITS_SYSTEM_TRACK_COUNT,
            MITS_TRACKS,
            MITSDSKDriver,
        )

        expected_size = MITS_TRACKS * MITS_SECTORS_PER_TRACK * MITS_PHYSICAL_SECTOR_SIZE
        driver = MITSDSKDriver(
            file_path="/dev/null", image_data=b"\x00" * expected_size
        )
        driver.initialize_new_image(PhysicalFormat.create_default())

        # Validate system track sectors (tracks 0-5)
        system_ok = 0
        for track in range(MITS_SYSTEM_TRACK_COUNT):
            for sector in [0, 1, 15, 31]:
                offset = (
                    track * MITS_SECTORS_PER_TRACK + sector
                ) * MITS_PHYSICAL_SECTOR_SIZE
                sector_bytes = driver.image_data[
                    offset : offset + MITS_PHYSICAL_SECTOR_SIZE
                ]
                if driver._validate_sector_checksum(sector_bytes, track):
                    system_ok += 1

        assert system_ok > 0, (
            "BUG-6: No system track sectors pass _validate_sector_checksum "
            "after initialize_new_image. The XOR checksum in "
            "_calculate_sector_checksums does not match the additive "
            "algorithm in _validate_sector_checksum."
        )

    def test_new_mits_image_header_bytes_correct(self) -> None:
        """Verify byte[0] of each sector is track|0x80, not 0x00."""
        from fatfloppy.core.drivers.mits_dsk import (
            MITS_PHYSICAL_SECTOR_SIZE,
            MITS_SECTORS_PER_TRACK,
            MITS_TRACK_FLAG_MASK,
            MITS_TRACKS,
            MITSDSKDriver,
        )

        expected_size = MITS_TRACKS * MITS_SECTORS_PER_TRACK * MITS_PHYSICAL_SECTOR_SIZE
        driver = MITSDSKDriver(
            file_path="/dev/null", image_data=b"\x00" * expected_size
        )
        driver.initialize_new_image(PhysicalFormat.create_default())

        for track in [0, 5, 6, 40, 76]:
            offset = track * MITS_SECTORS_PER_TRACK * MITS_PHYSICAL_SECTOR_SIZE
            byte0 = driver.image_data[offset]
            expected = track | MITS_TRACK_FLAG_MASK
            assert byte0 == expected, (
                f"BUG-6: Track {track} sector 0 byte[0] = 0x{byte0:02X}, "
                f"expected 0x{expected:02X} (track | 0x80). "
                f"initialize_new_image writes wrong header."
            )

    def test_modified_sector_passes_validation(self) -> None:
        """Write to a system-track sector, verify it still validates."""
        from fatfloppy.core.drivers.mits_dsk import (
            MITS_PHYSICAL_SECTOR_SIZE,
            MITS_SECTORS_PER_TRACK,
            MITS_TRACKS,
            MITSDSKDriver,
        )

        expected_size = MITS_TRACKS * MITS_SECTORS_PER_TRACK * MITS_PHYSICAL_SECTOR_SIZE
        driver = MITSDSKDriver(
            file_path="/dev/null", image_data=b"\x00" * expected_size
        )
        driver.initialize_new_image(PhysicalFormat.create_default())
        driver._create_physical_format()

        # Write new data to track 0, sector 0
        new_data = bytes(range(128))
        driver.write_sector(0, 0, 0, new_data)

        # Flush to apply _reconstruct_physical_sector
        # (flush wants to write to file — mock it)
        for key, data in driver.modified_sectors.items():
            cyl, head, sec = key
            offset = (cyl * MITS_SECTORS_PER_TRACK + sec) * MITS_PHYSICAL_SECTOR_SIZE
            old_sector = driver.image_data[offset : offset + MITS_PHYSICAL_SECTOR_SIZE]
            new_sector = driver._reconstruct_physical_sector(old_sector, data, cyl, sec)
            driver.image_data[offset : offset + MITS_PHYSICAL_SECTOR_SIZE] = new_sector

        # Validate the modified sector
        offset = 0  # track 0, sector 0
        sector_bytes = driver.image_data[offset : offset + MITS_PHYSICAL_SECTOR_SIZE]
        assert driver._validate_sector_checksum(sector_bytes, 0), (
            "BUG-6: Modified system track sector fails _validate_sector_checksum. "
            "_reconstruct_physical_sector uses wrong checksum algorithm."
        )


# ---------------------------------------------------------------------------
# BUG-7: FAT12 create_directory missing _commit_fat() in error path
#
# fat12_fs.py:474-482 — on parent dir write failure, the cluster is freed
# in cache but _commit_fat() is not called. The on-disk FAT still has the
# cluster as allocated. Compare with lines 451-452 and 461-462.
# ---------------------------------------------------------------------------


class TestBug7CreateDirMissingCommitFat:
    """FAT12: error in create_directory must commit FAT changes to disk."""

    @pytest.fixture(autouse=True)
    def _skip_if_missing(self):
        if not EMPTY_IMG_SRC.exists():
            pytest.skip("empty_formatted_144m.img not found")

    def test_fat_committed_on_parent_write_failure(self, tmp_path: Path) -> None:
        """
        Force a failure when writing the parent directory entry.
        Verify the FAT is committed (cluster freed on disk), not just in cache.
        """
        img = tmp_path / "commitfat.img"
        shutil.copy(EMPTY_IMG_SRC, img)

        ctrl = DiskController()
        assert ctrl.open_disk(str(img), disk_type="IMG")
        fs = ctrl.filesystem
        assert isinstance(fs, FATFilesystem)

        # Record FAT state before
        free_before, _ = fs.get_free_space()

        # Patch _get_offset_for_directory_entry to fail — this is called
        # inside the try block at line 478 when computing the disk offset
        # for the PARENT directory entry, after . and .. are already written.
        with (
            patch.object(
                fs,
                "_get_offset_for_directory_entry",
                side_effect=OSError("Simulated write failure"),
            ),
            pytest.raises(OSError, match="Failed to write directory entry"),
        ):
            fs.create_directory("/FAILDIR")

        # Now verify: the cluster that was allocated and then freed in the
        # error handler should be freed ON DISK, not just in cache.
        # Re-read FAT from disk to check.
        fs.fat_cache = None  # Force reload from disk
        fs._cached_allocated_clusters = None
        fs._load_fat_cache()

        free_after, _ = fs.get_free_space()
        assert free_after == free_before, (
            f"BUG-7: After create_directory failure, free space is "
            f"{free_after} but should be {free_before}. "
            f"The cluster was freed in cache but _commit_fat() was not called, "
            f"so the on-disk FAT still shows it as allocated."
        )


# ---------------------------------------------------------------------------
# BUG-8: FAT12 configs_match only compares total_sectors
#
# fat12_fs.py:266 — two configs with same total_sectors but different
# layout (bytes_per_sector, sectors_per_cluster, etc.) incorrectly match.
# ---------------------------------------------------------------------------


class TestBug8ConfigsMatchIncomplete:
    """FAT12: configs_match must compare layout-affecting fields, not just total_sectors."""

    def test_same_total_sectors_different_layout_should_not_match(self) -> None:
        """Two configs with total_sectors=720 but different geometry must not match."""
        # ibm_3.5_360k: 80 cyl, 1 head, 9 spt → 720 total sectors
        config_35 = FATVolumeInfo(
            bytes_per_sector=512,
            sectors_per_cluster=2,
            reserved_sectors=1,
            num_fats=2,
            root_entries=112,
            total_sectors=720,
            media_descriptor=0xFC,
            sectors_per_fat=2,
            sectors_per_track=9,
            num_heads=1,
        )

        # ibm_5.25_360k: 40 cyl, 2 heads, 9 spt → 720 total sectors
        config_525 = FATVolumeInfo(
            bytes_per_sector=512,
            sectors_per_cluster=2,
            reserved_sectors=1,
            num_fats=2,
            root_entries=112,
            total_sectors=720,
            media_descriptor=0xFD,
            sectors_per_fat=2,
            sectors_per_track=9,
            num_heads=2,
        )

        # These have same total_sectors but different media_descriptor and num_heads.
        # They represent physically different disks and should NOT match.
        assert not FATFilesystem.configs_match(config_35, config_525), (
            "BUG-8: configs_match returns True for two configs with "
            "total_sectors=720 but different media descriptors and head counts. "
            "Only total_sectors is compared."
        )

    def test_different_sectors_per_cluster_should_not_match(self) -> None:
        """Same total_sectors but different cluster size must not match."""
        config_1spc = FATVolumeInfo(
            total_sectors=2880,
            sectors_per_cluster=1,
            bytes_per_sector=512,
            sectors_per_fat=9,
        )
        config_2spc = FATVolumeInfo(
            total_sectors=2880,
            sectors_per_cluster=2,
            bytes_per_sector=512,
            sectors_per_fat=9,
        )
        assert not FATFilesystem.configs_match(config_1spc, config_2spc), (
            "BUG-8: configs_match returns True for configs with same "
            "total_sectors but different sectors_per_cluster."
        )

    def test_identical_configs_still_match(self) -> None:
        """Two truly identical configs should match."""
        config_a = FATVolumeInfo(
            total_sectors=2880,
            sectors_per_cluster=1,
            bytes_per_sector=512,
            sectors_per_fat=9,
            media_descriptor=0xF0,
            num_heads=2,
        )
        config_b = FATVolumeInfo(
            total_sectors=2880,
            sectors_per_cluster=1,
            bytes_per_sector=512,
            sectors_per_fat=9,
            media_descriptor=0xF0,
            num_heads=2,
        )
        assert FATFilesystem.configs_match(config_a, config_b)


# ---------------------------------------------------------------------------
# BUG-9: H17 flush logs "0 sectors flushed"
#
# h17.py:348 clears modified_sectors, then line 351 logs len() which is 0.
# ---------------------------------------------------------------------------


class TestBug9H17FlushLogStale:
    """H17: flush log message should report actual sector count, not 0."""

    def test_flush_log_reports_nonzero_count(self, tmp_path: Path) -> None:
        """Write sectors, flush, verify log says how many were flushed."""
        from fatfloppy.core.drivers.h17 import H17ImageDriver

        h17_img = RESOURCE_DIR / "HDOS" / "HDOS_2-0_TEST.h17disk"
        if not h17_img.exists():
            pytest.skip("H17 test image not found")

        test_img = tmp_path / "h17_flush.h17disk"
        shutil.copy(h17_img, test_img)

        driver = H17ImageDriver(file_path=str(test_img))

        # Write a sector to make the image dirty
        driver.write_sector(0, 0, 0, b"\xaa" * 256)
        assert driver.dirty
        assert len(driver.modified_sectors) == 1

        # Capture log output during flush
        with patch.object(driver.logger, "info", wraps=driver.logger.info) as mock_log:
            driver.flush()

        # Find the flush success message
        flush_messages = [
            call.args[0]
            for call in mock_log.call_args_list
            if "flushed" in call.args[0].lower()
        ]
        assert flush_messages, "No flush log message found"

        # BUG-9: The message says "0 sectors" because modified_sectors was
        # cleared before the log call.
        for msg in flush_messages:
            assert "0 sectors" not in msg, (
                f"BUG-9: Flush log says '{msg}' — reports 0 sectors because "
                f"modified_sectors.clear() is called before len() is logged."
            )


# ---------------------------------------------------------------------------
# BUG-10: Controller detection cache not invalidated after format/set_format
#
# After format_disk_media() or set_format(), _detection_cached stays True
# and detect_format() returns stale cached data.
# ---------------------------------------------------------------------------


class TestBug10DetectionCacheStale:
    """Controller: detection cache must be invalidated when format changes."""

    @pytest.fixture(autouse=True)
    def _skip_if_missing(self):
        if not EMPTY_IMG_SRC.exists():
            pytest.skip("empty_formatted_144m.img not found")

    def test_cache_invalidated_after_format_disk_media(self, tmp_path: Path) -> None:
        """Format to a different profile, verify detect_format returns new name."""
        img = tmp_path / "cache_test.img"
        shutil.copy(EMPTY_IMG_SRC, img)

        ctrl = DiskController()
        assert ctrl.open_disk(str(img), disk_type="IMG")

        # First detection — caches result
        name1, _, _ = ctrl.detect_format()
        assert name1 is not None

        # Reformat to 720K profile
        assert ctrl.format_disk_media("ibm_3.5_720k")

        # Second detection — should reflect the new format
        name2, _, _ = ctrl.detect_format()

        assert name2 != name1, (
            f"BUG-10: After reformatting from '{name1}' to 720K, "
            f"detect_format() still returns '{name2}'. "
            f"The detection cache was not invalidated by format_disk_media()."
        )

    def test_cache_invalidated_after_set_format(self, tmp_path: Path) -> None:
        """Change format via set_format(), verify detect_format cache is cleared."""
        img = tmp_path / "setfmt_test.img"
        shutil.copy(EMPTY_IMG_SRC, img)

        ctrl = DiskController()
        assert ctrl.open_disk(str(img), disk_type="IMG")

        # Cache detection
        name1, _, pf1 = ctrl.detect_format()
        assert ctrl._detection_cached is True

        # Change format via set_format (explicit user action)
        profile_720 = ctrl.get_format_by_name("ibm_3.5_720k")
        ctrl.set_format(profile_720)

        # After set_format, cache should be invalidated
        assert ctrl._detection_cached is False, (
            "BUG-10: _detection_cached is still True after set_format(). "
            "Subsequent detect_format() calls will return stale data."
        )
