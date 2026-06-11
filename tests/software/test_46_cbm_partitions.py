"""D81 CBM partition / sub-directory tests."""

import pytest

from fatfloppy.core.disk import Disk
from fatfloppy.core.drivers.cbm_image import CBMImageDriver
from fatfloppy.core.filesystems.cbm_fs import CBMFilesystem

from .test_42_cbm_filesystem_read import RESOURCES
from .test_44_cbm_write import fresh_formatted


class TestPartitionCreate:
    def test_create_default_size(self):
        fs = fresh_formatted("cbm_1581_d81", label="PARTS")
        fs.create_directory("/SUB")
        entries = fs.list_directory("/")
        assert len(entries) == 1
        e = entries[0]
        assert e.is_dir and e.name == "SUB"
        assert e.size == 0
        assert e.extra_data["sectors"] == 120

    def test_create_custom_size(self):
        fs = fresh_formatted("cbm_1581_d81")
        fs.create_directory("/BIG,240")
        assert fs.list_directory("/")[0].extra_data["sectors"] == 240

    def test_create_rejects_bad_sizes(self):
        fs = fresh_formatted("cbm_1581_d81")
        with pytest.raises(ValueError):
            fs.create_directory("/BAD,100")  # not a multiple of 40
        with pytest.raises(ValueError):
            fs.create_directory("/BAD,80")  # below the 120-sector minimum

    def test_create_on_d64_raises(self):
        fs = fresh_formatted("cbm_1541_d64")
        with pytest.raises(NotImplementedError):
            fs.create_directory("/SUB")

    def test_create_rejects_existing_name(self):
        fs = fresh_formatted("cbm_1581_d81")
        fs.write_file("/TAKEN", b"x")
        with pytest.raises(FileExistsError):
            fs.create_directory("/TAKEN")

    def test_create_no_contiguous_region(self):
        fs = fresh_formatted("cbm_1581_d81")
        # 39 + 40 tracks: consumes every data track on both sides of 40.
        fs.create_directory("/A,1560")
        fs.create_directory("/B,1600")
        with pytest.raises(OSError, match="contiguous"):
            fs.create_directory("/C,120")

    def test_partition_region_never_straddles_track_40(self):
        fs = fresh_formatted("cbm_1581_d81")
        # consume tracks 1..37 with a filler partition (37 tracks)
        fs.create_directory("/FILL,1480")
        fs.create_directory("/NEXT,160")  # needs 4 tracks: must land at 41+
        e = {x.name: x for x in fs.list_directory("/")}["NEXT"]
        assert e.extra_data["first_ts"][0] >= 41

    def test_subdir_bam_marks_outside_allocated(self):
        fs = fresh_formatted("cbm_1581_d81")
        fs.create_directory("/SUB")  # lands at track 1 (first fit)
        start = fs.list_directory("/")[0].extra_data["first_ts"][0]
        bam1 = bytes(fs._read_ts(start, 1))
        assert bam1[2:4] == bytes([0x44, 0xBB])
        e_inside = 0x10 + 6 * ((start + 1 - 1) % 40)  # 2nd partition track
        assert bam1[e_inside] == 40
        e_outside = 0x10 + 6 * ((start + 3 + 1 - 1) % 40)  # first track past it
        assert bam1[e_outside] == 0

    def test_subdir_structures_on_disk(self):
        fs = fresh_formatted("cbm_1581_d81", label="ROOT,RI")
        fs.create_directory("/SUB")
        start = fs.list_directory("/")[0].extra_data["first_ts"][0]
        hdr = bytes(fs._read_ts(start, 0))
        assert hdr[0], hdr[1] == (start, 3)  # header points at the dir
        assert hdr[2] == 0x44
        assert hdr[0x04:0x14] == b"SUB" + b"\xa0" * 13
        assert hdr[0x16:0x18] == b"RI"  # root disk ID reused
        assert hdr[0x19:0x1B] == b"3D"
        # interior of start track: 0..3 allocated, 4..39 free (36 free)
        bam1 = bytes(fs._read_ts(start, 1))
        e = 0x10 + 6 * ((start - 1) % 40)
        assert bam1[e] == 36
        assert bam1[e + 1] == 0xF0
        # dir sector: chain end with 8 fresh slots
        d = bytes(fs._read_ts(start, 3))
        assert d[0] == 0 and d[1] == 0xFF

    def test_create_allocates_range_in_root_bam(self):
        fs = fresh_formatted("cbm_1581_d81")
        fs._initialize()
        free0 = fs._bam.free_blocks()
        fs.create_directory("/SUB")
        assert fs._bam.free_blocks() == free0 - 120
        assert fs._bam.verify_counts()


