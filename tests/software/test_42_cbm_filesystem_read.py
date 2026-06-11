"""CBM filesystem read-path tests."""

import os
import tempfile
from pathlib import Path

import pytest

from fatfloppy.core.cbm_layout import layout_for_variant
from fatfloppy.core.disk import Disk
from fatfloppy.core.drivers.cbm_image import CBMImageDriver
from fatfloppy.core.filesystems.cbm_fs import (
    CBMFilesystem,
    petscii_to_unicode,
    unicode_to_petscii,
)


class TestPetscii:
    def test_basic_round_trip(self):
        raw = b"HELLO WORLD 123"
        assert petscii_to_unicode(raw) == "HELLO WORLD 123"
        assert unicode_to_petscii("HELLO WORLD 123") == raw

    def test_strips_a0_padding_on_decode(self):
        assert petscii_to_unicode(b"GAME\xa0\xa0\xa0\xa0") == "GAME"

    def test_special_glyphs(self):
        assert petscii_to_unicode(b"\x5c\x5e\x5f") == "£↑←"
        assert unicode_to_petscii("£") == b"\x5c"

    def test_shifted_letters_map_to_lowercase(self):
        assert petscii_to_unicode(b"\xc1\xda") == "az"
        assert unicode_to_petscii("az") == b"\xc1\xda"

    def test_unmappable_bytes_escape_round_trip(self):
        s = petscii_to_unicode(b"\x12\x13")
        assert s == "~12~13"
        assert unicode_to_petscii(s) == b"\x12\x13"

    def test_encode_rejects_unmappable_char(self):
        with pytest.raises(ValueError):
            unicode_to_petscii("中")

    def test_truncated_escape_rejected(self):
        with pytest.raises(ValueError):
            unicode_to_petscii("NAME~1")

    def test_escape_rejects_non_hex_digit(self):
        with pytest.raises(ValueError):
            unicode_to_petscii("X~1G")

    def test_escape_rejects_uppercase_hex(self):
        with pytest.raises(ValueError):
            unicode_to_petscii("X~1A")

    def test_every_byte_round_trips(self):
        for b in range(256):
            if b == 0xA0:
                continue  # pad byte is stripped by design
            raw = bytes([b])
            assert unicode_to_petscii(petscii_to_unicode(raw)) == raw


def formatted_d64_bytes(name=b"TEST DISK", disk_id=b"AA") -> bytearray:
    layout = layout_for_variant("D64", 35)
    img = bytearray(layout.total_sectors * 256)
    bam = bytearray(256)
    bam[0], bam[1], bam[2] = 18, 1, 0x41
    for t in range(1, 36):
        spt = layout.spt(t)
        e = 0x04 + 4 * (t - 1)
        if t == 18:
            free, bits = spt - 2, (2**spt - 1) & ~0b11  # 18/0,18/1 allocated
        else:
            free, bits = spt, 2**spt - 1
        bam[e] = free
        bam[e + 1], bam[e + 2], bam[e + 3] = (
            bits & 0xFF,
            (bits >> 8) & 0xFF,
            (bits >> 16) & 0xFF,
        )
    bam[0x90:0xA0] = name.ljust(16, b"\xa0")
    bam[0xA0:0xA2] = b"\xa0\xa0"
    bam[0xA2:0xA4] = disk_id
    bam[0xA4] = 0xA0
    bam[0xA5:0xA7] = b"2A"
    bam[0xA7:0xAB] = b"\xa0" * 4
    off18 = layout.sectors_before(18) * 256
    img[off18 : off18 + 256] = bam
    img[off18 + 256 + 1] = 0xFF  # dir sector 18/1: chain end (0x00, 0xFF)
    return img


