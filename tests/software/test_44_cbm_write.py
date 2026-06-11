"""CBM write-path tests."""

import pytest

from fatfloppy.core.cbm_layout import layout_for_variant
from fatfloppy.core.filesystems.cbm_fs import CBMFilesystem

from .test_42_cbm_filesystem_read import formatted_d64_bytes, open_fs


class TestBamWrite:
    def test_allocate_and_free_round_trip(self):
        fs = open_fs(formatted_d64_bytes())
        fs._initialize()
        assert fs._bam.is_free(17, 0)
        fs._bam.set_allocated(17, 0)
        assert not fs._bam.is_free(17, 0)
        assert fs._bam.free_count(17) == 20
        fs._bam.set_free(17, 0)
        assert fs._bam.is_free(17, 0)
        assert fs._bam.free_count(17) == 21
        assert fs._bam.verify_counts()

    def test_double_allocate_raises(self):
        fs = open_fs(formatted_d64_bytes())
        fs._initialize()
        fs._bam.set_allocated(17, 0)
        with pytest.raises(ValueError):
            fs._bam.set_allocated(17, 0)

    def test_double_free_raises(self):
        fs = open_fs(formatted_d64_bytes())
        fs._initialize()
        with pytest.raises(ValueError):
            fs._bam.set_free(17, 0)  # already free

    def test_bam_flush_persists(self):
        fs = open_fs(formatted_d64_bytes())
        fs._initialize()
        fs._bam.set_allocated(17, 5)
        fs._bam.flush()
        fs2 = CBMFilesystem(fs.disk)
        fs2._initialize()
        assert not fs2._bam.is_free(17, 5)

    def test_data_write_does_not_discard_pending_bam_mutations(self):
        fs = open_fs(formatted_d64_bytes())
        fs._initialize()
        fs._bam.set_allocated(17, 5)  # pending in cache
        fs._write_ts(17, 5, bytes(256))  # data write elsewhere
        assert not fs._bam.is_free(17, 5)  # mutation survives
        fs._bam.flush()
        fs2 = CBMFilesystem(fs.disk)
        fs2._initialize()
        assert not fs2._bam.is_free(17, 5)

    def test_1571_set_allocated_side2(self):
        from .test_42_cbm_filesystem_read import formatted_d71_bytes

        fs = open_fs(formatted_d71_bytes())
        fs._initialize()
        assert fs._bam.is_free(40, 3)
        fs._bam.set_allocated(40, 3)
        assert not fs._bam.is_free(40, 3)
        assert fs._bam.free_count(40) == 20  # track 40 has 21 spt
        fs._bam.set_free(40, 3)
        assert fs._bam.verify_counts()

    def test_1581_set_allocated_high_track(self):
        from .test_42_cbm_filesystem_read import formatted_d81_bytes

        fs = open_fs(formatted_d81_bytes())
        fs._initialize()
        fs._bam.set_allocated(41, 0)
        assert not fs._bam.is_free(41, 0)
        assert fs._bam.free_count(41) == 39
        fs._bam.flush()
        fs2 = CBMFilesystem(fs.disk)
        fs2._initialize()
        assert not fs2._bam.is_free(41, 0)

    def test_1541_track36_plus_rejects_mutation(self):
        layout40 = layout_for_variant("D64", 40)
        img = bytearray(layout40.total_sectors * 256)
        base = formatted_d64_bytes()
        img[: len(base)] = base
        fs = open_fs(bytes(img))
        fs._initialize()
        with pytest.raises(ValueError):
            fs._bam.set_allocated(36, 0)  # unmapped extension tracks: never write


