"""
Edge-case tests for under-tested code paths.

Addresses TEST-3 (non-512-byte sector FAT12), TEST-4 (CP/M 16-bit blocks),
and TEST-6 (critical edge cases: interleave, variable tracks, heads_inverted,
FAT cluster chain boundaries).
"""

import os
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from fatfloppy.core.disk import Disk
from fatfloppy.core.drivers.img import IMGImageDriver
from fatfloppy.core.filesystem_registry import FilesystemRegistry
from fatfloppy.core.filesystems.cpm_fs import (
    CPMDirectoryEntry,
    CPMDiskParameterBlock,
    CPMFilesystem,
)
from fatfloppy.core.filesystems.fat12_fs import FATFilesystem, FATVolumeInfo
from fatfloppy.core.physical_format import PhysicalFormat, TrackFormat

_ALL_FORMATS = FilesystemRegistry.get_all_formats()


# ---------------------------------------------------------------------------
# TEST-3: Non-512-byte sector FAT12 format tests
# ---------------------------------------------------------------------------


class TestFAT12Non512Sectors:
    """Tests for FAT12 on 128-byte and 256-byte sector geometries."""

    def _make_8inch_250k_image(self) -> tuple[Disk, IMGImageDriver, PhysicalFormat]:
        """Create a blank 8\" SSSD 250KB image (77 cyls, 1 head, 26 spt, 128 bps)."""
        profile = _ALL_FORMATS["ibm_8_250k"]
        pf = profile.physical_format
        total_bytes = pf.total_bytes  # 77 * 26 * 128 = 256256
        driver = IMGImageDriver(file_path=os.devnull, image_data=b"\x00" * total_bytes)
        disk = Disk(driver)
        disk.set_geometry(pf)
        return disk, driver, pf

    def test_128_byte_sector_format_and_read_boot(self) -> None:
        """Format a 128-byte sector image and read back BPB fields."""
        disk, driver, pf = self._make_8inch_250k_image()
        profile = _ALL_FORMATS["ibm_8_250k"]

        fs = FATFilesystem(disk)
        fs.format_fs(profile)

        # Read back boot sector
        boot_data = disk.read_sector(0, 0, 0)
        assert len(boot_data) == 128

        bpb = FATVolumeInfo.from_bytes(boot_data)
        assert bpb.bytes_per_sector == 128
        assert bpb.sectors_per_track == 26
        assert bpb.num_heads == 1
        assert bpb.total_sectors == 2002
        assert bpb.sectors_per_cluster == 4

    def test_128_byte_sector_write_read_file(self) -> None:
        """Write and read a file on a 128-byte sector image."""
        disk, driver, pf = self._make_8inch_250k_image()
        profile = _ALL_FORMATS["ibm_8_250k"]

        fs = FATFilesystem(disk)
        fs.format_fs(profile)

        payload = b"Test on 8-inch floppy!" + b"\x00" * 10
        fs.write_file("/TEST.DAT", payload)

        readback = fs.read_file("/TEST.DAT")
        assert readback == payload

    def test_128_byte_sector_free_space(self) -> None:
        """Verify free space accounting on 128-byte sector image."""
        disk, driver, pf = self._make_8inch_250k_image()
        profile = _ALL_FORMATS["ibm_8_250k"]

        fs = FATFilesystem(disk)
        fs.format_fs(profile)

        free_before, total = fs.get_free_space()
        assert total > 0
        assert free_before > 0
        assert free_before <= total

        fs.write_file("/F.TXT", b"x" * 512)  # 512 bytes = 1 cluster (4 * 128)
        free_after, _ = fs.get_free_space()
        assert free_after == free_before - (4 * 128)  # 1 cluster = 4 sectors * 128 bps

    def _make_8inch_298k_image(self) -> tuple[Disk, IMGImageDriver, PhysicalFormat]:
        """Create a blank 8\" 298KB image (77 cyls, 1 head, 15 spt, 256 bps)."""
        profile = _ALL_FORMATS["ibm_8_298k"]
        pf = profile.physical_format
        total_bytes = pf.total_bytes
        driver = IMGImageDriver(file_path=os.devnull, image_data=b"\x00" * total_bytes)
        disk = Disk(driver)
        disk.set_geometry(pf)
        return disk, driver, pf

    def test_256_byte_sector_format_and_write(self) -> None:
        """Format a 256-byte sector image, write a file, read it back."""
        disk, driver, pf = self._make_8inch_298k_image()
        profile = _ALL_FORMATS["ibm_8_298k"]

        fs = FATFilesystem(disk)
        fs.format_fs(profile)

        payload = b"256-byte sectors work!" * 3
        fs.write_file("/S256.TXT", payload)

        readback = fs.read_file("/S256.TXT")
        assert readback == payload

    def test_validity_score_on_128_byte_sector(self) -> None:
        """Validity score should pass threshold on a correctly formatted 128-byte image."""
        disk, driver, pf = self._make_8inch_250k_image()
        profile = _ALL_FORMATS["ibm_8_250k"]

        fs = FATFilesystem(disk)
        fs.format_fs(profile)

        # Re-create filesystem to force fresh scoring
        fs2 = FATFilesystem(disk)
        score = fs2.get_validity_score()
        assert score >= fs2.validity_threshold