def formatted_d71_bytes(name=b"SIDE TWO") -> bytearray:
    layout = layout_for_variant("D71", 70)
    img = bytearray(layout.total_sectors * 256)
    bam = bytearray(256)
    bam[0], bam[1], bam[2] = 18, 1, 0x41
    # Tracks 1-35: 1541-style four-byte entries at 0x04.
    for t in range(1, 36):
        spt = layout.spt(t)
        e = 0x04 + 4 * (t - 1)
        if t == 18:
            free, bits = spt - 2, (2**spt - 1) & ~0b11  # 18/0,18/1 allocated
        else:
            free, bits = spt, 2**spt - 1
        bam[e] = free
        bam[e + 1], bam[e + 2], bam[e + 3] = (
            bits & 0xFF,
            (bits >> 8) & 0xFF,
            (bits >> 16) & 0xFF,
        )
    bam[0x90:0xA0] = name.ljust(16, b"\xa0")
    # Tracks 36-70: free counts at 0xDD + (t - 36); track 53 holds side-2 BAM.
    for t in range(36, 71):
        bam[0xDD + (t - 36)] = 0 if t == 53 else layout.spt(t)
    off18 = layout.sectors_before(18) * 256
    img[off18 : off18 + 256] = bam
    img[off18 + 256 + 1] = 0xFF  # dir sector 18/1: chain end
    # Side-2 bitmaps at 53/0 (offset 0x41000): 3 bytes per track.
    off53 = layout.sectors_before(53) * 256
    assert off53 == 0x41000
    for t in range(36, 71):
        bits = 0 if t == 53 else (2 ** layout.spt(t)) - 1
        o = off53 + 3 * (t - 36)
        img[o], img[o + 1], img[o + 2] = (
            bits & 0xFF,
            (bits >> 8) & 0xFF,
            (bits >> 16) & 0xFF,
        )
    return img


def formatted_d81_bytes(name=b"EIGHTY TRACKS") -> bytearray:
    layout = layout_for_variant("D81", 80)
    img = bytearray(layout.total_sectors * 256)
    off40 = layout.sectors_before(40) * 256
    assert off40 == 0x61800
    hdr = bytearray(256)
    hdr[0], hdr[1], hdr[2] = 40, 3, 0x44
    hdr[0x04:0x14] = name.ljust(16, b"\xa0")
    img[off40 : off40 + 256] = hdr
    # BAM sectors 40/1 (tracks 1-40, offset 0x61900) and 40/2 (41-80, 0x61A00).
    for half, sec in ((0, 1), (1, 2)):
        bam = bytearray(256)
        bam[2], bam[3] = 0x44, 0xBB
        for i in range(40):
            t = half * 40 + i + 1
            e = 0x10 + 6 * i
            if t == 40:
                bam[e] = 36  # 40/0-40/3 allocated (header, BAM x2, dir)
                bam[e + 1 : e + 6] = bytes([0xF0, 0xFF, 0xFF, 0xFF, 0xFF])
            else:
                bam[e] = 40
                bam[e + 1 : e + 6] = b"\xff" * 5
        img[off40 + 256 * sec : off40 + 256 * (sec + 1)] = bam
    return img


def open_fs(img_bytes) -> CBMFilesystem:
    # Temp-dir path: write tests flush via the driver, which persists to
    # file_path -- a relative name would litter the repo root with mem.d64.
    mem_path = Path(tempfile.gettempdir()) / f"fatfloppy-mem-{os.getpid()}.d64"
    drv = CBMImageDriver(str(mem_path), image_data=bytes(img_bytes))
    disk = Disk(drv)
    disk.set_geometry(drv.physical_format)
    return CBMFilesystem(disk)