class TestAllocator:
    def test_first_file_starts_near_directory(self):
        fs = open_fs(formatted_d64_bytes())
        fs._initialize()
        t, s = fs._allocate_first_sector()
        assert (t, s) == (17, 0)  # nearest track below 18, sector 0

    def test_interleave_10_within_track(self):
        fs = open_fs(formatted_d64_bytes())
        fs._initialize()
        t1, s1 = fs._allocate_first_sector()
        t2, s2 = fs._allocate_next_sector(t1, s1)
        assert (t2, s2) == (17, 10)
        t3, s3 = fs._allocate_next_sector(t2, s2)
        assert (t3, s3) == (17, 20)
        t4, s4 = fs._allocate_next_sector(t3, s3)
        assert (t4, s4) == (17, 9)  # (20+10)%21=9 free -> taken

    def test_collision_advances_forward(self):
        fs = open_fs(formatted_d64_bytes())
        fs._initialize()
        fs._bam.set_allocated(17, 10)  # occupy the interleave target
        t1, s1 = fs._allocate_first_sector()  # 17/0
        t2, s2 = fs._allocate_next_sector(t1, s1)
        assert (t2, s2) == (17, 11)  # forward scan from the collision

    def test_track_full_moves_outward(self):
        fs = open_fs(formatted_d64_bytes())
        fs._initialize()
        for s in range(21):
            if fs._bam.is_free(17, s):
                fs._bam.set_allocated(17, s)
        t, s = fs._allocate_first_sector()
        assert t == 19  # next nearest: 17 full -> 19 (above), per search order

    def test_directory_track_never_allocated_for_files(self):
        fs = open_fs(formatted_d64_bytes())
        fs._initialize()
        allocated = set()
        t, s = fs._allocate_first_sector()
        allocated.add((t, s))
        with pytest.raises(OSError):
            while True:
                t, s = fs._allocate_next_sector(t, s)
                allocated.add((t, s))
        assert not any(t == 18 for t, _s in allocated)
        assert len(allocated) == 664

    def test_d81_linear_allocation(self):
        from .test_42_cbm_filesystem_read import formatted_d81_bytes

        fs = open_fs(formatted_d81_bytes())
        fs._initialize()
        t1, s1 = fs._allocate_first_sector()
        assert (t1, s1) == (1, 0)
        t2, s2 = fs._allocate_next_sector(t1, s1)
        assert (t2, s2) == (1, 1)  # interleave 1