class TestPartitionTraversal:
    def test_write_read_inside_partition(self):
        fs = fresh_formatted("cbm_1581_d81")
        fs.create_directory("/SUB")
        fs.write_file("/SUB/HELLO", b"data inside partition")
        assert fs.read_file("/SUB/HELLO") == b"data inside partition"
        assert [e.name for e in fs.list_directory("/SUB")] == ["HELLO"]
        assert [e.name for e in fs.list_directory("/")] == ["SUB"]

    def test_partition_files_isolated_from_root_bam(self):
        fs = fresh_formatted("cbm_1581_d81")
        fs.create_directory("/SUB")
        fs._initialize()
        free_after_create = fs._bam.free_blocks()
        fs.write_file("/SUB/F", bytes(254 * 5))
        assert fs._bam.free_blocks() == free_after_create  # root untouched

    def test_partition_file_allocation_units_inside_range(self):
        fs = fresh_formatted("cbm_1581_d81")
        fs.create_directory("/SUB")
        start = fs.list_directory("/")[0].extra_data["first_ts"][0]
        fs.write_file("/SUB/F", bytes(254 * 5))
        units = fs.get_file_allocation_units("/SUB/F")
        assert units
        lo = sum(fs.layout.spt(t) for t in range(1, start))
        hi = lo + 120
        assert all(lo <= u < hi for u in units)

    def test_partition_delete_file_and_overwrite(self):
        fs = fresh_formatted("cbm_1581_d81")
        fs.create_directory("/SUB")
        fs.write_file("/SUB/F", b"v1")
        fs.write_file("/SUB/F", b"version2")
        assert fs.read_file("/SUB/F") == b"version2"
        fs.delete("/SUB/F")
        assert fs.list_directory("/SUB") == []

    def test_nested_partition_paths_rejected(self):
        fs = fresh_formatted("cbm_1581_d81")
        fs.create_directory("/SUB")
        with pytest.raises(FileNotFoundError):
            fs.read_file("/SUB/X/Y")

    def test_non_subdir_partition_not_a_directory(self):
        fs = fresh_formatted("cbm_1581_d81")
        fs.create_directory("/SUB")
        start = fs.list_directory("/")[0].extra_data["first_ts"][0]
        fs._write_ts(start, 0, bytes(256))  # corrupt the partition header
        e = fs.list_directory("/")[0]
        assert not e.is_dir
        raw = fs.read_file("/SUB")
        assert len(raw) == 120 * 256
        with pytest.raises(NotADirectoryError):
            fs.list_directory("/SUB")
        with pytest.raises(NotADirectoryError):
            fs.read_file("/SUB/F")

    def test_slash_in_root_filename_still_works(self):
        # CBM names may legally contain '/'; only partition prefixes split.
        fs = fresh_formatted("cbm_1581_d81")
        fs.write_file("/A/B", b"slashed name")
        assert fs.read_file("/A/B") == b"slashed name"
        assert [e.name for e in fs.list_directory("/")] == ["A/B"]

    def test_writes_persist_through_reopen(self):
        fs = fresh_formatted("cbm_1581_d81")
        fs.create_directory("/SUB")
        fs.write_file("/SUB/KEEP", b"persistent")
        fs.disk.flush()
        fs2 = CBMFilesystem(fs.disk)
        assert fs2.read_file("/SUB/KEEP") == b"persistent"
        assert fs2.check()


class TestPartitionDelete:
    def test_delete_empty_partition(self):
        fs = fresh_formatted("cbm_1581_d81")
        fs._initialize()
        free0 = fs._bam.free_blocks()
        fs.create_directory("/SUB")
        fs.delete("/SUB")
        assert fs.list_directory("/") == []
        assert fs._bam.free_blocks() == free0
        assert fs._bam.verify_counts()

    def test_delete_nonempty_requires_recursive(self):
        fs = fresh_formatted("cbm_1581_d81")
        fs.create_directory("/SUB")
        fs.write_file("/SUB/F", b"x")
        with pytest.raises(OSError, match="not empty"):
            fs.delete("/SUB")
        assert fs.delete_recursive("/SUB") is True
        assert fs.list_directory("/") == []

    def test_delete_raw_partition_no_emptiness_check(self):
        fs = fresh_formatted("cbm_1581_d81")
        fs._initialize()
        free0 = fs._bam.free_blocks()
        fs.create_directory("/SUB")
        fs.write_file("/SUB/F", b"x")
        fs._write_ts(1, 0, bytes(256))  # corrupt header: raw partition now
        fs.delete("/SUB")  # no emptiness check for raw partitions
        assert fs.list_directory("/") == []
        assert fs._bam.free_blocks() == free0

    def test_file_write_cannot_overwrite_partition(self):
        fs = fresh_formatted("cbm_1581_d81")
        fs.create_directory("/SUB")
        with pytest.raises(ValueError, match="[Pp]artition"):
            fs.write_file("/SUB", b"clobber")
        with pytest.raises(ValueError, match="[Pp]artition"):
            fs.write_file("/SUB,r:100", b"clobber")
        assert fs.list_directory("/")[0].is_dir  # partition survived intact