class TestCBMSkeleton:
    def test_layout_inferred_and_label_read(self):
        fs = open_fs(formatted_d64_bytes())
        assert fs.get_specific_config().variant == "1541"
        assert fs.get_volume_label() == "TEST DISK"

    def test_free_blocks_empty_disk(self):
        fs = open_fs(formatted_d64_bytes())
        free, total = fs.get_free_space()
        assert free == 664 * 254
        assert total == 664 * 254

    def test_bam_verify_counts_detects_mismatch(self):
        img = formatted_d64_bytes()
        img[0x16500 + 0x04] = 5  # claim track 1 has 5 free, bitmap says 21
        fs = open_fs(img)
        fs._initialize()
        assert fs._bam.verify_counts() is False

    def test_allocated_units_empty_disk(self):
        fs = open_fs(formatted_d64_bytes())
        units = fs.get_allocated_units()
        base = 357  # linear index of 18/0
        assert units == [base, base + 1]

    def test_configs_match_delegates(self):
        a = layout_for_variant("D64", 35)
        b = layout_for_variant("D64", 35)
        assert CBMFilesystem.configs_match(a, b)
        assert not CBMFilesystem.configs_match(a, layout_for_variant("D81", 80))
        assert not CBMFilesystem.configs_match(a, object())

    def test_d71_bam_strategy(self):
        fs = open_fs(formatted_d71_bytes())
        assert fs.get_specific_config().variant == "1571"
        free, total = fs.get_free_space()
        assert free == 1328 * 254
        assert total == 1328 * 254
        fs._initialize()
        assert fs._bam.verify_counts() is True

    def test_d81_bam_strategy(self):
        fs = open_fs(formatted_d81_bytes())
        assert fs.get_specific_config().variant == "1581"
        assert fs.get_volume_label() == "EIGHTY TRACKS"
        free, total = fs.get_free_space()
        assert free == 3160 * 254
        assert total == 3160 * 254


def d64_with_file(data: bytes = b"\x01\x08" + bytes(300)) -> bytearray:
    assert len(data) <= 508, "d64_with_file only supports 2-sector chains"
    img = formatted_d64_bytes()
    layout = layout_for_variant("D64", 35)
    # Data chain: 17/0 -> 17/10 (interleave 10), authentic 1541 placement.
    # A zero-byte file still occupies one sector (byte1 = 1, no payload).
    chunks = [data[i : i + 254] for i in range(0, len(data), 254)] or [b""]
    chain = [(17, 0), (17, 10)][: len(chunks)]
    for i, (t, s) in enumerate(chain):
        sec = bytearray(256)
        if i + 1 < len(chain):
            sec[0], sec[1] = chain[i + 1]
            sec[2:256] = chunks[i].ljust(254, b"\x00")
        else:
            sec[0], sec[1] = 0, len(chunks[i]) + 1
            sec[2 : 2 + len(chunks[i])] = chunks[i]
        off = (layout.sectors_before(t) + s) * 256
        img[off : off + 256] = sec
    # Directory entry, slot 0 of 18/1.
    off = (layout.sectors_before(18) + 1) * 256
    img[off + 2] = 0x82  # closed PRG
    img[off + 3], img[off + 4] = 17, 0
    img[off + 5 : off + 0x15] = b"HELLO".ljust(16, b"\xa0")
    img[off + 0x1E] = len(chain)
    # Mark chain allocated so BAM stays consistent.
    bam_off = layout.sectors_before(18) * 256
    e = bam_off + 0x04 + 4 * 16  # track 17
    img[e] = 21 - len(chain)
    bits = (2**21 - 1) & ~sum(1 << s for _t, s in chain)
    img[e + 1], img[e + 2], img[e + 3] = (
        bits & 0xFF,
        (bits >> 8) & 0xFF,
        (bits >> 16) & 0xFF,
    )
    return img


