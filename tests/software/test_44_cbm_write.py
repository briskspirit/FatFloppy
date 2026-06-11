"""CBM write-path tests."""

import logging
import tempfile
from pathlib import Path

import pytest

from fatfloppy.core.cbm_layout import layout_for_variant
from fatfloppy.core.disk import Disk
from fatfloppy.core.drivers.cbm_image import CBMImageDriver
from fatfloppy.core.filesystems.cbm_fs import CBMFilesystem
from fatfloppy.core.filesystems.formats.cbm_formats import CBM_FORMATS

from .test_42_cbm_filesystem_read import formatted_d64_bytes, open_fs


def fresh_formatted(profile_name, label="MY DISK,XY"):
    """Blank driver-initialized image of the given profile, then format_fs."""
    p = CBM_FORMATS[profile_name]
    path = Path(tempfile.mkdtemp(prefix="fatfloppy-fmt-")) / "new.img"
    drv = CBMImageDriver(str(path))
    drv.initialize_new_image(p.physical_format)
    disk = Disk(drv)
    disk.set_geometry(drv.physical_format)
    fs = CBMFilesystem(disk)
    fs.format_fs(p, volume_label=label)
    return fs


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

    def test_rel_suffix_validates_record_length(self):
        # REL writing itself is covered in test_45_cbm_rel.
        fs = open_fs(formatted_d64_bytes())
        with pytest.raises(ValueError):
            fs.write_file("/R,r:0", b"")
        with pytest.raises(ValueError):
            fs.write_file("/R,r:255", b"")

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


class TestDeletePreValidation:
    """delete() must validate the whole chain BEFORE any BAM mutation."""

    @staticmethod
    def _fs_with_chain_into_track36():
        """40-track D64 with a file whose chain links into unmapped track 36."""
        layout40 = layout_for_variant("D64", 40)
        img = bytearray(layout40.total_sectors * 256)
        base = formatted_d64_bytes()
        img[: len(base)] = base
        fs = open_fs(bytes(img))
        fs._initialize()
        fs._bam.set_allocated(17, 0)
        fs._bam.flush()
        sec = bytearray(256)
        sec[0], sec[1] = 36, 0  # link into the BAM-less extension area
        fs._write_ts(17, 0, bytes(sec))
        end = bytearray(256)
        end[1] = 0xFF
        fs._write_ts(36, 0, bytes(end))
        d = bytearray(fs._read_ts(18, 1))
        d[2] = 0x82  # closed PRG
        d[3], d[4] = 17, 0
        d[5:0x15] = b"BAD".ljust(16, b"\xa0")
        d[0x1E] = 2
        fs._write_ts(18, 1, bytes(d))
        fs.disk.flush()
        return fs

    def test_chain_into_unmapped_track_leaves_bam_untouched(self):
        fs = self._fs_with_chain_into_track36()
        pre_disk = bytes(fs.disk.driver.read_sector(17, 0, 0))
        pre_cache = bytes(fs._bam._sector(18, 0))
        with pytest.raises(ValueError, match="36"):
            fs.delete("/BAD")
        assert bytes(fs._bam._sector(18, 0)) == pre_cache
        fs._bam.flush()
        fs.disk.flush()
        assert bytes(fs.disk.driver.read_sector(17, 0, 0)) == pre_disk
        assert [e.name for e in fs.list_directory("/")] == ["BAD"]

    def test_chain_with_already_free_block_leaves_bam_untouched(self):
        fs = open_fs(formatted_d64_bytes())
        fs.write_file("/X", bytes(254 * 3))  # chain 17/0 -> 17/10 -> 17/20
        fs._initialize()
        fs._bam.set_free(17, 10)  # simulate a cross-linked/corrupt BAM
        fs._bam.flush()
        fs.disk.flush()
        pre_disk = bytes(fs.disk.driver.read_sector(17, 0, 0))
        pre_cache = bytes(fs._bam._sector(18, 0))
        with pytest.raises(ValueError, match="already free"):
            fs.delete("/X")
        assert bytes(fs._bam._sector(18, 0)) == pre_cache
        fs._bam.flush()
        fs.disk.flush()
        assert bytes(fs.disk.driver.read_sector(17, 0, 0)) == pre_disk
        assert len(fs.list_directory("/")) == 1


