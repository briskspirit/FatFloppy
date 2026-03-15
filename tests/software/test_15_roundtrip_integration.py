"""
Integration tests: write-read roundtrip for all filesystems using real disk images.

Addresses TEST-1 (real-image counterparts for mocked error tests) and
TEST-2 (write-read roundtrip verification for FAT12, CP/M, HDOS).
"""

import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from fatfloppy.core.controller import DiskController
from fatfloppy.core.filesystem_registry import FilesystemRegistry
from fatfloppy.core.filesystems.cpm_fs import CPMFilesystem
from fatfloppy.core.filesystems.fat12_fs import FATFilesystem
from fatfloppy.core.filesystems.hdos_fs import HDOSFilesystem

RESOURCE_DIR = Path(__file__).parent.parent / "resources"
EMPTY_IMG_SRC = RESOURCE_DIR / "empty_formatted_144m.img"
POPULATED_IMG_SRC = RESOURCE_DIR / "populated_read_test_144m.img"
CPM_RESOURCE_DIR = RESOURCE_DIR / "CPM"
HDOS_RESOURCE_DIR = RESOURCE_DIR / "HDOS"

_ALL_FORMATS = FilesystemRegistry.get_all_formats()


# ---------------------------------------------------------------------------
# FAT12 roundtrip tests
# ---------------------------------------------------------------------------


class TestFAT12Roundtrip:
    """Write-read roundtrip tests on real FAT12 images."""

    def _open_empty_fat12(self, tmp_path: Path) -> tuple[DiskController, Path]:
        img = tmp_path / "fat12_roundtrip.img"
        shutil.copy(EMPTY_IMG_SRC, img)
        ctrl = DiskController()
        assert ctrl.open_disk(str(img), disk_type="IMG")
        assert isinstance(ctrl.filesystem, FATFilesystem)
        return ctrl, img

    @pytest.fixture(autouse=True)
    def _skip_if_missing(self):
        if not EMPTY_IMG_SRC.exists():
            pytest.skip("empty_formatted_144m.img not found")

    def test_small_file_roundtrip(self, tmp_path: Path) -> None:
        """Write a small file, flush, reopen, read back, verify byte-for-byte."""
        ctrl, img = self._open_empty_fat12(tmp_path)
        payload = b"Hello, FAT12 roundtrip test!\x00\xff\xfe"
        ctrl.write_file("/HELLO.TXT", payload)
        ctrl.flush()
        ctrl.close_disk()

        ctrl2 = DiskController()
        assert ctrl2.open_disk(str(img), disk_type="IMG")
        readback = ctrl2.read_file("/HELLO.TXT")
        assert readback == payload
        ctrl2.close_disk()

    def test_multicluster_file_roundtrip(self, tmp_path: Path) -> None:
        """Write a file spanning multiple clusters, verify after reopen."""
        ctrl, img = self._open_empty_fat12(tmp_path)
        payload = bytes(range(256)) * 20  # 5120 bytes = 10 sectors on 1.44M
        ctrl.write_file("/BIG.BIN", payload)
        ctrl.flush()
        ctrl.close_disk()

        ctrl2 = DiskController()
        assert ctrl2.open_disk(str(img), disk_type="IMG")
        readback = ctrl2.read_file("/BIG.BIN")
        assert readback == payload
        ctrl2.close_disk()

    def test_overwrite_file_roundtrip(self, tmp_path: Path) -> None:
        """Write a file, overwrite it with different content, verify."""
        ctrl, img = self._open_empty_fat12(tmp_path)
        ctrl.write_file("/OVER.TXT", b"original content")
        ctrl.flush()

        ctrl.write_file("/OVER.TXT", b"replaced content!!")
        ctrl.flush()
        ctrl.close_disk()

        ctrl2 = DiskController()
        assert ctrl2.open_disk(str(img), disk_type="IMG")
        readback = ctrl2.read_file("/OVER.TXT")
        assert readback == b"replaced content!!"
        ctrl2.close_disk()

    def test_delete_then_write_roundtrip(self, tmp_path: Path) -> None:
        """Delete a file, write a new one, verify both operations persisted."""
        ctrl, img = self._open_empty_fat12(tmp_path)
        ctrl.write_file("/A.TXT", b"fileA")
        ctrl.write_file("/B.TXT", b"fileB")
        ctrl.flush()

        ctrl.delete_item("/A.TXT")
        ctrl.write_file("/C.TXT", b"fileC")
        ctrl.flush()
        ctrl.close_disk()

        ctrl2 = DiskController()
        assert ctrl2.open_disk(str(img), disk_type="IMG")
        listing = ctrl2.list_directory("/")
        names = {e["name"] for e in listing}
        assert "A.TXT" not in names
        assert "B.TXT" in names
        assert "C.TXT" in names
        assert ctrl2.read_file("/B.TXT") == b"fileB"
        assert ctrl2.read_file("/C.TXT") == b"fileC"
        ctrl2.close_disk()

    def test_subdirectory_file_roundtrip(self, tmp_path: Path) -> None:
        """Create subdirectory, write file inside, verify after reopen."""
        ctrl, img = self._open_empty_fat12(tmp_path)
        ctrl.create_directory("/MYDIR")
        ctrl.write_file("/MYDIR/SUB.TXT", b"in subdir")
        ctrl.flush()
        ctrl.close_disk()

        ctrl2 = DiskController()
        assert ctrl2.open_disk(str(img), disk_type="IMG")
        readback = ctrl2.read_file("/MYDIR/SUB.TXT")
        assert readback == b"in subdir"
        ctrl2.close_disk()

    def test_invalid_filename_rejected_on_real_image(self, tmp_path: Path) -> None:
        """Verify invalid 8.3 filenames are rejected when using real images."""
        ctrl, _ = self._open_empty_fat12(tmp_path)
        with pytest.raises((ValueError, OSError)):
            ctrl.write_file("/TOOLONGFILENAME.TXT", b"data")
        ctrl.close_disk()

    def test_delete_nonempty_dir_rejected_on_real_image(self, tmp_path: Path) -> None:
        """Verify deleting a non-empty directory raises on a real image."""
        ctrl, _ = self._open_empty_fat12(tmp_path)
        ctrl.create_directory("/NOTEMPTY")
        ctrl.write_file("/NOTEMPTY/F.TXT", b"data")
        ctrl.flush()
        with pytest.raises(OSError):
            ctrl.delete_item("/NOTEMPTY")
        ctrl.close_disk()

    def test_multiple_files_free_space_accounting(self, tmp_path: Path) -> None:
        """Write several files, check free space decreases correctly."""
        ctrl, _ = self._open_empty_fat12(tmp_path)
        free_before, total = ctrl.get_free_space()
        assert total > 0

        ctrl.write_file("/F1.TXT", b"x" * 512)
        ctrl.write_file("/F2.TXT", b"y" * 1024)
        free_after, _ = ctrl.get_free_space()

        assert free_after < free_before
        bps = ctrl.filesystem.boot_sector.bytes_per_sector
        spc = ctrl.filesystem.boot_sector.sectors_per_cluster
        cluster_size = bps * spc
        # Two files: 512 bytes = 1 cluster, 1024 bytes = 2 clusters = 3 total
        assert free_before - free_after == 3 * cluster_size
        ctrl.close_disk()