class TestCBMRead:
    def test_list_directory(self):
        fs = open_fs(d64_with_file())
        entries = fs.list_directory("/")
        assert len(entries) == 1
        e = entries[0]
        assert e.name == "HELLO"
        assert e.attributes == "PRG"
        assert not e.is_dir
        assert e.size == 300 + 2

    def test_read_file_exact_bytes(self):
        payload = b"\x01\x08" + bytes(range(256)) + b"END"
        fs = open_fs(d64_with_file(payload))
        assert fs.read_file("/HELLO") == payload

    def test_read_missing_file_raises(self):
        fs = open_fs(d64_with_file())
        with pytest.raises(FileNotFoundError):
            fs.read_file("/NOPE")

    def test_scratched_entries_hidden(self):
        img = d64_with_file()
        layout = layout_for_variant("D64", 35)
        img[(layout.sectors_before(18) + 1) * 256 + 2] = 0x00  # scratch
        fs = open_fs(img)
        assert fs.list_directory("/") == []

    def test_splat_file_listed_with_star(self):
        img = d64_with_file()
        layout = layout_for_variant("D64", 35)
        img[(layout.sectors_before(18) + 1) * 256 + 2] = 0x02  # PRG, not closed
        fs = open_fs(img)
        assert fs.list_directory("/")[0].attributes == "*PRG"

    def test_locked_file_attribute(self):
        img = d64_with_file()
        layout = layout_for_variant("D64", 35)
        img[(layout.sectors_before(18) + 1) * 256 + 2] = 0xC2  # closed+locked PRG
        fs = open_fs(img)
        assert fs.list_directory("/")[0].attributes == "PRG<"

    # Real-world corpus findings (1541 Test/Demo disk's "CBM" USR file, crack
    # intros with garbage links): reads must salvage the valid prefix instead
    # of raising, while write-side chain walking stays strict.

    def test_chain_cycle_read_truncates(self):
        img = d64_with_file()
        layout = layout_for_variant("D64", 35)
        off = (layout.sectors_before(17) + 0) * 256
        img[off], img[off + 1] = 17, 0  # sector links to itself
        fs = open_fs(img)
        # One valid block before the cycle: its full 254-byte payload.
        assert fs.read_file("/HELLO") == b"\x01\x08" + bytes(252)

    def test_chain_out_of_range_read_truncates(self):
        # Same shape as the 1541 Test/Demo disk's famous "CBM" USR file: a
        # raw data block whose first two bytes are content, not a link.
        img = d64_with_file()
        layout = layout_for_variant("D64", 35)
        off = (layout.sectors_before(17) + 0) * 256
        img[off], img[off + 1] = 99, 0  # invalid track
        fs = open_fs(img)
        assert fs.read_file("/HELLO") == b"\x01\x08" + bytes(252)

    def test_corrupt_start_pointer_reads_empty(self):
        # Crack-era entries can point straight off the disk; nothing is
        # recoverable, but read must not raise (the entry still lists).
        img = d64_with_file()
        layout = layout_for_variant("D64", 35)
        off = (layout.sectors_before(18) + 1) * 256
        img[off + 3], img[off + 4] = 75, 1  # entry start track 75: off-disk
        fs = open_fs(img)
        assert fs.list_directory("/")[0].name == "HELLO"
        assert fs.read_file("/HELLO") == b""

    def test_delete_stays_strict_on_corrupt_chain(self):
        # Only the reader is tolerant: scratching a broken chain must still
        # refuse rather than corrupt the BAM.
        img = d64_with_file()
        layout = layout_for_variant("D64", 35)
        off = (layout.sectors_before(17) + 0) * 256
        img[off], img[off + 1] = 99, 0  # invalid track
        fs = open_fs(img)
        with pytest.raises(ValueError):
            fs.delete("/HELLO")

    def test_file_allocation_units(self):
        fs = open_fs(d64_with_file())
        layout = layout_for_variant("D64", 35)
        assert fs.get_file_allocation_units("/HELLO") == [
            layout.linear_index(17, 0),
            layout.linear_index(17, 10),
        ]

    def test_empty_payload_last_sector(self):
        fs = open_fs(d64_with_file(b""))  # zero-byte file: one sector, byte1=1
        assert fs.read_file("/HELLO") == b""
        assert fs.list_directory("/")[0].size == 0

    def test_filename_starting_with_slash(self):
        # CBM names may legally start with '/': only the leading path slash
        # is stripped, so "//X" must resolve to the entry named "/X".
        img = d64_with_file()
        layout = layout_for_variant("D64", 35)
        off = (layout.sectors_before(18) + 1) * 256
        img[off + 5 : off + 0x15] = b"/X".ljust(16, b"\xa0")
        fs = open_fs(img)
        assert fs.read_file("//X") == b"\x01\x08" + bytes(300)


RESOURCES = Path(__file__).parent.parent / "resources" / "CBM"