class TestWriteFile:
    def test_write_read_round_trip(self):
        fs = open_fs(formatted_d64_bytes())
        data = b"\x01\x08" + bytes(range(256)) * 3
        fs.write_file("/MYPROG", data)
        assert fs.read_file("/MYPROG") == data
        e = fs.list_directory("/")[0]
        assert e.name == "MYPROG" and e.attributes == "PRG"
        assert e.size == len(data)
        fs._initialize()
        assert fs._bam.verify_counts()

    def test_write_persists_through_reopen(self):
        fs = open_fs(formatted_d64_bytes())
        fs.write_file("/KEEP", b"persist me")
        fs.disk.flush()
        fs2 = CBMFilesystem(fs.disk)
        assert fs2.read_file("/KEEP") == b"persist me"

    def test_type_suffixes(self):
        fs = open_fs(formatted_d64_bytes())
        fs.write_file("/NOTES,s", b"hello")
        fs.write_file("/RAW,u", b"x")
        fs.write_file("/CODE,p", b"y")
        types = {e.name: e.attributes for e in fs.list_directory("/")}
        assert types == {"NOTES": "SEQ", "RAW": "USR", "CODE": "PRG"}

    def test_rel_suffix_validates_then_defers(self):
        fs = open_fs(formatted_d64_bytes())
        with pytest.raises(ValueError):
            fs.write_file("/R,r:0", b"")
        with pytest.raises(ValueError):
            fs.write_file("/R,r:255", b"")
        with pytest.raises(NotImplementedError):
            fs.write_file("/R,r:100", b"x" * 100)

    def test_comma_in_name_not_a_type_suffix(self):
        fs = open_fs(formatted_d64_bytes())
        fs.write_file("/A,B", b"d")
        assert fs.list_directory("/")[0].name == "A,B"

    def test_overwrite_replaces_and_frees(self):
        fs = open_fs(formatted_d64_bytes())
        fs.write_file("/A", bytes(254 * 5))
        fs._initialize()
        free1 = fs._bam.free_blocks()
        fs.write_file("/A", bytes(10))
        assert fs.read_file("/A") == bytes(10)
        assert fs._bam.free_blocks() == free1 + 4
        assert len(fs.list_directory("/")) == 1

    def test_empty_file(self):
        fs = open_fs(formatted_d64_bytes())
        fs.write_file("/EMPTY", b"")
        assert fs.read_file("/EMPTY") == b""
        assert fs.list_directory("/")[0].size == 0

    def test_disk_full_rolls_back_cleanly(self):
        fs = open_fs(formatted_d64_bytes())
        with pytest.raises(OSError, match="full"):
            fs.write_file("/BIG", bytes(254 * 700))
        fs._initialize()
        assert fs._bam.free_blocks() == 664
        assert fs.list_directory("/") == []
        assert fs._bam.verify_counts()

    def test_name_validation(self):
        fs = open_fs(formatted_d64_bytes())
        with pytest.raises(ValueError):
            fs.write_file("/" + "X" * 17, b"data")
        with pytest.raises(ValueError):
            fs.write_file("/", b"data")
        with pytest.raises(ValueError):
            fs.write_file("/中", b"data")

    def test_directory_extends_beyond_8_entries(self):
        fs = open_fs(formatted_d64_bytes())
        for i in range(10):
            fs.write_file(f"/FILE{i}", b"x")
        names = {e.name for e in fs.list_directory("/")}
        assert names == {f"FILE{i}" for i in range(10)}
        # Directory now spans 2 sectors on track 18, chained with interleave 3.
        fs._initialize()
        secs = [(t, s) for t, s, _ in fs._iter_dir_sectors()]
        assert secs[0] == (18, 1)
        assert len(secs) == 2
        assert secs[1][0] == 18
        # The extension sector's set_allocated lives in the (18,0) BAM cache
        # buffer; the _write_ts of the new dir sector pops only its own key,
        # so the allocation must survive and persist through reopen.
        fs.disk.flush()
        fs2 = CBMFilesystem(fs.disk)
        fs2._initialize()
        assert not fs2._bam.is_free(18, secs[1][1])
        assert fs2._bam.verify_counts()

    def test_scratched_slot_reused(self):
        fs = open_fs(formatted_d64_bytes())
        for i in range(8):
            fs.write_file(f"/F{i}", b"x")
        fs.delete("/F3")
        fs.write_file("/NEW", b"y")
        fs._initialize()
        secs = [(t, s) for t, s, _ in fs._iter_dir_sectors()]
        assert len(secs) == 1  # reused the scratched slot, no extension

    def test_dir_entry_cap_enforced(self):
        fs = open_fs(formatted_d64_bytes())
        fs._initialize()
        fs.layout.max_dir_entries = 2  # simulate, avoid writing 144 files
        fs.write_file("/A", b"1")
        fs.write_file("/B", b"2")
        with pytest.raises(OSError, match="[Dd]irectory full"):
            fs.write_file("/C", b"3")
        # Rollback: C's chain freed.
        assert fs._bam.verify_counts()
        assert len(fs.list_directory("/")) == 2

    def test_write_allocates_with_dos_interleave(self):
        fs = open_fs(formatted_d64_bytes())
        fs.write_file("/X", bytes(254 * 3))
        assert fs.get_file_allocation_units("/X") == [
            layout_for_variant("D64", 35).linear_index(17, 0),
            layout_for_variant("D64", 35).linear_index(17, 10),
            layout_for_variant("D64", 35).linear_index(17, 20),
        ]


class TestDelete:
    def test_delete_frees_chain_and_scratches(self):
        fs = open_fs(formatted_d64_bytes())
        fs.write_file("/DOOMED", bytes(254 * 3))
        fs.delete("/DOOMED")
        assert fs.list_directory("/") == []
        fs._initialize()
        assert fs._bam.free_blocks() == 664
        assert fs._bam.verify_counts()
        with pytest.raises(FileNotFoundError):
            fs.read_file("/DOOMED")

    def test_delete_missing_raises(self):
        fs = open_fs(formatted_d64_bytes())
        with pytest.raises(FileNotFoundError):
            fs.delete("/NOPE")

    def test_delete_recursive_contract(self):
        fs = open_fs(formatted_d64_bytes())
        fs.write_file("/X", b"1")
        assert fs.delete_recursive("/X") is True
        assert fs.delete_recursive("/X") is False

    def test_delete_persists_through_reopen(self):
        fs = open_fs(formatted_d64_bytes())
        fs.write_file("/X", b"1")
        fs.delete("/X")
        fs.disk.flush()
        fs2 = CBMFilesystem(fs.disk)
        assert fs2.list_directory("/") == []
