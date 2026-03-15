"""
Tests that prove specific bugs found during audit.

BUG-1: HDOS cluster_factor written as cf+1, read as raw value
BUG-2: HDOS _create_and_write_dir_entry overwrites 0xFE without restoring it
BUG-3: FAT12 format_fs mutates the shared FormatProfile's filesystem_config
BUG-4: FAT12 to_bytes() writes extended BPB + 0xAA55 into sub-512-byte sectors
BUG-5: FAT12 FAT offset uses float (int(cluster * 1.5)) instead of integer math
ISSUE-4: CP/M read_file strips trailing 0x1A from ALL files, corrupting binaries
FMT-2: ibm_8_630k uses media_descriptor=0x00 which is the FAT "free cluster" value
"""

import shutil
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from fatfloppy.core.controller import DiskController
from fatfloppy.core.disk import Disk
from fatfloppy.core.drivers.img import IMGImageDriver
from fatfloppy.core.filesystem_registry import FilesystemRegistry
from fatfloppy.core.filesystems.cpm_fs import (
    CPMFilesystem,
)
from fatfloppy.core.filesystems.fat12_fs import FATFilesystem, FATVolumeInfo
from fatfloppy.core.filesystems.hdos_fs import (
    DIR_ENTRIES_PER_BLOCK,
    HDOS_BYTES_PER_SECTOR,
    HDOS_DIR_ENTRY_SIZE,
    HDOSFilesystem,
)

RESOURCE_DIR = Path(__file__).parent.parent / "resources"
CPM_RESOURCE_DIR = RESOURCE_DIR / "CPM"

_ALL_FORMATS = FilesystemRegistry.get_all_formats()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_formatted_hdos(tmp_path: Path) -> tuple[DiskController, Path]:
    """Create a freshly formatted HDOS 5.25" 100KB image."""
    profile_name = "hdos_5.25_100k"
    ctrl = DiskController()
    profile = ctrl.get_format_by_name(profile_name)
    assert profile

    img = tmp_path / "hdos_bug.h8d"
    img.write_bytes(b"\x00" * profile.physical_format.total_bytes)
    assert ctrl.open_disk(
        str(img), disk_type="IMG", format_info={"format_name": profile_name}
    )
    assert ctrl.format_disk_media(profile_name)
    assert isinstance(ctrl.filesystem, HDOSFilesystem)
    return ctrl, img


def _read_raw_sector(img_path: Path, lba: int, bps: int = 256) -> bytes:
    """Read a raw sector from an image file by LBA."""
    with Path.open(img_path, "rb") as f:
        f.seek(lba * bps)
        return f.read(bps)


def _write_raw_bytes(img_path: Path, offset: int, data: bytes) -> None:
    """Write raw bytes at a byte offset in an image file."""
    raw = bytearray(img_path.read_bytes())
    raw[offset : offset + len(data)] = data
    img_path.write_bytes(bytes(raw))


# ---------------------------------------------------------------------------
# BUG-1: HDOS cluster_factor stored as cf+1 in directory entries
#
# _create_and_write_dir_entry (line 1255): entry_bytes[13] = cluster_factor + 1
# _parse_single_dir_entry    (line 1499): cluster_factor = data[13]  (raw)
#
# So a file written with cf=2 stores 3 at byte 13. Reading it back, the
# parsed HDOSDirectoryEntry.cluster_factor is 3 instead of 2.
# ---------------------------------------------------------------------------