class TestCreateDirectoryGuards:
    """Fix 1: create_directory must reject nested / slash paths."""

    def test_nested_create_raises_not_implemented(self):
        # create /SUB first, then try to create /SUB/NEW — must raise
        fs = fresh_formatted("cbm_1581_d81")
        fs.create_directory("/SUB")
        with pytest.raises(NotImplementedError, match="[Nn]ested"):
            fs.create_directory("/SUB/NEW")

    def test_slash_in_path_with_no_matching_partition_raises_value_error(self):
        # /A/B where A is not an existing partition — must raise ValueError
        fs = fresh_formatted("cbm_1581_d81")
        with pytest.raises(ValueError, match="/"):
            fs.create_directory("/A/B")

    def test_plain_root_create_still_works(self):
        # sanity: plain create with no slash still succeeds
        fs = fresh_formatted("cbm_1581_d81")
        fs.create_directory("/OK")
        assert fs.list_directory("/")[0].name == "OK"


class TestCreateDirectoryRollback:
    """Fix 3: create_directory BAM allocation loop must roll back on ValueError."""

    def test_bam_inconsistency_mid_loop_leaves_state_unchanged(self):
        fs = fresh_formatted("cbm_1581_d81")
        fs._initialize()
        # Find the track range that would be used (first-fit: tracks 1..3)
        # Corrupt mid-range: manually clear sector 5's bit in track 2's BAM entry
        # while leaving the count at 40 (says free but bitmap disagrees).
        # Strategy: set_allocated track 2 / sector 5 via the low-level cache
        # to create the inconsistency, then call create_directory — it must
        # roll back and leave free_blocks unchanged.
        start_track = 2
        bam_sector = bytearray(fs._read_ts(40, 1))  # 1581 BAM sector 1 covers 1-40
        e = 0x10 + 6 * ((start_track - 1) % 40)
        # Count byte stays 40 (says fully free) but clear bit 5 in bitmap byte 0
        bam_sector[e + 1] &= ~(1 << 5)  # clear bit 5 of sector 5's byte
        fs._write_ts(40, 1, bytes(bam_sector))
        # Make the BAM strategy re-read by clearing its cache
        fs._bam.invalidate()

        # Capture BAM sector bytes BEFORE create attempt
        bam_before = bytes(fs._read_ts(40, 1))
        free_before = fs._bam.free_blocks()

        with pytest.raises(ValueError):
            fs.create_directory("/SUB")

        # BAM must be exactly as before (no tracks left allocated)
        fs._bam.invalidate()
        bam_after = bytes(fs._read_ts(40, 1))
        assert bam_after == bam_before, "BAM was mutated and not rolled back"
        assert fs._bam.free_blocks() == free_before


class TestPartitionCheckAndReal:
    def test_check_with_partitions_and_files(self):
        fs = fresh_formatted("cbm_1581_d81")
        fs.create_directory("/SUB")
        fs.write_file("/SUB/F", b"x" * 1000)
        fs.write_file("/ROOT", b"y" * 1000)
        assert fs.check()

    def test_check_fails_on_corrupt_subdir_bam(self):
        fs = fresh_formatted("cbm_1581_d81")
        fs.create_directory("/SUB")
        fs.write_file("/SUB/F", b"x" * 1000)
        start = fs.list_directory("/")[0].extra_data["first_ts"][0]
        bam = bytearray(fs._read_ts(start, 1))
        bam[0x10] = 39  # corrupt a free count inside the partition
        fs._write_ts(start, 1, bytes(bam))
        assert fs.check() is False

    def test_real_1581_demo_partition(self):
        path = RESOURCES / "1581_demo.d81"
        if not path.exists():
            pytest.skip("resource not present")
        drv = CBMImageDriver(str(path))
        disk = Disk(drv)
        disk.set_geometry(drv.physical_format)
        fs = CBMFilesystem(disk)
        pic = next(e for e in fs.list_directory("/") if e.name == "PIC.DIR")
        # Probed: PIC.DIR (400 sectors, tracks 50-59) IS a formatted
        # sub-directory: header at 50/0 (0x44, "3D"), BAM at 50/1+50/2
        # (0x44/0xBB, out-of-partition tracks count 0), dir chain 50/3-50/5.
        assert pic.is_dir
        assert pic.extra_data["sectors"] == 400
        assert pic.extra_data["first_ts"] == (50, 0)
        inner = fs.list_directory("/PIC.DIR")
        assert len(inner) == 17
        names = [e.name for e in inner]
        assert names[0] == "SUE.C" and names[-1] == ".LOADER"
        assert all(e.attributes == "PRG" for e in inner)
        for e in inner:
            data = fs.read_file(f"/PIC.DIR/{e.name}")
            assert len(data) == e.size
            assert data  # every file inside is readable and non-empty
        assert fs.check()