# ---------------------------------------------------------------------------
# CP/M roundtrip tests
# ---------------------------------------------------------------------------


class TestCPMRoundtrip:
    """Write-read roundtrip tests on real CP/M images."""

    @pytest.fixture(autouse=True)
    def _skip_if_missing(self):
        if not (CPM_RESOURCE_DIR / "disk1.img").exists():
            pytest.skip("CP/M disk1.img not found")

    def _open_cpm(self, tmp_path: Path) -> tuple[DiskController, Path]:
        img = tmp_path / "cpm_roundtrip.img"
        shutil.copy(CPM_RESOURCE_DIR / "disk1.img", img)
        ctrl = DiskController()
        assert ctrl.open_disk(
            str(img),
            disk_type="IMG",
            format_info={"format_name": "cpm_8_sssd_250k"},
        )
        assert isinstance(ctrl.filesystem, CPMFilesystem)
        return ctrl, img

    def test_write_read_roundtrip(self, tmp_path: Path) -> None:
        """Write a new file, flush, reopen, verify byte-for-byte."""
        ctrl, img = self._open_cpm(tmp_path)
        payload = b"CP/M roundtrip test data\r\n" * 10
        ctrl.write_file("/NEWFILE.TXT", payload)
        ctrl.flush()
        ctrl.close_disk()

        ctrl2 = DiskController()
        assert ctrl2.open_disk(
            str(img),
            disk_type="IMG",
            format_info={"format_name": "cpm_8_sssd_250k"},
        )
        readback = ctrl2.read_file("/NEWFILE.TXT")
        # CP/M pads to 128-byte records and strips trailing 0x1A
        assert readback.startswith(payload)
        ctrl2.close_disk()

    def test_delete_and_rewrite(self, tmp_path: Path) -> None:
        """Delete a file then write a new one, verify directory is consistent."""
        ctrl, img = self._open_cpm(tmp_path)
        listing_before = ctrl.list_directory("/")
        names_before = {e["name"] for e in listing_before}

        # Pick the first file to delete
        if not names_before:
            pytest.skip("No files on CP/M image to test deletion")
        victim = next(iter(names_before))
        ctrl.delete_item(f"/{victim}")
        ctrl.write_file("/FRESH.COM", b"\xc9" * 256)  # 256 bytes of RET
        ctrl.flush()
        ctrl.close_disk()

        ctrl2 = DiskController()
        assert ctrl2.open_disk(
            str(img),
            disk_type="IMG",
            format_info={"format_name": "cpm_8_sssd_250k"},
        )
        listing_after = ctrl2.list_directory("/")
        names_after = {e["name"] for e in listing_after}
        assert victim not in names_after
        assert "FRESH.COM" in names_after
        readback = ctrl2.read_file("/FRESH.COM")
        assert readback == b"\xc9" * 256
        ctrl2.close_disk()


