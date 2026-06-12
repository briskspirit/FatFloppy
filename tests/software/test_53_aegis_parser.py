"""AEGIS filesystem parser tests: labels, VTOC, VTOCEs."""

from datetime import datetime
from pathlib import Path

import pytest

from fatfloppy.core.apollo_aegis import (
    AegisError,
    LvLabel,
    PvLabel,
    Vtoce,
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
        assert isinstance(pv, PvLabel)
        assert pv.name == "FLPB.SR9"
        assert pv.uid_text == "2A58CC30.A0002FC2"
        assert pv.total_blocks == 0x4D0
        assert pv.lv_daddr == 1
        assert pv.alt_lv_daddr == 0x4CF

    def test_lv_label(self, disk5):
        lv = parse_lv_label(disk5, lv_base=1)
        assert isinstance(lv, LvLabel)
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
        assert isinstance(e, Vtoce)
        assert e.type_uid_hi == 0x315
        assert e.blocks_used == 10
        assert isinstance(e.dtm, datetime) and e.dtm.year == 1985
        assert e.uid_text and e.parent_uid_text
