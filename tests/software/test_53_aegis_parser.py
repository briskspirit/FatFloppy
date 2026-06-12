"""AEGIS filesystem parser tests: labels, VTOC, VTOCEs."""

import logging
from datetime import datetime
from pathlib import Path

import pytest

from fatfloppy.core.apollo_aegis import (
    AegisError,
    parse_lv_label,
    parse_pv_label,
    read_vtoc,
)

RESOURCES = Path(__file__).parent.parent / "resources" / "APOLLO"
DISK5 = RESOURCES / "disk5.img"


@pytest.fixture(scope="module")
def disk5() -> bytes:
    return DISK5.read_bytes()


class TestLabels:
    def test_pv_label(self, disk5):
        pv = parse_pv_label(disk5)
        assert pv.name == "FLPB.SR9"
        assert pv.uid_text == "2A58CC30.A0002FC2"
        assert pv.total_blocks == 0x4D0
        assert pv.lv_daddr == 1
        assert pv.alt_lv_daddr == 0x4CF

    def test_lv_label(self, disk5):
        lv = parse_lv_label(disk5, lv_base=1)
        assert lv.name == "FLPB.SR9"
        assert lv.uid_text == "2A58CCF6.B0002FC2"
        assert lv.label_written.replace(microsecond=0) == datetime(
            1985, 11, 25, 14, 25, 45
        )
        assert lv.bat_daddr == 0x268
        assert lv.bat_first_covered == 0xB
        assert lv.bat_free_count == 217
        assert lv.vtoc_bucket_count == 2
        assert lv.vtoc_total_blocks == 17
        assert lv.root_dir_vtocx == 0x2660
        assert lv.net_root_vtocx == 0x2661
        assert lv.sysboot_vtocx == 0x2670
        assert lv.vtoc_map == [(2, 0x266)]  # (n_blocks, daddr)

    def test_pv_label_rejects_wbak_media(self):
        wbak = (RESOURCES / "disk2.img").read_bytes()
        with pytest.raises(AegisError):
            parse_pv_label(wbak)  # magic at offset 0, not an AEGIS PV label

    def test_pv_label_rejects_garbage(self):
        with pytest.raises(AegisError):
            parse_pv_label(bytes(2048))


class TestVtoc:
    def test_vtoc_walk_finds_80_vtoces(self, disk5):
        vtoc = read_vtoc(disk5, lv_base=1)
        assert len(vtoc.entries) == 80
        assert vtoc.block_count == 17

    def test_bucket_hash_matches_handbook_formula(self, disk5):
        vtoc = read_vtoc(disk5, lv_base=1)
        for vtocx, e in vtoc.entries.items():
            assert vtoc.bucket_of(e.uid) == vtoc.bucket_containing(vtocx)

    def test_vtocx_lookup(self, disk5):
        vtoc = read_vtoc(disk5, lv_base=1)
        root = vtoc.entries[0x2660]
        assert root.kind_is_dir
        sysboot = vtoc.entries[0x2670]
        assert sysboot.length == 10240
        assert sysboot.direct_daddrs[:10] == list(range(1, 0xB))
        assert sysboot.direct_daddrs[10] == 0

    def test_vtoce_fields(self, disk5):
        vtoc = read_vtoc(disk5, lv_base=1)
        e = vtoc.entries[0x2670]  # SYSBOOT
        assert e.type_uid_hi == 0x315
        assert e.blocks_used == 10
        assert isinstance(e.dtm, datetime) and e.dtm.year == 1985
        assert e.uid_text == "2A58CCF7.C0002FC2"
        assert e.parent_uid_text == "2A58CE0A.D0002FC2"

    def test_dtm_dtu_order(self, disk5):
        # VTOCE 0x2664 has distinct timestamps: dtm (modified) > dtu (used).
        # Empirical out_02_vtoc.txt line 55 (02_vtoc.py reads +0x24 as dtm,
        # +0x28 as dtu):
        #   dtm=1985-11-25 14:23:07 UTC   dtu=1985-11-25 14:23:00 UTC
        # With the fields swapped in _parse_vtoce the assertions below fail.
        vtoc = read_vtoc(disk5, lv_base=1)
        e = vtoc.entries[0x2664]
        assert e.dtm.replace(microsecond=0) == datetime(1985, 11, 25, 14, 23, 7), (
            f"dtm should be 14:23:07 (modified); got {e.dtm} -- "
            "likely +0x24/+0x28 are swapped"
        )
        assert e.dtu.replace(microsecond=0) == datetime(1985, 11, 25, 14, 23, 0), (
            f"dtu should be 14:23:00 (used); got {e.dtu}"
        )

    def test_vtoc_chain_warnings_capped(self, caplog):
        # A garbage image whose VTOC has many buckets each with a bad chain
        # must emit at most a small number of warning-level log records.
        import struct

        blocks = 50
        data = bytearray(blocks * 1024)

        # PV label (block 0) -- not needed for read_vtoc, just LV
        data[2:8] = b"APOLLO"
        data[0x34:0x38] = struct.pack(">I", blocks)
        data[0x38:0x3A] = struct.pack(">H", 8)
        data[0x3A:0x3C] = struct.pack(">H", 2)
        data[0x3C:0x40] = struct.pack(">I", 1)

        lv = memoryview(data)[1024:2048]
        struct.pack_into(">H", lv, 0x4C, 0)  # vtoc_version
        struct.pack_into(">H", lv, 0x4E, 20)  # vtoc_bucket_count = 20 buckets
        struct.pack_into(">I", lv, 0x50, 0)
        # vtoc_map: 1 extent of 22 blocks starting at LV daddr 2
        struct.pack_into(">H", lv, 0x64, 22)
        struct.pack_into(">I", lv, 0x66, 2)
        # Each VTOC block (LV daddrs 2..23, abs blocks 3..24) has next-daddr=999
        for i in range(2, 24):
            abs_off = (1 + i) * 1024
            struct.pack_into(">I", data, abs_off, 999)

        with caplog.at_level(logging.WARNING, logger="fatfloppy.core.apollo_aegis"):
            read_vtoc(bytes(data), lv_base=1)

        warn_records = [
            r
            for r in caplog.records
            if r.levelno >= logging.WARNING and r.name == "fatfloppy.core.apollo_aegis"
        ]
        # With 20 buckets each triggering a broken chain, uncapped code emits 20
        # warnings.  Capped code must stay at or below a handful.
        assert len(warn_records) <= 5, (
            f"Expected ≤5 warning records from garbage VTOC, got {len(warn_records)}: "
            + str([r.message for r in warn_records])
        )