@pytest.mark.parametrize(
    "image",
    [
        "vic1541_bam.d64",
        "c128_tutorial.d64",
        "endless_forms.d64",
        "cpm_plus_30.d64",
        "1571_demo.d71",
        "1581_demo.d81",
    ],
)
def test_real_image_lists_and_reads(image):
    path = RESOURCES / image
    if not path.exists():
        pytest.skip(f"resource {image} not present")
    drv = CBMImageDriver(str(path))
    disk = Disk(drv)
    disk.set_geometry(drv.physical_format)
    fs = CBMFilesystem(disk)
    if image == "cpm_plus_30.d64":
        # CP/M Plus disk in a D64 container: track 18 holds CP/M data (LINK-80
        # help text), not a CBM directory -- the dir "chain" bytes 111/114 are
        # the ASCII letters 'o'/'r'. A real 1541 errors out here too, so the
        # reader must refuse; format detection will route this disk to CP/M.
        with pytest.raises(ValueError, match="outside disk"):
            fs.list_directory("/")
        return
    entries = fs.list_directory("/")
    assert entries, "expected at least one directory entry"
    for e in entries:
        if e.is_dir or e.attributes.startswith("*"):
            continue
        data = fs.read_file("/" + e.name)
        assert len(data) == e.size


class TestCBMValidity:
    def test_formatted_disk_scores_above_threshold(self):
        fs = open_fs(d64_with_file())
        assert fs.get_validity_score() >= CBMFilesystem.validity_threshold

    def test_zero_disk_scores_zero(self):
        layout = layout_for_variant("D64", 35)
        fs = open_fs(bytearray(layout.total_sectors * 256))
        assert fs.get_validity_score() == 0

    def test_zeroed_header_fields_not_penalized(self):
        # Crack-era disks (corpus: Archon.d64) keep a valid 18/0 link and DOS
        # byte but zero everything else - disk name, ID, BAM bitmap. The
        # directory is fully valid and a real 1541 operates on the disk, so
        # the zeroed name field must not forfeit points: dropping below a
        # heuristic CP/M DPB inference misdetects the disk as CP/M.
        img = d64_with_file()
        base = open_fs(bytearray(img)).get_validity_score()
        layout = layout_for_variant("D64", 35)
        off = layout.sectors_before(18) * 256
        img[off + 3 : off + 256] = bytes(253)  # keep 18/1 link + 'A', zero rest
        fs = open_fs(img)
        assert fs.get_validity_score() == base

    def test_random_disk_scores_below_threshold(self):
        import random

        rng = random.Random(42)
        layout = layout_for_variant("D64", 35)
        img = bytearray(rng.randbytes(layout.total_sectors * 256))
        fs = open_fs(img)
        assert fs.get_validity_score() < CBMFilesystem.validity_threshold

    def test_non_cbm_geometry_scores_zero(self):
        from fatfloppy.core.drivers import IMGImageDriver
        from fatfloppy.core.filesystem_registry import FilesystemRegistry

        res = Path(__file__).parent.parent / "resources" / "empty_formatted_360k.img"
        if not res.exists():
            pytest.skip("empty_formatted_360k.img not present")
        fmt_360 = FilesystemRegistry.get_all_formats()["ibm_5.25_360k"]
        drv = IMGImageDriver(str(res))
        disk = Disk(drv)
        disk.set_geometry(fmt_360.physical_format)
        drv.set_physical_format(fmt_360.physical_format)
        fs = CBMFilesystem(disk)
        assert fs.get_validity_score() == 0

    @pytest.mark.parametrize(
        "image",
        [
            "vic1541_bam.d64",
            "c128_tutorial.d64",
            "endless_forms.d64",
            "1571_demo.d71",
            "1581_demo.d81",
        ],
    )
    def test_real_images_score_above_threshold(self, image):
        path = RESOURCES / image
        if not path.exists():
            pytest.skip(f"resource {image} not present")
        drv = CBMImageDriver(str(path))
        disk = Disk(drv)
        disk.set_geometry(drv.physical_format)
        fs = CBMFilesystem(disk)
        assert fs.get_validity_score() >= CBMFilesystem.validity_threshold

    def test_closed_del_entries_score_full_marks(self):
        # 1571_demo.d71 carries closed-DEL directory entries (type byte 0x80,
        # ftype nibble 0): benign placeholders, invisible to listings, that
        # must not forfeit the +25 directory-entry points.
        path = RESOURCES / "1571_demo.d71"
        if not path.exists():
            pytest.skip("resource not present")
        drv = CBMImageDriver(str(path))
        disk = Disk(drv)
        disk.set_geometry(drv.physical_format)
        fs = CBMFilesystem(disk)
        assert fs.get_validity_score() == 80

    def test_dir_walk_oserror_caps_score(self):
        # An unreadable directory sector means CBM DOS cannot operate on the
        # disk: the score must be capped below the threshold, same as for a
        # structurally invalid chain (ValueError).
        fs = open_fs(d64_with_file())
        fs._initialize()
        real_read = fs._read_ts

        def failing_read(t, s):
            if (t, s) == (18, 1):
                raise OSError("simulated unreadable directory sector")
            return real_read(t, s)

        fs._read_ts = failing_read
        assert fs.get_validity_score() < CBMFilesystem.validity_threshold

    def test_cpm_plus_d64_scores_below_threshold(self):
        # Real CP/M-on-CBM disk: no CBM directory; CBM must not claim it loudly.
        path = RESOURCES / "cpm_plus_30.d64"
        if not path.exists():
            pytest.skip("resource not present")
        drv = CBMImageDriver(str(path))
        disk = Disk(drv)
        disk.set_geometry(drv.physical_format)
        fs = CBMFilesystem(disk)
        assert fs.get_validity_score() < CBMFilesystem.validity_threshold