class TestReviewHardening:
    def test_non_canonical_escape_rejected(self):
        fs = open_fs(formatted_d64_bytes())
        with pytest.raises(ValueError, match="canonical"):
            fs.write_file("/~41", b"x")  # ~41 is an alias of "A"
        with pytest.raises(ValueError, match="canonical"):
            fs.write_file("/AB~a0", b"x")  # $A0 pad byte inside a name

    def test_canonical_name_still_replaces(self):
        fs = open_fs(formatted_d64_bytes())
        fs.write_file("/A", b"1")
        fs.write_file("/A", b"22")
        assert len(fs.list_directory("/")) == 1
        assert fs.read_file("/A") == b"22"

    def test_empty_file_read_emits_no_warning(self, caplog):
        fs = open_fs(formatted_d64_bytes())
        fs.write_file("/EMPTY", b"")
        with caplog.at_level(logging.WARNING):
            assert fs.read_file("/EMPTY") == b""
        assert not caplog.records

    def test_zero_last_byte_pointer_still_warns(self, caplog):
        fs = open_fs(formatted_d64_bytes())
        fs.write_file("/Z", b"")  # single sector at 17/0, last-byte ptr 1
        sec = bytearray(fs._read_ts(17, 0))
        sec[1] = 0  # impossible pointer: never written by CBM DOS
        fs._write_ts(17, 0, bytes(sec))
        with caplog.at_level(logging.WARNING):
            assert fs.read_file("/Z") == b""
        assert any("valid bytes" in r.message for r in caplog.records)

    def test_rel_reclen_must_be_numeric(self):
        fs = open_fs(formatted_d64_bytes())
        with pytest.raises(ValueError, match="must be a number"):
            fs.write_file("/R,r:abc", b"")

    def test_data_write_failure_rolls_back_allocation(self):
        fs = open_fs(formatted_d64_bytes())
        fs._initialize()
        orig = fs.disk.write_sector

        def failing(cylinder, head, sector, data):
            if cylinder == 16:  # any data write on track 17
                raise OSError("simulated write failure")
            return orig(cylinder, head, sector, data)

        fs.disk.write_sector = failing
        try:
            with pytest.raises(OSError, match="simulated"):
                fs.write_file("/X", bytes(254 * 3))
        finally:
            fs.disk.write_sector = orig
        assert fs._bam.free_blocks() == 664
        assert fs._bam.verify_counts()
        assert fs.list_directory("/") == []