# ---------------------------------------------------------------------------
# TEST-4: CP/M 16-bit block numbers (dsm > 255)
# ---------------------------------------------------------------------------


class TestCPM16BitBlocks:
    """Tests for CP/M directory entry parsing/writing with dsm > 255."""

    def _make_dpb_16bit(self) -> CPMDiskParameterBlock:
        """Create a DPB with dsm > 255 to trigger 16-bit block pointers."""
        return CPMDiskParameterBlock(
            spt=52,
            bsh=4,
            blm=15,
            exm=0,
            dsm=500,  # > 255 → 16-bit block pointers
            drm=127,
            al0=0xC0,
            al1=0x00,
            cks=0,
            off=2,
        )

    def _make_dpb_8bit(self) -> CPMDiskParameterBlock:
        """Create a DPB with dsm <= 255 for 8-bit block pointers."""
        return CPMDiskParameterBlock(
            spt=26,
            bsh=3,
            blm=7,
            exm=0,
            dsm=242,
            drm=63,
            al0=0xC0,
            al1=0x00,
            cks=0,
            off=2,
        )

    def test_16bit_entry_roundtrip(self) -> None:
        """Serialize and deserialize a directory entry with 16-bit block pointers."""
        dpb = self._make_dpb_16bit()
        blocks = [256, 300, 400, 500, 0, 0, 0, 0]  # Values > 255

        entry = CPMDirectoryEntry(
            user=0,
            name="BIGDISK",
            ext="COM",
            ex=0,
            s1=0,
            xh=0,
            rc=128,
            blks=blocks,
            attributes_raw={},
        )

        # Create a minimal filesystem mock just for serialization
        pf = PhysicalFormat(
            cylinders=200,
            heads=2,
            rpm=300,
            heads_inverted=False,
            bytes_per_sector=256,
            track_formats=[
                TrackFormat(
                    track_start=0,
                    track_end=199,
                    head_start=0,
                    head_end=1,
                    sectors_per_track=26,
                    encoding="MFM",
                    rate=500,
                )
            ],
        )
        driver = IMGImageDriver(
            file_path=os.devnull,
            image_data=b"\x00" * pf.total_bytes,
        )
        disk = Disk(driver)
        disk.set_geometry(pf)

        fs = CPMFilesystem(disk, config=dpb)

        serialized = fs._format_entry_to_bytes(entry)
        assert len(serialized) == 32

        # Verify 16-bit block pointers at offset 16
        for i, expected_block in enumerate(blocks):
            actual = struct.unpack_from("<H", serialized, 16 + i * 2)[0]
            assert actual == expected_block, (
                f"Block pointer {i}: expected {expected_block}, got {actual}"
            )

        # Deserialize and verify round-trip
        parsed = fs._parse_directory_entry(serialized)
        assert parsed is not None
        assert parsed.name == "BIGDISK"
        assert parsed.ext == "COM"
        assert parsed.blks == blocks

    def test_8bit_entry_roundtrip(self) -> None:
        """Verify 8-bit block pointers when dsm <= 255."""
        dpb = self._make_dpb_8bit()
        blocks = [10, 11, 12, 13, 14, 15, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]

        entry = CPMDirectoryEntry(
            user=0,
            name="SMALL",
            ext="TXT",
            ex=0,
            s1=0,
            xh=0,
            rc=64,
            blks=blocks,
            attributes_raw={},
        )

        pf = PhysicalFormat(
            cylinders=77,
            heads=1,
            rpm=360,
            heads_inverted=False,
            bytes_per_sector=128,
            track_formats=[
                TrackFormat(
                    track_start=0,
                    track_end=76,
                    head_start=0,
                    head_end=0,
                    sectors_per_track=26,
                    encoding="FM",
                    rate=250,
                )
            ],
        )
        driver = IMGImageDriver(
            file_path=os.devnull,
            image_data=b"\x00" * pf.total_bytes,
        )
        disk = Disk(driver)
        disk.set_geometry(pf)

        fs = CPMFilesystem(disk, config=dpb)

        serialized = fs._format_entry_to_bytes(entry)
        assert len(serialized) == 32

        # Verify 8-bit block pointers at offset 16
        for i, expected_block in enumerate(blocks):
            assert serialized[16 + i] == expected_block

        parsed = fs._parse_directory_entry(serialized)
        assert parsed is not None
        assert parsed.name == "SMALL"
        assert parsed.blks == blocks

    def test_16bit_max_block_value(self) -> None:
        """Test that the maximum 16-bit block number (65535) round-trips correctly."""
        dpb = self._make_dpb_16bit()
        dpb.dsm = 65535  # Max 16-bit value
        blocks = [65535, 65534, 1000, 0, 0, 0, 0, 0]

        entry = CPMDirectoryEntry(
            user=3,
            name="MAXBLK",
            ext="DAT",
            ex=0,
            s1=0,
            xh=0,
            rc=128,
            blks=blocks,
            attributes_raw={},
        )

        pf = PhysicalFormat(
            cylinders=200,
            heads=2,
            rpm=300,
            heads_inverted=False,
            bytes_per_sector=512,
            track_formats=[
                TrackFormat(
                    track_start=0,
                    track_end=199,
                    head_start=0,
                    head_end=1,
                    sectors_per_track=26,
                    encoding="MFM",
                    rate=500,
                )
            ],
        )
        driver = IMGImageDriver(
            file_path=os.devnull,
            image_data=b"\x00" * pf.total_bytes,
        )
        disk = Disk(driver)
        disk.set_geometry(pf)

        fs = CPMFilesystem(disk, config=dpb)

        serialized = fs._format_entry_to_bytes(entry)
        parsed = fs._parse_directory_entry(serialized)
        assert parsed is not None
        assert parsed.blks == blocks
        assert parsed.user == 3