# ---------------------------------------------------------------------------
# HDOS roundtrip tests
# ---------------------------------------------------------------------------


class TestHDOSRoundtrip:
    """Write-read roundtrip tests on real HDOS images."""

    @pytest.fixture(autouse=True)
    def _skip_if_missing(self):
        if not (HDOS_RESOURCE_DIR / "HDOS_2-0_TEST.h8d").exists():
            pytest.skip("HDOS_2-0_TEST.h8d not found")

    def _open_formatted_hdos(self, tmp_path: Path) -> tuple[DiskController, Path]:
        """Create a freshly formatted blank HDOS image."""
        profile_name = "hdos_5.25_100k"
        ctrl = DiskController()
        profile = ctrl.get_format_by_name(profile_name)
        assert profile

        img = tmp_path / "hdos_roundtrip.h8d"
        img.write_bytes(b"\x00" * profile.physical_format.total_bytes)
        assert ctrl.open_disk(
            str(img), disk_type="IMG", format_info={"format_name": profile_name}
        )
        assert ctrl.format_disk_media(profile_name)
        assert isinstance(ctrl.filesystem, HDOSFilesystem)
        return ctrl, img

    def test_write_read_roundtrip_on_blank(self, tmp_path: Path) -> None:
        """Format, write, flush, reopen, verify byte-for-byte."""
        ctrl, img = self._open_formatted_hdos(tmp_path)
        payload = b"HDOS roundtrip test\r\n" * 5
        ctrl.write_file("/TEST.TXT", payload)
        ctrl.flush()
        ctrl.close_disk()

        ctrl2 = DiskController()
        assert ctrl2.open_disk(
            str(img), disk_type="IMG", format_info={"format_name": "hdos_5.25_100k"}
        )
        readback = ctrl2.read_file("/TEST.TXT")
        # HDOS pads to sector boundaries and may trim trailing nulls
        assert readback[: len(payload)] == payload
        ctrl2.close_disk()

    def test_multiple_files_roundtrip(self, tmp_path: Path) -> None:
        """Write multiple files, reopen, verify all present."""
        ctrl, img = self._open_formatted_hdos(tmp_path)
        files = {
            "/AAA.TXT": b"alpha",
            "/BBB.TXT": b"bravo" * 50,
            "/CCC.BIN": bytes(range(256)),
        }
        for name, data in files.items():
            ctrl.write_file(name, data)
        ctrl.flush()
        ctrl.close_disk()

        ctrl2 = DiskController()
        assert ctrl2.open_disk(
            str(img), disk_type="IMG", format_info={"format_name": "hdos_5.25_100k"}
        )
        listing = ctrl2.list_directory("/")
        written_names = {Path(n).name for n in files}
        listed_names = {e["name"] for e in listing}
        for name in written_names:
            assert name in listed_names
        for name, data in files.items():
            readback = ctrl2.read_file(name)
            assert readback[: len(data)] == data
        ctrl2.close_disk()
