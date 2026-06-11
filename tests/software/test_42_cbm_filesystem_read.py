"""CBM filesystem read-path tests."""

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
    drv = CBMImageDriver("mem.d64", image_data=bytes(img_bytes))
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

    def test_validity_score_is_quiet_zero_for_now(self):
        # Real scoring arrives later; for now CBM must never claim a disk.
        fs = open_fs(formatted_d64_bytes())
        assert fs.get_validity_score() == 0

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

    def test_chain_cycle_detected(self):
        img = d64_with_file()
        layout = layout_for_variant("D64", 35)
        off = (layout.sectors_before(17) + 0) * 256
        img[off], img[off + 1] = 17, 0  # sector links to itself
        fs = open_fs(img)
        with pytest.raises(ValueError, match="[Cc]ycle"):
            fs.read_file("/HELLO")

    def test_chain_out_of_range_detected(self):
        img = d64_with_file()
        layout = layout_for_variant("D64", 35)
        off = (layout.sectors_before(17) + 0) * 256
        img[off], img[off + 1] = 99, 0  # invalid track
        fs = open_fs(img)
        with pytest.raises(ValueError):
            fs.read_file("/HELLO")

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