class TestPhase3Hardening:
    """Hardening batch from the phase-3 merge review."""

    def test_midfree_failure_degrades_to_orphans(self):
        """delete() scratches the entry FIRST: a failure mid-free leaves
        orphaned-allocated blocks (warn-only) instead of a live entry pointing
        at freed blocks (data loss)."""
        fs = open_fs(formatted_d64_bytes())
        fs.write_file("/X", bytes(254 * 3))  # chain 17/0 -> 17/10 -> 17/20
        fs._initialize()
        orig = fs._bam.set_free
        calls = {"n": 0}

        def failing(t, s):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("simulated BAM failure")
            return orig(t, s)

        fs._bam.set_free = failing
        try:
            with pytest.raises(OSError, match="simulated"):
                fs.delete("/X")
        finally:
            fs._bam.set_free = orig
        # Entry already scratched: the file is gone from the directory.
        assert fs.list_directory("/") == []
        # First block freed, the rest stay allocated -> orphans, never
        # live-but-free.
        assert fs._bam.is_free(17, 0)
        assert not fs._bam.is_free(17, 10)
        assert not fs._bam.is_free(17, 20)
        assert fs._bam.verify_counts()
        assert fs.check() is True  # orphans warn only

    def test_corrupt_count_set_free_raises_without_mutation(self):
        fs = open_fs(formatted_d64_bytes())
        fs._initialize()
        fs._bam.set_allocated(17, 0)
        buf, c, _bm = fs._bam._entry(17)
        buf[c] = 255  # poke a corrupt free count
        pre = bytes(buf)
        with pytest.raises(ValueError, match="count"):
            fs._bam.set_free(17, 0)
        assert bytes(fs._bam._entry(17)[0]) == pre  # bitmap and count untouched

    def test_corrupt_count_1571_side2_raises_without_mutation(self):
        from .test_42_cbm_filesystem_read import formatted_d71_bytes

        fs = open_fs(formatted_d71_bytes())
        fs._initialize()
        fs._bam.set_allocated(40, 3)
        cnt_buf = fs._bam._sector(18, 0)
        cnt_buf[0xDD + (40 - 36)] = 255  # poke a corrupt side-2 free count
        pre_bm = bytes(fs._bam._sector(53, 0))
        pre_cnt = bytes(cnt_buf)
        with pytest.raises(ValueError, match="count"):
            fs._bam.set_free(40, 3)
        assert bytes(fs._bam._sector(53, 0)) == pre_bm
        assert bytes(fs._bam._sector(18, 0)) == pre_cnt

    def test_check_returns_false_on_read_failure(self):
        from .test_42_cbm_filesystem_read import d64_with_file

        fs = open_fs(d64_with_file())
        assert fs.check()  # consistent fixture passes first
        orig = fs.disk.read_sector

        def failing(cylinder, head, sector):
            if cylinder == 16:  # the file's data track 17
                raise OSError("simulated read failure")
            return orig(cylinder, head, sector)

        fs.disk.read_sector = failing
        try:
            assert fs.check() is False
        finally:
            fs.disk.read_sector = orig

    def test_format_rejects_uniform_non_cbm_geometry(self):
        """A 35x21x256 uniform geometry matches the old shallow gate
        (cylinders + track-0 spt) but is not CBM-zoned: must be rejected."""
        from fatfloppy.core.drivers.img import IMGImageDriver
        from fatfloppy.core.physical_format import PhysicalFormat, TrackFormat

        pf = PhysicalFormat(
            cylinders=35,
            heads=1,
            rpm=300,
            heads_inverted=False,
            bytes_per_sector=256,
            track_formats=[
                TrackFormat(
                    track_start=0,
                    track_end=34,
                    head_start=0,
                    head_end=0,
                    sectors_per_track=21,
                    encoding="MFM",
                    rate=250,
                    interleave=1,
                    bytes_per_sector=256,
                )
            ],
        )
        path = Path(tempfile.mkdtemp(prefix="fatfloppy-uni-")) / "uniform.img"
        drv = IMGImageDriver(str(path))
        drv.initialize_new_image(pf)
        disk = Disk(drv)
        disk.set_geometry(pf)
        fs = CBMFilesystem(disk)
        with pytest.raises(ValueError, match="geometry"):
            fs.format_fs(CBM_FORMATS["cbm_1541_d64"], volume_label="NOPE")