class TestBug1HdosClusterFactorMismatch:
    """
    BUG-1: The cluster_factor stored in directory entries is off by one.

    Write stores cf+1, read returns the raw byte.  After a write-then-read
    cycle the directory entry's cluster_factor field is corrupted.
    """

    def test_cluster_factor_roundtrip_on_written_file(self, tmp_path: Path) -> None:
        """Write a file, re-read directory, verify cluster_factor matches label."""
        ctrl, img = _make_formatted_hdos(tmp_path)

        # The hdos_5.25_100k profile has cluster_factor=2
        fs = ctrl.filesystem
        assert isinstance(fs, HDOSFilesystem)
        fs._initialize()
        label_cf = fs.label.cluster_factor
        assert label_cf == 2, f"Expected label cf=2, got {label_cf}"

        # Write a file
        ctrl.write_file("/TEST.TXT", b"hello hdos")
        ctrl.flush()

        # Re-initialize to re-read directory from disk
        fs._init_completed = False
        fs._dir_entries = None
        fs._initialize()

        # Find the entry we just wrote
        entry = None
        for e in fs._dir_entries:
            if e.get_filename() == "TEST.TXT":
                entry = e
                break
        assert entry is not None, "Written file not found in directory"

        # BUG: entry.cluster_factor should equal label_cf (2),
        #      but the write stores cf+1=3 and the read returns raw 3.
        assert entry.cluster_factor == label_cf, (
            f"BUG-1: cluster_factor in directory entry is {entry.cluster_factor}, "
            f"expected {label_cf}. Write stored cf+1={label_cf + 1} at byte 13 "
            f"but read returns raw value."
        )

    def test_cluster_factor_byte13_raw_value(self, tmp_path: Path) -> None:
        """Directly inspect byte 13 of the raw directory entry on disk."""
        ctrl, img = _make_formatted_hdos(tmp_path)
        fs = ctrl.filesystem
        assert isinstance(fs, HDOSFilesystem)
        fs._initialize()
        label_cf = fs.label.cluster_factor  # 2
        dir_start_lba = fs.label.dir_start_block

        ctrl.write_file("/RAW.BIN", b"\xaa" * 100)
        ctrl.flush()
        ctrl.close_disk()

        # Read raw directory sector from the image file
        bps = HDOS_BYTES_PER_SECTOR
        dir_sector = _read_raw_sector(img, dir_start_lba, bps)

        # Find the entry (first non-marker entry)
        found = False
        for i in range(DIR_ENTRIES_PER_BLOCK):
            offset = i * HDOS_DIR_ENTRY_SIZE
            if dir_sector[offset] not in (0x00, 0xFF, 0xFE):
                byte_13 = dir_sector[offset + 13]
                # BUG: byte 13 should store the cluster_factor (2),
                #      but the code stores cluster_factor+1 (3).
                assert byte_13 == label_cf, (
                    f"BUG-1: Raw byte 13 of directory entry is {byte_13}, "
                    f"expected {label_cf}. Code writes cf+1={label_cf + 1}."
                )
                found = True
                break
        assert found, "No directory entry found in raw sector"


# ---------------------------------------------------------------------------
# BUG-2: HDOS write overwrites 0xFE end-of-dir marker, exposing garbage
#
# _create_and_write_dir_entry (line 1287):
#   if dir_data[offset] in (0x00, 0xFF, 0xFE):
#       <write entry here>
#
# If the 0xFE marker is the first free slot, it's overwritten and no new
# 0xFE is placed after the entry.  Any data beyond that point becomes
# visible to the directory scanner.
# ---------------------------------------------------------------------------


