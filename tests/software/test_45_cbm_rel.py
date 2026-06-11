"""REL file tests: side sectors (D64/D71) and super side sectors (D81)."""

import pytest

from fatfloppy.core.disk import Disk
from fatfloppy.core.drivers.cbm_image import CBMImageDriver
from fatfloppy.core.filesystems.cbm_fs import CBMFilesystem

from .test_42_cbm_filesystem_read import RESOURCES
from .test_44_cbm_write import fresh_formatted


class TestRel1541:
    def test_rel_write_read_round_trip(self):
        fs = fresh_formatted("cbm_1541_d64", label="RELTEST")
        records = b"".join(bytes([i] * 100) for i in range(1, 31))
        fs.write_file("/DATA,r:100", records)
        e = fs.list_directory("/")[0]
        assert e.attributes == "REL:100"
        assert fs.read_file("/DATA") == records

    def test_rel_pads_partial_record(self):
        fs = fresh_formatted("cbm_1541_d64")
        fs.write_file("/PAD,r:50", b"\x01" * 60)
        assert fs.read_file("/PAD") == b"\x01" * 60 + b"\x00" * 40

    def test_rel_empty_writes_one_blank_record(self):
        fs = fresh_formatted("cbm_1541_d64")
        fs.write_file("/E,r:80", b"")
        assert fs.read_file("/E") == bytes(80)

    def test_side_sector_structure_byte_level(self):
        fs = fresh_formatted("cbm_1541_d64")
        data = bytes(254 * 130)  # 130 data blocks -> 2 side sectors
        fs.write_file("/BIG,r:127", data)
        _t, _s, _k, entry = fs._find_entry("/BIG")
        ss_t, ss_s = entry[0x15], entry[0x16]
        assert ss_t != 0
        ss0 = bytes(fs._read_ts(ss_t, ss_s))
        assert ss0[2] == 0
        assert ss0[3] == 127
        ss1_t, ss1_s = ss0[0], ss0[1]
        ss1 = bytes(fs._read_ts(ss1_t, ss1_s))
        assert ss1[2] == 1
        assert ss1[3] == 127
        assert ss0[4:8] == ss1[4:8] == bytes([ss_t, ss_s, ss1_t, ss1_s])
        assert ss0[8:16] == ss1[8:16] == bytes(8)
        chain = fs._follow_chain(entry[3], entry[4])
        assert len(chain) == 130
        for j in range(120):
            assert (ss0[0x10 + 2 * j], ss0[0x11 + 2 * j]) == chain[j]
        for j in range(10):
            assert (ss1[0x10 + 2 * j], ss1[0x11 + 2 * j]) == chain[120 + j]
        assert ss1[0] == 0 and ss1[1] == 0x10 + 2 * 10 - 1

    def test_entry_counts_include_side_sectors(self):
        fs = fresh_formatted("cbm_1541_d64")
        fs.write_file("/BIG,r:127", bytes(254 * 130))
        e = fs.list_directory("/")[0]
        assert e.extra_data["sectors"] == 130 + 2

    def test_rel_delete_frees_everything(self):
        fs = fresh_formatted("cbm_1541_d64")
        fs._initialize()
        free0 = fs._bam.free_blocks()
        fs.write_file("/R,r:100", bytes(254 * 10))
        fs.delete("/R")
        assert fs._bam.free_blocks() == free0
        assert fs._bam.verify_counts()
        assert fs.list_directory("/") == []

    def test_rel_check_passes(self):
        fs = fresh_formatted("cbm_1541_d64")
        fs.write_file("/R,r:100", bytes(254 * 10))
        assert fs.check()

    def test_rel_too_big_for_1541_rejected_cleanly(self):
        fs = fresh_formatted("cbm_1541_d64")
        fs._initialize()
        free0 = fs._bam.free_blocks()
        with pytest.raises(OSError):
            fs.write_file("/HUGE,r:254", bytes(254 * 721))
        assert fs._bam.free_blocks() == free0
        assert fs._bam.verify_counts()
        assert fs.list_directory("/") == []

    def test_rel_overwrite_replaces(self):
        fs = fresh_formatted("cbm_1541_d64")
        fs.write_file("/R,r:100", bytes(100 * 4))
        fs.write_file("/R,r:50", bytes(50 * 2))
        e = fs.list_directory("/")[0]
        assert e.attributes == "REL:50"
        assert len(fs.list_directory("/")) == 1