# ---------------------------------------------------------------------------
# TEST-6: Critical edge cases
# ---------------------------------------------------------------------------


class TestInterleave:
    """Tests for sector interleave != 1."""

    def test_interleave_2_translation_table(self) -> None:
        """Verify interleave=2 builds correct sector translation table."""
        tf = TrackFormat(
            track_start=0,
            track_end=0,
            head_start=0,
            head_end=0,
            sectors_per_track=8,
            encoding="MFM",
            rate=500,
            interleave=2,
            id_start=1,
        )
        # Interleave 2 with 8 sectors, starting at ID 1:
        # Logical 0→phys 1, L1→phys 3, L2→phys 5, L3→phys 7,
        # L4→phys 2, L5→phys 4, L6→phys 6, L7→phys 8
        expected = [1, 3, 5, 7, 2, 4, 6, 8]
        assert tf.sector_translation_table == expected

    def test_interleave_6_for_8inch(self) -> None:
        """Verify interleave=6 table for 26-sector 8\" floppy."""
        tf = TrackFormat(
            track_start=0,
            track_end=0,
            head_start=0,
            head_end=0,
            sectors_per_track=26,
            encoding="FM",
            rate=250,
            interleave=6,
            id_start=1,
        )
        table = tf.sector_translation_table
        assert len(table) == 26
        # All sector IDs 1-26 must appear exactly once
        assert sorted(table) == list(range(1, 27))
        # First entry is sector ID 1 (logical 0 → physical 1)
        assert table[0] == 1
        # Second entry should be 6 positions later: sector ID 7
        assert table[1] == 7

    def test_chs_to_byte_offset_with_interleave(self) -> None:
        """Verify byte offset calculation respects interleave ordering."""
        pf = PhysicalFormat(
            cylinders=2,
            heads=1,
            rpm=300,
            heads_inverted=False,
            bytes_per_sector=128,
            track_formats=[
                TrackFormat(
                    track_start=0,
                    track_end=1,
                    head_start=0,
                    head_end=0,
                    sectors_per_track=4,
                    encoding="FM",
                    rate=250,
                    interleave=2,
                    id_start=1,
                )
            ],
        )
        # With interleave=2, 4 sectors, id_start=1:
        # Translation table: [1, 3, 2, 4]
        # Logical sector 0 → physical ID 1 → file position (1-1)=0 → offset 0
        # Logical sector 1 → physical ID 3 → file position (3-1)=2 → offset 256
        # Logical sector 2 → physical ID 2 → file position (2-1)=1 → offset 128
        assert pf.chs_to_byte_offset(0, 0, 0) == 0
        assert pf.chs_to_byte_offset(0, 0, 1) == 256
        assert pf.chs_to_byte_offset(0, 0, 2) == 128
        assert pf.chs_to_byte_offset(0, 0, 3) == 384