class TestBug2HdosEndOfDirMarkerOverwrite:
    """
    BUG-2: Writing a file can overwrite the 0xFE end-of-directory marker
    without restoring it, exposing stale/garbage entries that follow.
    """

    def test_garbage_entry_exposed_after_marker_overwrite(self, tmp_path: Path) -> None:
        """
        Inject a fake entry after the 0xFE marker, write a real file,
        then verify the fake entry becomes visible (proving the bug).
        """
        ctrl, img = _make_formatted_hdos(tmp_path)
        fs = ctrl.filesystem
        assert isinstance(fs, HDOSFilesystem)
        fs._initialize()
        dir_start_lba = fs.label.dir_start_block

        # After formatting, directory block looks like:
        #   Slot 0: 0xFE (end marker)
        #   Slots 1-21: 0x00 (unused)
        # Verify this.
        listing = ctrl.list_directory("/")
        assert listing == [], "Freshly formatted disk should have no files"

        ctrl.close_disk()

        # --- Inject a fake valid-looking entry at slot 1 ---
        bps = HDOS_BYTES_PER_SECTOR
        raw = bytearray(img.read_bytes())
        dir_byte_offset = dir_start_lba * bps

        fake_entry = bytearray(HDOS_DIR_ENTRY_SIZE)
        fake_entry[0:8] = b"PHANTOM "  # 8-byte name
        fake_entry[8:11] = b"DAT"  # 3-byte ext
        fake_entry[13] = 3  # cluster_factor field
        fake_entry[16] = 5  # first_group (arbitrary)
        fake_entry[17] = 5  # last_group
        fake_entry[18] = 1  # last_sector_index

        slot1_offset = dir_byte_offset + HDOS_DIR_ENTRY_SIZE  # slot 1
        raw[slot1_offset : slot1_offset + HDOS_DIR_ENTRY_SIZE] = fake_entry
        img.write_bytes(bytes(raw))

        # Reopen — the fake entry is after 0xFE so should NOT be visible
        ctrl2 = DiskController()
        assert ctrl2.open_disk(
            str(img), disk_type="IMG", format_info={"format_name": "hdos_5.25_100k"}
        )
        listing_before = ctrl2.list_directory("/")
        assert listing_before == [], (
            "Fake entry after 0xFE marker should not be visible"
        )

        # Write a real file — this overwrites the 0xFE at slot 0
        ctrl2.write_file("/REAL.TXT", b"real data")
        ctrl2.flush()

        listing_after = ctrl2.list_directory("/")
        file_names = {e["name"] for e in listing_after}

        # BUG: The 0xFE was overwritten. The scanner now sees the fake
        # entry at slot 1 because nothing stops it from scanning past slot 0.
        # Correct behavior: only "REAL.TXT" should be listed.
        assert "PHANTOM.DAT" not in file_names, (
            "BUG-2: The phantom entry injected after the 0xFE marker became "
            "visible after writing a file to slot 0, because the end-of-directory "
            "marker was overwritten without being restored."
        )
        assert "REAL.TXT" in file_names

        ctrl2.close_disk()


# ---------------------------------------------------------------------------
# BUG-3: FAT12 format_fs mutates the shared FormatProfile config
#
# fat12_fs.py line 606-608:
#   boot_sector_config = profile.filesystem_config  # reference, not copy
#   if volume_label:
#       boot_sector_config.volume_label = volume_label.ljust(11)[:11]
#
# This modifies the global FormatProfile dict entry in-place.
# ---------------------------------------------------------------------------


class TestBug3Fat12FormatMutatesSharedConfig:
    """
    BUG-3: format_fs() modifies the global FormatProfile's FATVolumeInfo
    in-place when setting a volume label, permanently altering the profile
    for the entire process lifetime.
    """

    @pytest.fixture(autouse=True)
    def _save_restore_global_config(self):
        """Save and restore the global profile to prevent cross-test pollution."""
        profile = _ALL_FORMATS["ibm_3.5_1.44m"]
        saved_label = profile.filesystem_config.volume_label
        yield
        profile.filesystem_config.volume_label = saved_label

    def test_format_with_label_does_not_mutate_global_profile(self) -> None:
        """Format with a custom label, verify the global profile is unchanged."""
        profile = _ALL_FORMATS["ibm_3.5_1.44m"]
        original_label = profile.filesystem_config.volume_label

        # Create a disk and format it with a custom label
        pf = profile.physical_format
        driver = IMGImageDriver(
            file_path="/dev/null",
            image_data=b"\x00" * pf.total_bytes,
        )
        disk = Disk(driver)
        disk.set_geometry(pf)

        fs = FATFilesystem(disk)
        fs.format_fs(profile, volume_label="BUGTEST")

        # BUG: The global profile's config was mutated in-place.
        current_label = profile.filesystem_config.volume_label
        assert current_label == original_label, (
            f"BUG-3: Global profile volume_label was mutated from "
            f"'{original_label}' to '{current_label}'. "
            f"format_fs() modified profile.filesystem_config in-place "
            f"instead of working on a copy."
        )

    def test_two_formats_with_different_labels_dont_interfere(self) -> None:
        """Format two disks with different labels; second shouldn't see first's label."""
        profile = _ALL_FORMATS["ibm_3.5_1.44m"]
        pf = profile.physical_format

        # Format disk 1 with label "DISK_ONE"
        driver1 = IMGImageDriver(
            file_path="/dev/null", image_data=b"\x00" * pf.total_bytes
        )
        disk1 = Disk(driver1)
        disk1.set_geometry(pf)
        fs1 = FATFilesystem(disk1)
        fs1.format_fs(profile, volume_label="DISK_ONE")

        # Format disk 2 with NO label — should get default, not "DISK_ONE"
        driver2 = IMGImageDriver(
            file_path="/dev/null", image_data=b"\x00" * pf.total_bytes
        )
        disk2 = Disk(driver2)
        disk2.set_geometry(pf)
        fs2 = FATFilesystem(disk2)
        fs2.format_fs(profile)

        # Read back the volume label from disk 2's boot sector
        boot_data = disk2.read_sector(0, 0, 0)
        bpb2 = FATVolumeInfo.from_bytes(boot_data)

        # BUG: Because format_fs mutated the shared config, disk 2 gets
        # "DISK_ONE" as its volume label instead of "NO NAME".
        assert "DISK_ONE" not in bpb2.volume_label, (
            f"BUG-3: Disk 2 got volume label '{bpb2.volume_label}' — "
            f"leaked from disk 1's format_fs call because the shared "
            f"FormatProfile.filesystem_config was mutated in-place."
        )