class TestRel1571:
    def test_rel_round_trip_d71(self):
        fs = fresh_formatted("cbm_1571_d71", label="R71")
        records = b"".join(bytes([i % 256] * 60) for i in range(100))
        fs.write_file("/R,r:60", records)
        assert fs.read_file("/R") == records
        assert fs.check()


class TestRelD81:
    def test_super_side_sector_structure(self):
        fs = fresh_formatted("cbm_1581_d81", label="R81")
        data = bytes(254 * 130)
        fs.write_file("/BIG,r:127", data)
        _t, _s, _k, entry = fs._find_entry("/BIG")
        sss = bytes(fs._read_ts(entry[0x15], entry[0x16]))
        assert sss[2] == 0xFE
        g0 = (sss[0], sss[1])
        assert (sss[3], sss[4]) == g0
        assert sss[5:] == bytes(251)
        ss0 = bytes(fs._read_ts(*g0))
        assert ss0[2] == 0 and ss0[3] == 127
        assert fs.read_file("/BIG") == data

    def test_d81_rel_round_trip_and_delete(self):
        fs = fresh_formatted("cbm_1581_d81")
        fs._initialize()
        free0 = fs._bam.free_blocks()
        records = b"".join(bytes([i % 256] * 200) for i in range(50))
        fs.write_file("/R,r:200", records)
        e = fs.list_directory("/")[0]
        # 50*200=10000 bytes -> ceil(10000/254)=40 data blocks + 1 SS + 1 SSS
        assert e.extra_data["sectors"] == 40 + 1 + 1
        assert fs.read_file("/R") == records
        assert fs.check()
        fs.delete("/R")
        assert fs._bam.free_blocks() == free0
        assert fs.check()

    def test_d81_multi_group_rel(self):
        fs = fresh_formatted("cbm_1581_d81")
        data = bytes(254 * 750)  # 750 data blocks -> group 0 (720) + group 1 (30)
        fs.write_file("/HUGE,r:127", data)
        _t, _s, _k, entry = fs._find_entry("/HUGE")
        sss = bytes(fs._read_ts(entry[0x15], entry[0x16]))
        assert sss[2] == 0xFE
        g0, g1 = (sss[3], sss[4]), (sss[5], sss[6])
        assert (sss[0], sss[1]) == g0  # group 0 pointer duplicated at 0/1
        assert g1[0] != 0
        ss_g1 = bytes(fs._read_ts(*g1))
        assert ss_g1[2] == 0  # group-local SS numbering restarts
        assert fs.read_file("/HUGE") == data
        assert fs.check()
        e = fs.list_directory("/")[0]
        # 750 data + 6 SS (g0) + 1 SS (g1) + 1 SSS
        assert e.extra_data["sectors"] == 750 + 6 + 1 + 1


class TestRelCheckVerification:
    def test_check_fails_on_corrupt_side_sector_pointer(self):
        fs = fresh_formatted("cbm_1541_d64")
        fs.write_file("/R,r:100", bytes(254 * 10))
        _t, _s, _k, entry = fs._find_entry("/R")
        ss = bytearray(fs._read_ts(entry[0x15], entry[0x16]))
        ss[0x11] ^= 0x01  # flip the first data block's sector pointer
        fs._write_ts(entry[0x15], entry[0x16], bytes(ss))
        assert fs.check() is False

    def test_check_fails_on_reclen_mismatch(self):
        fs = fresh_formatted("cbm_1541_d64")
        fs.write_file("/R,r:100", bytes(254 * 10))
        _t, _s, _k, entry = fs._find_entry("/R")
        ss = bytearray(fs._read_ts(entry[0x15], entry[0x16]))
        ss[3] = 99  # side-sector record length no longer matches the entry
        fs._write_ts(entry[0x15], entry[0x16], bytes(ss))
        assert fs.check() is False


class TestRealRelStillWorks:
    def test_1571_demo_rel_intact(self):
        path = RESOURCES / "1571_demo.d71"
        if not path.exists():
            pytest.skip("resource not present")
        drv = CBMImageDriver(str(path))
        disk = Disk(drv)
        disk.set_geometry(drv.physical_format)
        fs = CBMFilesystem(disk)
        rel = [e for e in fs.list_directory("/") if e.attributes.startswith("REL")]
        assert rel and rel[0].attributes == "REL:127"
        data = fs.read_file("/" + rel[0].name)
        assert len(data) == rel[0].size
