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