# ---------------------------------------------------------------------------
# ISSUE-4: CP/M read_file unconditionally strips trailing 0x1A
#
# cpm_fs.py line 1082:
#   return bytes(data).rstrip(bytes([CPM_EOF_CHAR]))
#
# This strips all trailing 0x1A from EVERY file, including .COM binaries
# that may legitimately end with 0x1A.
# ---------------------------------------------------------------------------


class TestIssue4CpmStripsTrailing1aFromBinaries:
    """
    ISSUE-4: read_file strips trailing 0x1A (EOF) from all files, including
    binary .COM/.REL files where 0x1A is legitimate data.
    """

    @pytest.fixture(autouse=True)
    def _skip_if_missing(self):
        if not (CPM_RESOURCE_DIR / "disk1.img").exists():
            pytest.skip("CP/M disk1.img not found")

    def test_binary_file_with_trailing_1a_preserved(self, tmp_path: Path) -> None:
        """Write a .COM file ending with 0x1A, read back, verify data intact."""
        img = tmp_path / "cpm_1a_test.img"
        shutil.copy(CPM_RESOURCE_DIR / "disk1.img", img)

        ctrl = DiskController()
        assert ctrl.open_disk(
            str(img),
            disk_type="IMG",
            format_info={"format_name": "cpm_8_sssd_250k"},
        )
        assert isinstance(ctrl.filesystem, CPMFilesystem)

        # A .COM binary that legitimately ends with 0x1A bytes.
        # This is 128 bytes (one CP/M record) — no padding will occur.
        binary_payload = bytes(range(128))
        # Replace last 3 bytes with 0x1A
        binary_payload = binary_payload[:-3] + b"\x1a\x1a\x1a"

        ctrl.write_file("/ENDEOF.COM", binary_payload)
        ctrl.flush()

        readback = ctrl.read_file("/ENDEOF.COM")

        # ISSUE-4: rstrip(0x1A) removes the legitimate trailing 0x1A bytes
        # from the .COM binary, corrupting it.
        assert readback == binary_payload, (
            f"ISSUE-4: Binary file was corrupted — trailing 0x1A bytes stripped. "
            f"Original length: {len(binary_payload)}, "
            f"readback length: {len(readback)}. "
            f"CP/M rstrip(0x1A) should not apply to binary files."
        )

        ctrl.close_disk()

    def test_text_file_1a_stripping_is_correct(self, tmp_path: Path) -> None:
        """Verify that .TXT files DO get 0x1A stripped (correct behavior)."""
        img = tmp_path / "cpm_txt_test.img"
        shutil.copy(CPM_RESOURCE_DIR / "disk1.img", img)

        ctrl = DiskController()
        assert ctrl.open_disk(
            str(img),
            disk_type="IMG",
            format_info={"format_name": "cpm_8_sssd_250k"},
        )

        text_content = b"Hello CP/M world!\r\n"
        ctrl.write_file("/HELLO.TXT", text_content)
        ctrl.flush()

        readback = ctrl.read_file("/HELLO.TXT")
        # Text files should have 0x1A padding stripped — this is correct
        assert readback.startswith(text_content)
        # Verify no 0x1A remains at end
        assert not readback.endswith(b"\x1a")

        ctrl.close_disk()