class TestVariableTrackFormats:
    """Tests for disks with different track formats per cylinder range."""

    def test_imsai_mixed_density_geometry(self) -> None:
        """Verify IMSAI mixed-density format has variable BPS."""
        profile = _ALL_FORMATS.get("cpm_8_ssdd_imsai_mixed")
        if profile is None:
            pytest.skip("cpm_8_ssdd_imsai_mixed format not registered")

        pf = profile.physical_format
        assert len(pf.track_formats) == 2

        # Track 0: FM, 128 bps
        tf0 = pf.get_track_format(0, 0)
        assert tf0.encoding == "FM"
        assert tf0.bytes_per_sector == 128
        assert tf0.sectors_per_track == 26

        # Track 1+: MFM, 256 bps
        tf1 = pf.get_track_format(1, 0)
        assert tf1.encoding == "MFM"
        assert tf1.bytes_per_sector == 256
        assert tf1.sectors_per_track == 26

        assert pf.has_variable_bps is True

    def test_total_bytes_with_variable_bps(self) -> None:
        """Verify total_bytes calculation handles variable sector sizes."""
        pf = PhysicalFormat(
            cylinders=3,
            heads=1,
            rpm=300,
            heads_inverted=False,
            bytes_per_sector=128,
            track_formats=[
                TrackFormat(
                    track_start=0,
                    track_end=0,
                    head_start=0,
                    head_end=0,
                    sectors_per_track=10,
                    bytes_per_sector=128,
                    encoding="FM",
                    rate=250,
                ),
                TrackFormat(
                    track_start=1,
                    track_end=2,
                    head_start=0,
                    head_end=0,
                    sectors_per_track=10,
                    bytes_per_sector=256,
                    encoding="MFM",
                    rate=500,
                ),
            ],
        )
        expected = (10 * 128) + (2 * 10 * 256)  # 1280 + 5120 = 6400
        assert pf.total_bytes == expected


class TestHeadsInverted:
    """Tests for the heads_inverted physical format flag."""

    def test_physical_head_mapping(self) -> None:
        """Verify logical-to-physical head mapping with heads_inverted=True."""
        pf = PhysicalFormat(
            cylinders=2,
            heads=2,
            rpm=300,
            heads_inverted=True,
            bytes_per_sector=512,
            track_formats=[
                TrackFormat(
                    track_start=0,
                    track_end=1,
                    head_start=0,
                    head_end=1,
                    sectors_per_track=9,
                    encoding="MFM",
                    rate=250,
                )
            ],
        )
        # With heads_inverted, logical head 0 → physical head 1
        assert pf.get_physical_head(0) == 1
        assert pf.get_physical_head(1) == 0

    def test_disk_read_uses_physical_head(self) -> None:
        """Verify that Disk.read_sector maps heads when driver uses physical heads."""
        pf = PhysicalFormat(
            cylinders=1,
            heads=2,
            rpm=300,
            heads_inverted=True,
            bytes_per_sector=128,
            track_formats=[
                TrackFormat(
                    track_start=0,
                    track_end=0,
                    head_start=0,
                    head_end=1,
                    sectors_per_track=4,
                    encoding="FM",
                    rate=250,
                )
            ],
        )
        total = pf.total_bytes  # 1 * 2 * 4 * 128 = 1024

        # Place distinct data on each head
        image_data = bytearray(total)
        # Head 0 (physical) gets 0xAA, Head 1 (physical) gets 0xBB
        for i in range(4 * 128):
            image_data[i] = 0xAA  # Cyl 0, Head 0 (physical)
            image_data[4 * 128 + i] = 0xBB  # Cyl 0, Head 1 (physical)

        driver = IMGImageDriver(file_path=os.devnull, image_data=bytes(image_data))
        # IMG driver does NOT use physical heads (uses logical offsets)
        disk = Disk(driver)
        disk.set_geometry(pf)

        # Reading logical head 0 should get data from the first half
        data_h0 = disk.read_sector(0, 0, 0)
        assert data_h0[0] == 0xAA

        # Reading logical head 1 should get data from the second half
        data_h1 = disk.read_sector(0, 1, 0)
        assert data_h1[0] == 0xBB