class TestCBMDisplayAndMap:
    def test_display_info_keys(self):
        fs = open_fs(d64_with_file())
        info = fs.get_display_info()
        assert info["Disk Name"] == "TEST DISK"
        assert info["Variant"] == "1541"
        assert info["Disk ID"] == "AA"
        assert info["DOS Type"] == "2A"
        assert "Blocks Free" in info

    def test_display_info_reports_error_block(self, tmp_path):
        img = bytes(d64_with_file()) + bytes([0x01]) * 683
        p = tmp_path / "err.d64"
        p.write_bytes(img)
        drv = CBMImageDriver(str(p))
        disk = Disk(drv)
        disk.set_geometry(drv.physical_format)
        fs = CBMFilesystem(disk)
        assert fs.get_display_info()["Recorded Sector Errors"] == "0"

    def test_disk_map_layout(self):
        fs = open_fs(d64_with_file())
        m = fs.get_disk_map_layout()
        layout = layout_for_variant("D64", 35)
        get_type = m["get_sector_type"]
        assert get_type(layout.linear_index(18, 0)) == "system"
        assert get_type(layout.linear_index(18, 1)) == "directory"
        assert get_type(layout.linear_index(17, 0)) == "file"
        assert get_type(layout.linear_index(1, 0)) == "free"
        assert m["allocation_unit_size_sectors"] == 1
        assert m["legend"]
        assert m["type_color_map"]


class TestCBM40TrackBam:
    def test_40_track_d64_bam_tolerated(self):
        # Tracks 36-40 have no standard BAM; they read as allocated, count 0,
        # and verify_counts ignores them (read-tolerated, never written).
        layout40 = layout_for_variant("D64", 40)
        img = bytearray(layout40.total_sectors * 256)
        base = formatted_d64_bytes()
        img[: len(base)] = base
        drv = CBMImageDriver("mem.d64", image_data=bytes(img))
        assert drv.physical_format.cylinders == 40
        disk = Disk(drv)
        disk.set_geometry(drv.physical_format)
        fs = CBMFilesystem(disk)
        fs._initialize()
        assert fs.layout.tracks == 40
        assert fs._bam.free_count(36) == 0
        assert not fs._bam.is_free(36, 0)
        assert fs._bam.verify_counts()  # tracks >35 excluded from the check
        free, _total = fs.get_free_space()
        assert free == 664 * 254
        assert fs.get_validity_score() >= CBMFilesystem.validity_threshold