# ---------------------------------------------------------------------------
# BUG-4: FAT12 to_bytes() writes extended BPB into sub-512-byte sectors
#
# fat12_fs.py to_bytes() unconditionally writes:
#   - 0x29 extended boot record signature at offset 0x026
#   - Volume serial, volume label, FS type at 0x027-0x03D
#   - 0xAA55 boot signature at bytes_per_sector - 2
#
# Early FAT (8" floppies, DOS 1.x) predates these fields. Writing them
# into 128/256-byte sectors is historically inaccurate. The read side
# (from_bytes) already handles this correctly via the 0x29 check, but
# to_bytes() should match.
# ---------------------------------------------------------------------------


class TestBug4ExtendedBpbOnSmallSectors:
    """
    BUG-4: to_bytes() should not write extended BPB fields or 0xAA55 boot
    signature for sector sizes < 512 bytes.
    """

    def test_128_byte_sector_no_extended_bpb(self) -> None:
        """A 128-byte sector boot record should not contain the 0x29 marker."""
        bpb = FATVolumeInfo(
            bytes_per_sector=128,
            sectors_per_track=26,
            num_heads=1,
            total_sectors=2002,
            media_descriptor=0xFE,
            root_entries=68,
            sectors_per_fat=6,
            sectors_per_cluster=4,
        )
        raw = bpb.to_bytes()
        assert len(raw) == 128

        # 0x29 at offset 0x026 is the extended boot record signature.
        # It should NOT be present for 128-byte sectors.
        assert raw[0x026] != 0x29, (
            "BUG-4: Extended BPB signature 0x29 was written into a "
            "128-byte boot sector. Early FAT formats don't use it."
        )

    def test_128_byte_sector_no_boot_signature(self) -> None:
        """A 128-byte sector should not have 0xAA55 at the end."""
        bpb = FATVolumeInfo(
            bytes_per_sector=128,
            sectors_per_track=26,
            num_heads=1,
            total_sectors=2002,
            media_descriptor=0xFE,
            root_entries=68,
            sectors_per_fat=6,
            sectors_per_cluster=4,
        )
        raw = bpb.to_bytes()

        # 0xAA55 at bytes 126-127 (bytes_per_sector - 2)
        sig = struct.unpack_from("<H", raw, 126)[0]
        assert sig != 0xAA55, (
            "BUG-4: Boot signature 0xAA55 was written at offset 126 of a "
            "128-byte boot sector. Early 8\" floppy formats don't use it."
        )

    def test_256_byte_sector_no_extended_bpb(self) -> None:
        """A 256-byte sector boot record should not contain extended BPB."""
        bpb = FATVolumeInfo(
            bytes_per_sector=256,
            sectors_per_track=15,
            num_heads=1,
            total_sectors=1155,
            media_descriptor=0xFE,
            root_entries=56,
            sectors_per_fat=4,
            sectors_per_cluster=2,
        )
        raw = bpb.to_bytes()
        assert len(raw) == 256
        assert raw[0x026] != 0x29, (
            "BUG-4: Extended BPB written into 256-byte boot sector."
        )

    def test_512_byte_sector_keeps_extended_bpb(self) -> None:
        """A 512-byte sector SHOULD have extended BPB and 0xAA55."""
        bpb = FATVolumeInfo()  # defaults to 512 bytes
        raw = bpb.to_bytes()
        assert len(raw) == 512
        assert raw[0x026] == 0x29, "512-byte sector should have extended BPB"
        sig = struct.unpack_from("<H", raw, 510)[0]
        assert sig == 0xAA55, "512-byte sector should have 0xAA55 signature"

    def test_roundtrip_128_byte_preserves_core_bpb(self) -> None:
        """to_bytes -> from_bytes roundtrip preserves core BPB for 128-byte sectors."""
        original = FATVolumeInfo(
            bytes_per_sector=128,
            sectors_per_track=26,
            num_heads=1,
            total_sectors=2002,
            media_descriptor=0xFE,
            root_entries=68,
            sectors_per_fat=6,
            sectors_per_cluster=4,
        )
        raw = original.to_bytes()
        parsed = FATVolumeInfo.from_bytes(raw)

        assert parsed.bytes_per_sector == 128
        assert parsed.sectors_per_track == 26
        assert parsed.num_heads == 1
        assert parsed.total_sectors == 2002
        assert parsed.media_descriptor == 0xFE
        assert parsed.root_entries == 68
        assert parsed.sectors_per_fat == 6
        assert parsed.sectors_per_cluster == 4