class TestFormat:
    @pytest.mark.parametrize(
        "profile,variant,blocks_free",
        [
            ("cbm_1541_d64", "1541", 664),
            ("cbm_1571_d71", "1571", 1328),
            ("cbm_1581_d81", "1581", 3160),
        ],
    )
    def test_format_blank_image(self, profile, variant, blocks_free):
        fs = fresh_formatted(profile)
        assert fs.get_volume_label() == "MY DISK"
        assert fs.get_display_info()["Disk ID"] == "XY"
        fs._initialize()
        assert fs.layout.variant == variant
        assert fs._bam.free_blocks() == blocks_free
        assert fs._bam.verify_counts()
        assert fs.list_directory("/") == []
        assert fs.get_validity_score() >= 60
        fs.write_file("/T", b"x" * 1000)
        assert fs.read_file("/T") == b"x" * 1000

    def test_format_matches_real_1541_bam_layout(self):
        fs = fresh_formatted("cbm_1541_d64", label="TEST")
        bam = fs.disk.driver.read_sector(17, 0, 0)
        assert bam[0:3] == bytes([18, 1, 0x41])
        assert bam[0x04:0x08] == bytes([21, 0xFF, 0xFF, 0x1F])  # track 1 all free
        assert bam[0x48] == 17  # track 18: 18/0+18/1 allocated
        assert bam[0x90:0xA0] == b"TEST".ljust(16, b"\xa0")
        assert bam[0xA0:0xA2] == b"\xa0\xa0"
        assert bam[0xA2:0xA4] == b"00"  # default ID when label has no comma
        assert bam[0xA4] == 0xA0
        assert bam[0xA5:0xA7] == b"2A"
        assert bam[0xA7:0xAB] == b"\xa0" * 4
        d = fs.disk.driver.read_sector(17, 0, 1)
        assert d[0] == 0 and d[1] == 0xFF

    def test_format_d71_layout(self):
        fs = fresh_formatted("cbm_1571_d71")
        bam = fs.disk.driver.read_sector(17, 0, 0)
        assert bam[3] == 0x80  # double-sided flag
        assert bam[0xDD] == 21  # track 36 free count
        assert bam[0xDD + 17] == 0  # track 53 reserved: count 0
        side = fs.disk.driver.read_sector(52, 0, 0)  # 53/0
        assert side[0:3] == b"\xff\xff\x1f"  # track 36 bitmap all free
        assert side[3 * 17 : 3 * 17 + 3] == b"\x00\x00\x00"  # track 53 all allocated

    def test_format_d81_layout(self):
        fs = fresh_formatted("cbm_1581_d81", label="EIGHTY,GB")
        hdr = fs.disk.driver.read_sector(39, 0, 0)
        assert hdr[0:3] == bytes([40, 3, 0x44])
        assert hdr[0x04:0x14] == b"EIGHTY".ljust(16, b"\xa0")
        assert hdr[0x16:0x18] == b"GB"
        assert hdr[0x19:0x1B] == b"3D"
        bam1 = fs.disk.driver.read_sector(39, 0, 1)
        assert bam1[0:2] == bytes([40, 2])
        assert bam1[2:4] == bytes([0x44, 0xBB])
        assert bam1[4:6] == b"GB"
        assert bam1[6] == 0xC0
        e40 = 0x10 + 6 * 39
        assert bam1[e40] == 36  # track 40: header+2 BAM+dir allocated
        assert bam1[e40 + 1] == 0xF0
        bam2 = fs.disk.driver.read_sector(39, 0, 2)
        assert bam2[0:2] == bytes([0, 0xFF])
        assert bam2[2:4] == bytes([0x44, 0xBB])
        d = fs.disk.driver.read_sector(39, 0, 3)
        assert d[0] == 0 and d[1] == 0xFF

    def test_format_wipes_previous_contents(self):
        fs = open_fs(formatted_d64_bytes())
        fs.write_file("/OLD", b"data")
        fs.format_fs(CBM_FORMATS["cbm_1541_d64"], volume_label="WIPED")
        assert fs.list_directory("/") == []
        assert fs.get_volume_label() == "WIPED"
        fs._initialize()
        assert fs._bam.free_blocks() == 664

    def test_format_label_validation(self):
        fs = open_fs(formatted_d64_bytes())
        with pytest.raises(ValueError):
            fs.format_fs(CBM_FORMATS["cbm_1541_d64"], volume_label="X" * 17)

    def test_format_geometry_mismatch_rejected_before_wipe(self):
        fs = open_fs(formatted_d64_bytes())
        fs.write_file("/KEEP", b"safe")
        for wrong in ("cbm_1581_d81", "cbm_1541_d64_40track"):
            with pytest.raises(ValueError, match="geometry"):
                fs.format_fs(CBM_FORMATS[wrong], volume_label="NOPE")
        # Rejected BEFORE wiping: previous contents fully intact.
        assert fs.get_volume_label() == "TEST DISK"
        assert fs.read_file("/KEEP") == b"safe"

    def test_format_40track_profile(self):
        fs = fresh_formatted("cbm_1541_d64_40track", label="FORTY")
        fs._initialize()
        assert fs.layout.tracks == 40
        assert fs._bam.free_blocks() == 664  # tracks 36-40 unmapped, never written
        assert fs._bam.verify_counts()
        fs.write_file("/T", b"y" * 600)
        assert fs.read_file("/T") == b"y" * 600