class TestFATClusterChainEdgeCases:
    """Tests for FAT12 cluster chain boundary conditions."""

    def _make_fat12_image(self) -> tuple[Disk, FATFilesystem]:
        """Create a minimal FAT12 image for cluster chain testing."""
        profile = _ALL_FORMATS["ibm_5.25_160k"]  # Small: 320 sectors, 1 spc
        pf = profile.physical_format
        total_bytes = pf.total_bytes  # 40 * 1 * 8 * 512 = 163840
        driver = IMGImageDriver(file_path=os.devnull, image_data=b"\x00" * total_bytes)
        disk = Disk(driver)
        disk.set_geometry(pf)
        fs = FATFilesystem(disk)
        fs.format_fs(profile)
        return disk, fs

    def test_file_exactly_one_cluster(self) -> None:
        """File that fills exactly 1 cluster (no chain needed)."""
        _, fs = self._make_fat12_image()
        cluster_size = fs.allocation_unit_size  # bytes_per_sector * sectors_per_cluster
        payload = b"\xaa" * cluster_size
        fs.write_file("/EXACT1.BIN", payload)
        readback = fs.read_file("/EXACT1.BIN")
        assert readback == payload

    def test_file_one_byte_over_cluster(self) -> None:
        """File that is 1 byte over a cluster boundary (needs 2 clusters)."""
        _, fs = self._make_fat12_image()
        cluster_size = fs.allocation_unit_size
        payload = b"\xbb" * (cluster_size + 1)
        fs.write_file("/OVER1.BIN", payload)
        readback = fs.read_file("/OVER1.BIN")
        assert readback == payload

    def test_zero_byte_file(self) -> None:
        """Zero-byte file has no cluster chain."""
        _, fs = self._make_fat12_image()
        fs.write_file("/EMPTY.TXT", b"")
        readback = fs.read_file("/EMPTY.TXT")
        assert readback == b""

    def test_many_files_exhaust_clusters(self) -> None:
        """Fill the disk until no free clusters remain, verify error."""
        _, fs = self._make_fat12_image()
        cluster_size = fs.allocation_unit_size
        free_before, _ = fs.get_free_space()

        # Write a single large file that consumes all free clusters.
        # This avoids running into the fixed root directory entry limit.
        fs.write_file("/FILL.BIN", b"\xcc" * free_before)

        free_after, _ = fs.get_free_space()
        assert free_after == 0

        with pytest.raises(OSError):
            fs.write_file("/TOOMUCH.BIN", b"\xdd" * cluster_size)

    def test_delete_frees_clusters_for_reuse(self) -> None:
        """Deleting a file makes its clusters available for new writes."""
        _, fs = self._make_fat12_image()
        cluster_size = fs.allocation_unit_size
        free_before, _ = fs.get_free_space()

        fs.write_file("/REUSE.BIN", b"\xee" * cluster_size * 5)
        free_mid, _ = fs.get_free_space()
        assert free_mid == free_before - 5 * cluster_size

        fs.delete("/REUSE.BIN")
        free_after, _ = fs.get_free_space()
        assert free_after == free_before

        # Now write again into the freed space
        fs.write_file("/NEW.BIN", b"\xff" * cluster_size * 3)
        readback = fs.read_file("/NEW.BIN")
        assert readback == b"\xff" * cluster_size * 3