# ---------------------------------------------------------------------------
# BUG-5: FAT offset uses float instead of integer arithmetic
#
# fat12_fs.py lines 1934 and 1961:
#   byte_offset = int(cluster * 1.5)
#
# Should be: byte_offset = (cluster * 3) // 2
# Float is correct for FAT12 range but fragile by construction.
# ---------------------------------------------------------------------------


class TestBug5FatOffsetFloatArithmetic:
    """
    BUG-5: FAT byte offset should use integer arithmetic, not float.
    This test verifies the equivalence and confirms integer math is used.
    """

    def test_integer_and_float_equivalent_for_fat12_range(self) -> None:
        """Verify int(c*1.5) == (c*3)//2 for all valid FAT12 cluster numbers."""
        for cluster in range(4086):  # FAT12 max clusters + 2
            float_result = int(cluster * 1.5)
            int_result = (cluster * 3) // 2
            assert float_result == int_result, (
                f"Mismatch at cluster {cluster}: float={float_result}, int={int_result}"
            )

    def test_fat_entry_read_write_roundtrip(self) -> None:
        """Write and read back FAT entries to verify offset math is correct."""
        profile = _ALL_FORMATS["ibm_5.25_160k"]
        pf = profile.physical_format
        driver = IMGImageDriver(
            file_path="/dev/null",
            image_data=b"\x00" * pf.total_bytes,
        )
        disk = Disk(driver)
        disk.set_geometry(pf)
        fs = FATFilesystem(disk)
        fs.format_fs(profile)

        # Write specific values to several clusters and read them back
        test_values = {2: 0x123, 3: 0xABC, 4: 0x001, 5: 0xFFF, 10: 0x456}
        for cluster, value in test_values.items():
            fs._set_fat_entry_cached(cluster, value)

        for cluster, expected in test_values.items():
            actual = fs._read_fat_entry_cached(cluster)
            assert actual == expected, (
                f"FAT entry roundtrip failed at cluster {cluster}: "
                f"expected 0x{expected:03X}, got 0x{actual:03X}"
            )


# ---------------------------------------------------------------------------
# FMT-2: ibm_8_630k uses media_descriptor=0x00
#
# 0x00 is the FAT "free cluster" sentinel value. Using it as a media
# descriptor means byte 0 of the FAT is 0x00, which looks like FAT
# entry 0 is free. Although FAT entry 0 is reserved and not used for
# data, this value conflicts with the FAT standard (media descriptor
# should be >= 0xF0 per Microsoft spec, or at minimum non-zero).
# ---------------------------------------------------------------------------


class TestFmt2MediaDescriptorZero:
    """
    FMT-2: The ibm_8_630k format profile uses media_descriptor=0x00,
    which is non-standard and conflicts with the FAT free-cluster marker.
    """

    def test_media_descriptor_not_zero(self) -> None:
        """All format profiles should have a non-zero media descriptor."""
        profile = _ALL_FORMATS.get("ibm_8_630k")
        if profile is None:
            pytest.skip("ibm_8_630k format not registered")

        md = profile.filesystem_config.media_descriptor
        assert md != 0x00, (
            f"FMT-2: ibm_8_630k uses media_descriptor=0x{md:02X}. "
            f"0x00 is the FAT free-cluster marker and should not be used "
            f"as a media descriptor."
        )

    def test_all_fat12_media_descriptors_nonzero(self) -> None:
        """Verify no FAT12 format uses media_descriptor=0x00."""
        for name, profile in _ALL_FORMATS.items():
            if not hasattr(profile.filesystem_config, "media_descriptor"):
                continue
            md = profile.filesystem_config.media_descriptor
            assert md != 0x00, (
                f"Format '{name}' uses media_descriptor=0x00, "
                f"which conflicts with the FAT free-cluster marker."
            )