class TestChurnAndCheck:
    @pytest.mark.parametrize(
        "profile", ["cbm_1541_d64", "cbm_1571_d71", "cbm_1581_d81"]
    )
    def test_fragmentation_churn(self, profile):
        import random

        rng = random.Random(1234)
        fs = fresh_formatted(profile, label="CHURN")
        live = {}
        for i in range(120):
            name = f"F{i % 30}"
            if name in live and rng.random() < 0.5:
                fs.delete("/" + name)
                del live[name]
            else:
                data = rng.randbytes(rng.randint(0, 2000))
                fs.write_file("/" + name, data)
                live[name] = data
            assert fs._bam.verify_counts(), f"BAM count drift at iteration {i}"
        for name, data in live.items():
            assert fs.read_file("/" + name) == data, f"content mismatch for {name}"
        assert fs.check()

    def test_churn_then_reopen_consistent(self):
        import random

        rng = random.Random(99)
        fs = fresh_formatted("cbm_1541_d64", label="REOPEN")
        live = {}
        for i in range(60):
            name = f"G{i % 12}"
            if name in live and rng.random() < 0.4:
                fs.delete("/" + name)
                del live[name]
            else:
                data = rng.randbytes(rng.randint(1, 1500))
                fs.write_file("/" + name, data)
                live[name] = data
        fs.disk.flush()
        fs2 = CBMFilesystem(fs.disk)
        assert {e.name for e in fs2.list_directory("/")} == set(live)
        for name, data in live.items():
            assert fs2.read_file("/" + name) == data
        assert fs2.check()

    def test_check_detects_bam_chain_mismatch(self):
        from .test_42_cbm_filesystem_read import d64_with_file

        fs = open_fs(d64_with_file())
        assert fs.check()  # consistent fixture passes first
        fs._initialize()
        fs._bam.set_free(17, 0)  # file's first sector now marked free
        fs._bam.flush()
        fs3 = CBMFilesystem(fs.disk)
        assert fs3.check() is False

    def test_check_warns_on_orphaned_allocation(self, caplog):
        # Probe-driven rule: an orphaned-but-allocated block is what a real
        # DOS VALIDATE silently frees -- no data is at risk, so check() warns
        # and still passes (1571_demo legitimately carries two such blocks).
        from .test_42_cbm_filesystem_read import d64_with_file

        fs = open_fs(d64_with_file())
        fs._initialize()
        fs._bam.set_allocated(5, 5)  # allocated but belongs to no file
        fs._bam.flush()
        fs2 = CBMFilesystem(fs.disk)
        with caplog.at_level(logging.WARNING):
            assert fs2.check() is True
        assert any("orphaned" in r.message for r in caplog.records)

    def test_check_fails_on_count_bitmap_mismatch(self):
        from .test_42_cbm_filesystem_read import d64_with_file

        img = d64_with_file()
        img[0x16500 + 0x04] = 5  # track 1 count says 5 free, bitmap says 21
        fs = open_fs(img)
        assert fs.check() is False

    def test_check_fails_on_corrupt_live_chain(self):
        from .test_42_cbm_filesystem_read import d64_with_file

        img = d64_with_file()
        off = (layout_for_variant("D64", 35).sectors_before(17) + 0) * 256
        img[off], img[off + 1] = 99, 0  # live file's chain points off-disk
        fs = open_fs(img)
        assert fs.check() is False

    @pytest.mark.parametrize(
        "image",
        [
            "vic1541_bam.d64",
            "c128_tutorial.d64",
            "endless_forms.d64",
            # 1571_demo carries 2 orphaned all-zero blocks (36/6, 36/7) plus a
            # closed-DEL entry owning a 4-block chain and a REL file: orphans
            # warn (never fail), DEL/REL chains are traced as expected-allocated.
            "1571_demo.d71",
            "1581_demo.d81",  # has a CBM partition (PIC.DIR, tracks 50-59)
        ],
    )
    def test_real_images_pass_check(self, image):
        from .test_42_cbm_filesystem_read import RESOURCES

        path = RESOURCES / image
        if not path.exists():
            pytest.skip(f"resource {image} not present")
        drv = CBMImageDriver(str(path))
        disk = Disk(drv)
        disk.set_geometry(drv.physical_format)
        fs = CBMFilesystem(disk)
        assert fs.check()
