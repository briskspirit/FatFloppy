"""AEGIS filesystem parser tests: labels, VTOC, VTOCEs, directories, catalog."""

import hashlib
import logging
import struct
from datetime import datetime
from pathlib import Path

import pytest

from fatfloppy.core.apollo_aegis import (
    AegisCatalog,
    AegisError,
    build_catalog,
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


@pytest.fixture(scope="module")
def disk5_catalog(disk5) -> AegisCatalog:
    return build_catalog(disk5)


class TestDirectories:
    def test_root_dir_entries(self, disk5_catalog):
        # Root = the volume entry directory (vtoc_hdr.root_dir_vtocx 0x2660).
        # Names exactly as stored on disk (UPPERCASE on disk5 -- the plan's
        # lowercase transcription drifted; out_03_dirs.txt is authoritative).
        names = [e.name for e in disk5_catalog.root.children]
        assert names == ["SYSBOOT", "SYS", "COM", "BSCOM", "DOMAIN_EXAMPLES"]

    def test_network_root_wrapper(self, disk5_catalog):
        # The network root ``//`` wraps the volume entry dir under the node
        # name; it is metadata (spec section 4), not a path component.
        assert disk5_catalog.node_entry_name == "NODE_2FC2"
        assert disk5_catalog.network_root is not None
        assert disk5_catalog.network_root.vtoce.uid_text == "2A58CE0C.E0002FC2"
        assert disk5_catalog.root.vtoce.uid_text == "2A58CE0A.D0002FC2"

    def test_full_tree_object_counts(self, disk5_catalog):
        # Empirical walk (out_04_walk.txt): total=80 in-tree=69
        # acl-objects=11 unaccounted=0; the 69 split into 52 FILE and
        # 17 DIR lines (the network root and the volume entry dir are
        # both directories).
        assert disk5_catalog.counts == {"files": 52, "dirs": 17, "acl": 11}
        assert disk5_catalog.reachable_objects == 69
        assert disk5_catalog.unreferenced == 0
        assert len(disk5_catalog.objects) == 80

    def test_lookup_case_sensitivity(self, disk5_catalog):
        exact = disk5_catalog.lookup("/SYS/DM/DM")
        fallback = disk5_catalog.lookup("/sys/dm/dm")
        assert exact is fallback
        assert disk5_catalog.lookup("/") is disk5_catalog.root
        with pytest.raises(FileNotFoundError):
            disk5_catalog.lookup("/NO/SUCH/FILE")

    def test_clean_volume_has_no_warnings(self, disk5_catalog):
        assert disk5_catalog.warnings == []


class TestFileMaps:
    def test_direct_only_file(self, disk5, disk5_catalog):
        # SYSBOOT: 10240 bytes over LV daddrs 1..0xA == physical blocks 2..11
        # (out_04_walk.txt: "SYSBOOT == abs blocks 2..11: True").  Lowercase
        # path exercises the case-insensitive lookup fallback.
        data = disk5_catalog.read(disk5_catalog.lookup("/sysboot"))
        assert data == disk5[2 * 1024 : 12 * 1024]

    def test_l1_indirect_file(self, disk5_catalog):
        # /SYS/DM/DM: 242,406 bytes = 32 direct + 205 L1 pages, 238 blocks
        # used.  Type 0x302 (obj) is not in the managed strip set, so the
        # size stays the raw VTOCE length.
        e = disk5_catalog.lookup("/sys/dm/dm")
        assert e.size == 242406
        data = disk5_catalog.read(e)
        assert len(data) == 242406

    def test_managed_file_size_excludes_storage_header(self, disk5_catalog):
        # uasc (0x311) files report length minus the 32-byte storage header.
        e = disk5_catalog.lookup("/SYS/SPM/STARTUP_TEMPLATES/STARTUP.SPM")
        assert e.vtoce.length == 1735
        assert e.size == 1735 - 32

    def test_read_directory_raises(self, disk5_catalog):
        with pytest.raises(IsADirectoryError):
            disk5_catalog.read(disk5_catalog.lookup("/SYS"))

    def test_extraction_oracle_all_files(self, disk5_catalog):
        # Every regular file hashes to the empirical oracle value.
        #
        # Derivation (throwaway script over the dissection artifacts): the 52
        # files in docs/superpowers/research/apollo/aegis_empirical/extracted/
        # were written by 04_walk.py via vtoclib.read_file, which returns the
        # raw object content truncated to the VTOCE length -- storage headers
        # KEPT (verified: NODE_2FC2__SYS__SPM__STARTUP_TEMPLATES__STARTUP.SPM
        # begins 00 20 00 01 d7 df f8 b3).  cat.read strips the 32-byte
        # storage header for managed types {0x311 uasc, 0x300 rec, 0x301
        # hdru} when the 0020 0001 magic is present, so for the 14 such files
        # (types from the out_04_walk.txt listing; all 14 verified to start
        # with the magic) the pinned hash is sha256(oracle_bytes[32:]); for
        # the other 38 it is sha256(oracle_bytes).  Note 0x302 obj files also
        # carry the magic on disk5 but are deliberately NOT stripped.
        oracle = {
            "/SYSBOOT": "769611ed01878a6eb30634747b4085c0d16a470858b8acdf727b8d83b663a425",
            "/SYS/SPM/SPMLOGIN": "b38a5ad8efe099380989a10e6e3fd2ce19a293664bfdeb6f4a68165b7c7d69ff",
            "/SYS/SPM/STARTUP_TEMPLATES/STARTUP.SPM": "e978b375bc37f872936ffad482c40c539b8588cf5f5f3f9d5bcc5c5316ea3f03",
            "/SYS/SUBSYS/LOGIN": "c994ae300f298d3224da52b2935a0dcbb15c84e468c3fc48e84aa0e47c912d03",
            "/SYS/SIOLOGIN/SIOLOGIN": "fb3673762419cf47ecdd1739e7ded375d105ef8b9e7934d673ca5503b74207d1",
            "/SYS/DM/FONTS/ICONS": "a27406be76f3c61c87caf0acc8923f0ec8eaf4d9b8ff8662507c7df6442b9331",
            "/SYS/DM/FONTS/LEGEND": "1b04c8d164bd2c66ea4d63ccc6b8d6217e3077c71ec276a7d8b6b6d75dd00c79",
            "/SYS/DM/FONTS/LEGEND.19L": "f2db4b51b38236083acdbd299b375ce3a54078fb2c8535d57a026e186424c88b",
            "/SYS/DM/FONTS/F7X13": "1c40c3432479dedf0053fc57fa12dc6e32adc19cdf0a2774aa2518eb7acf045d",
            "/SYS/DM/FONTS/F5X9": "012658f557530c9fa84a78265a2f4596722856ab7a3e3fc996b10342255ac0b6",
            "/SYS/DM/FONTS/F7X13.B": "1c9167918749eafea19a0a72f1efef13416bc6c5b13f9128a052d342323a13bf",
            "/SYS/DM/FONTS/BLANK": "f29aa36784f3e822a153539fd682c80ca53933596ea5c606e3367b844b36ee58",
            "/SYS/DM/COLOR_MAP": "d91598496ca6ef59f5bf08fd07e98711064466759391d7ec810a230fb3d40916",
            "/SYS/DM/DM": "dba56a943f2bff3dba8b418681e08be39e8464f62545ab6ea7f73fd38625544c",
            "/SYS/DM/STARTUP_LOGIN": "3879c3cd783eaeec040fd0d640b0a12176442d2254a91d2775ae7fc4a3c236db",
            "/SYS/DM/STARTUP_LOGIN.19L": "bb3f8e292466b92bc1fd6686d80e4060096813a0533a54c80c4b3e887ce135d0",
            "/SYS/DM/STARTUP_LOGIN.COLOR": "71e7d8eb148b8d9b956f4821d1a0894a05e57080e483b08b4397aacb81f930e3",
            "/SYS/DM/STD_KEYS": "9c6cde9c5c95073c70c0ae87a1f3d88bb7e7fce14dd1819f02812d27aac2acd9",
            "/SYS/DM/STD_KEYS2": "32360f5b9369761958519975251a5d877269e114ce71c97f2968c60f411756c8",
            "/SYS/DM/STARTUP_TEMPLATES/STARTUP.19L": "208ce5d7848be3e52e7dbcb638bdd1795cdd42451772346b022adb9af7d5300a",
            "/SYS/DM/STARTUP_TEMPLATES/STARTUP.COLOR": "9a8706632518c97c1383f8906ca538f1aa723eb9b6f2ab2fdac0bb297bf8c74a",
            "/SYS/DM/STARTUP_TEMPLATES/STARTUP": "2c2c363f8ff3aee81160a59302f8e4cd530cf7cd0c2525ce12cd024b1b21a965",
            "/SYS/APOLLO_LOGO": "19911c7ef20953711c69b08fa2b40fff839007d4acc756f1e880b6f41f00900a",
            "/SYS/COLOR_MICROCODE": "642b4671924d8eaf6b11eab880f44e0996f845d2a08ad3103f099d090c6ad83e",
            "/SYS/COLOR_MICROCODE.550": "b8df9cf762ffd7f5305be5f980563376fda6fad10f197b760eb88dca7e00cb45",
            "/SYS/ENV": "018c661434a2ece15b82ccee9cf54806d47291e9677beb65d13ce9e7ed3be14b",
            "/COM/LD": "6b102586bf40426f27f6943751484d05af2643cf199ad0963df4399cd11245c3",
            "/COM/WD": "61b9f681af3f929d23f2f79be799e59a62a0a4f8acb4b62116baaabcf61312ed",
            "/COM/SH": "6f7caecfcfc00a972574af9e7d8c024e455f97b853afcefd6af4590ffb50a68c",
            "/COM/CTNODE": "83ebb041387f73df344f4440a120c4d413d889b711f4f010dab851603bcf4e35",
            "/COM/UCTNODE": "a82dd43f5a8793c8ceabbb6a28f02ebe6de0818767134b7b2f5aface4a71a060",
            "/COM/MTVOL": "d8b7091ade0984445dc7fbdab77d649086483e3028eeb96bbe80a1c02da2a791",
            "/COM/DMTVOL": "fe9af134dfa6995aaeee74cd1c1051d0c840780b42c0b0248765e5efe59608bd",
            "/COM/LAMF": "a69d7ba89522b4adaab626c57d957d87b7bcec9bc9fc04e97de9dcaf1eb48893",
            "/COM/LOGIN": "33e32ab5e7571ac5d537e850d52bbd81df0c64bbffe4074aa29530f6275f1878",
            "/COM/EDACCT": "e40fbb8b52cdbe3bfd2d294b9455211603a55128ff914dcd109cbbcd73bf8e4e",
            "/COM/EDPPO": "d14e9b0f524f62c2a25d1e97de9498872b51e6615fdc4603c1d6fc5f8c70aad5",
            "/COM/CHHDIR": "a04ca5bb470a0c77a6bd9d8af4c99c90f9bfcb99d8a2c5addcb20a924919398e",
            "/COM/CHPASS": "17fed5daa5288b502aaa070aeaabcd046c3b1383911ea8be90d7603bb998b062",
            "/COM/SALACL": "24531d3da1b736e32128306636ba724821ef3f0a1e2c79d8c8e5fd7d1b64e03d",
            "/COM/FIND_ORPHANS": "642bf9011e4a5e070760a833df846c6e2899809b43202cde5909ebc4ec4a9b15",
            "/COM/XSUBS": "9a009777fdeb48bcb9896228eb3d5baeb59c4f37c695d94b3523e226543f0d53",
            "/BSCOM/LIB.BS": "8035723a1a16457f5b4378c310df0a41fec677730094de9b79cb80bc47c81721",
            "/BSCOM/CPBOOT.BS": "4fae0b13e9b75f9f6c1c88dbf899dbad075058d1bfd8d4f7304e901720cb7947",
            "/BSCOM/DLT.BS": "9d54ef2908ae33363b19d22646f6d19602f4786f698cc3d41078562f545e94c0",
            "/BSCOM/LAS.BS": "7e1aa2a0b29ffc0e34514dba929d42f64d94f0c1d8d4437e2bb849524d5af51f",
            "/BSCOM/CPT.BS": "2a4d6d3668fa0e137fb6577bf70fbff66788dd070347ea2b5a3e58ac7d16f445",
            "/BSCOM/RBAK_SHELL": "d734223b2278a056700f279d0d6cf291c0ec434d057b25c12a012d5b136d31e9",
            "/DOMAIN_EXAMPLES/NETMAIN/NUD_EXAMPLE.PAS": "1ae3e43307ecd81989fe0c65896a00a93297df70a4eca7fd64e1ca4d79a67f47",
            "/DOMAIN_EXAMPLES/NETMAIN/NUD_EXAMPLE.FTN": "f8fd0152ef0d3f1e36e77d2535a677d71a4ddcde36797d2256ff5d30ce689f2c",
            "/DOMAIN_EXAMPLES/GETTING_STARTED/HELLO.FTN": "fa2912b074c87c5490de3580987b8b9ffe83ad5666a655fe8616f2fe98882dcf",
            "/DOMAIN_EXAMPLES/GETTING_STARTED/SAMPLE_EDIT": "8bbf15f4237cacf468b47c1964c2f4134befc016f5993eedcf881dc5b93e28b8",
        }
        assert len(oracle) == 52
        for path, want in oracle.items():
            data = disk5_catalog.read(disk5_catalog.lookup(path))
            got = hashlib.sha256(data).hexdigest()
            assert got == want, path

    def test_all_catalogued_files_covered_by_oracle(self, disk5_catalog):
        def files_of(node):
            for child in node.children:
                if child.is_dir:
                    yield from files_of(child)
                else:
                    yield child

        assert len(list(files_of(disk5_catalog.root))) == 52


def _page_pattern(n: int) -> bytes:
    """Deterministic content with a distinct pattern per 1024-byte page."""
    out = bytearray()
    page = 0
    while len(out) < n:
        out += hashlib.sha256(f"aegis-page-{page}".encode()).digest() * 32
        page += 1
    return bytes(out[:n])


def _storage_header(total_length: int) -> bytes:
    """A 32-byte managed-file storage header (out_05_acl_stream.txt layout:
    u16 0x0020, u16 0x0001, u32 mtime, u32 record/line count, u32 total
    length including the header, u8 flags, zero pad)."""
    return (
        struct.pack(">HHIII", 0x0020, 0x0001, 0x2A58CC30, 0, total_length)
        + b"\x28"
        + bytes(15)
    )


class AegisVolumeBuilder:
    """Constructs a minimal valid AEGIS floppy image in memory.

    Mirrors the on-disk structures the parser consumes (PV/LV labels with
    the vtoc_hdr at +0x4C, a 1-bucket VTOC chain, a consistent BAT,
    directories per Internals ch.8, direct/L1/L2 file maps, sparse pages,
    managed-type storage headers).  Building it correctly IS a test of our
    format understanding: the multi-block directory layout in particular is
    doc-interpreted only (unobserved on disk5) -- see _dir_bytes.
    """

    TOTAL_BLOCKS = 1232
    LV_BASE = 1
    BLOCK = 1024
    TS = 0x2A58CC30  # 1985-11-25, same era as disk5

    def __init__(self):
        self._files = []  # (name, content, type_uid_hi, sparse_pages)
        self._dirs = []  # (name, entry_count)

    def with_file(self, name, size=None, content=None, type_uid_hi=0, sparse_pages=()):
        if content is None:
            content = _page_pattern(size)
        content = bytearray(content)
        for page in sparse_pages:  # sparse pages read back as zeros
            content[page * self.BLOCK : (page + 1) * self.BLOCK] = bytes(
                min(self.BLOCK, max(0, len(content) - page * self.BLOCK))
            )
        self._files.append((name, bytes(content), type_uid_hi, frozenset(sparse_pages)))
        return self

    def with_dir_entries(self, name, count):
        self._dirs.append((name, count))
        return self

    # -- low-level helpers -------------------------------------------------

    def _alloc(self):
        daddr = self._next_daddr
        self._next_daddr += 1
        if daddr + self.LV_BASE >= self.TOTAL_BLOCKS:
            raise RuntimeError("synthetic volume full")
        self._allocated.add(daddr)
        return daddr

    def _write_block(self, daddr, data):
        assert len(data) <= self.BLOCK
        start = (daddr + self.LV_BASE) * self.BLOCK
        self._img[start : start + len(data)] = data

    def _new_uid(self):
        self._uid_counter += 1
        return struct.pack(">II", self.TS + self._uid_counter, 0x00001111)

    def _place_pages(self, content, sparse_pages):
        """Write content pages; return their LV daddrs (0 = sparse)."""
        npages = -(-len(content) // self.BLOCK)
        daddrs = []
        for i in range(npages):
            if i in sparse_pages:
                daddrs.append(0)
                continue
            daddr = self._alloc()
            self._write_block(daddr, content[i * self.BLOCK : (i + 1) * self.BLOCK])
            daddrs.append(daddr)
        return daddrs

    def _index_block(self, daddrs):
        """Write one 256-entry BE-u32 indirect block; return its daddr."""
        assert len(daddrs) <= 256
        daddr = self._alloc()
        self._write_block(daddr, b"".join(struct.pack(">I", d) for d in daddrs))
        return daddr

    def _file_map(self, page_daddrs):
        """Split page daddrs into (direct[32], l1_daddr, l2_daddr)."""
        direct = page_daddrs[:32]
        l1 = l2 = 0
        rest = page_daddrs[32:]
        if rest:
            l1 = self._index_block(rest[:256])
            rest = rest[256:]
        if rest:
            l1_ptrs = [
                self._index_block(rest[i : i + 256]) for i in range(0, len(rest), 256)
            ]
            l2 = self._index_block(l1_ptrs)
        return direct, l1, l2

    @staticmethod
    def _dir_entry(buf, offset, name, uid):
        raw = name.encode("ascii")
        buf[offset : offset + 32] = raw.ljust(32, b" ")
        buf[offset + 0x26] = len(raw)
        buf[offset + 0x27] = 1  # entry type 1 = UID entry
        buf[offset + 0x28 : offset + 0x30] = uid

    def _dir_bytes(self, entries):
        """Directory object content per AEGIS Internals ch.8 (figure 8-3).

        Block 0 (1024 bytes, verified byte-exact against disk5):
        header 26 B (+0 version 1, +2 hash prime 43, +4 linear list size 18,
        +6 entry-block pool size 429, +8 entries/block 3, +0xA high block,
        +0xC free chain, +0xE parent UID (zero on disk5), +0x16 entry count,
        +0x18 max count 1300) + 18*48 B linear list + 48 B information block
        (+0x37A) + 43 u16 hash threads (+0x3AA) == 1024 exactly.

        Multi-block continuation (doc-interpreted, UNOBSERVED on real media):
        entries 19+ live in the entry-block pool -- 150-byte blocks (u16
        next, u16 prev hash-chain links, u8 block type 1 = entry array,
        u8 used count, 3 x 48-byte entries), numbered from 1, packed as a
        flat array from file offset 0x400 (the directory is mapped memory,
        so pool blocks crossing page boundaries is harmless).  "High block"
        is the highest pool block in use; hash threads are a search
        optimization and stay nil here (a full listing scans the pool).
        """
        linear = entries[:18]
        pool = entries[18:]
        pool_blocks = -(-len(pool) // 3)
        size = 0x400
        if pool_blocks:
            size = 0x400 + pool_blocks * 150
            size = -(-size // self.BLOCK) * self.BLOCK  # whole pages
        buf = bytearray(size)
        struct.pack_into(">5H", buf, 0, 1, 43, 18, 429, 3)
        struct.pack_into(">H", buf, 0x0A, pool_blocks)  # high block
        struct.pack_into(">H", buf, 0x16, len(entries))
        struct.pack_into(">H", buf, 0x18, 1300)  # maximum count
        buf[0x37A] = 0x01  # information block version (as on disk5)
        for i, (name, uid) in enumerate(linear):
            self._dir_entry(buf, 0x1A + 48 * i, name, uid)
        for block in range(pool_blocks):
            offset = 0x400 + block * 150
            group = pool[block * 3 : block * 3 + 3]
            buf[offset + 4] = 1  # block type 1 = directory entries
            buf[offset + 5] = len(group)  # used count
            for i, (name, uid) in enumerate(group):
                self._dir_entry(buf, offset + 6 + 48 * i, name, uid)
        return bytes(buf)

    # -- assembly ----------------------------------------------------------

    def build(self):
        self._img = bytearray(self.TOTAL_BLOCKS * self.BLOCK)
        self._allocated = set()
        self._next_daddr = 2  # LV daddr 0 = LV label; keep 1 free like a boot area
        self._uid_counter = 0

        net_uid = self._new_uid()
        root_uid = self._new_uid()
        # records: (uid, kind, type_hi, length, page_daddrs, parent_uid)
        records = []

        def add_object(uid, kind, type_hi, content, parent, sparse=frozenset()):
            pages = self._place_pages(content, sparse)
            records.append((uid, kind, type_hi, len(content), pages, parent))

        root_entries = []
        deferred = []  # directory content writes need children placed first
        for name, count in self._dirs:
            dir_uid = self._new_uid()
            child_entries = []
            for i in range(count):
                child_uid = self._new_uid()
                child_entries.append((f"E{i:02d}", child_uid))
            deferred.append((dir_uid, child_entries))
            root_entries.append((name, dir_uid))
        file_items = []
        for name, content, type_hi, sparse in self._files:
            file_uid = self._new_uid()
            file_items.append((file_uid, content, type_hi, sparse))
            root_entries.append((name, file_uid))

        # Object order matters for the truncated-chain test: the network
        # root and the volume entry dir land in the head VTOC block.
        add_object(net_uid, 2, 0, self._dir_bytes([("NODE_TEST", root_uid)]), bytes(8))
        add_object(root_uid, 2, 0, self._dir_bytes(root_entries), net_uid)
        for dir_uid, child_entries in deferred:
            add_object(dir_uid, 1, 0, self._dir_bytes(child_entries), root_uid)
            for _, child_uid in child_entries:
                records.append((child_uid, 0, 0, 0, [], dir_uid))
        for file_uid, content, type_hi, sparse in file_items:
            add_object(file_uid, 0, type_hi, content, root_uid, sparse)

        # VTOC: one bucket, 5 slots per block, chained via u32 next at +0.
        slot_offsets = (0x004, 0x0D0, 0x19C, 0x268, 0x334)
        vtoc_blocks = []
        block_buf = None
        vtocx_by_uid = {}
        for index, (uid, kind, type_hi, length, pages, parent) in enumerate(records):
            slot = index % 5
            if slot == 0:
                daddr = self._alloc()
                if vtoc_blocks:
                    struct.pack_into(">I", block_buf, 0, daddr)
                    self._write_block(vtoc_blocks[-1], block_buf)
                block_buf = bytearray(self.BLOCK)
                vtoc_blocks.append(daddr)
            offset = slot_offsets[slot]
            vtocx_by_uid[uid] = (vtoc_blocks[-1] << 4) | slot
            entry = bytearray(204)
            entry[1] = kind
            struct.pack_into(">H", entry, 2, 0x9800 if kind == 3 else 0x9000)
            entry[4:12] = uid
            struct.pack_into(">I", entry, 0x0C, type_hi)
            struct.pack_into(">I", entry, 0x1C, length)
            direct, l1, l2 = self._file_map(pages)
            blocks_used = len([d for d in pages if d]) + (1 if l1 else 0)
            struct.pack_into(">I", entry, 0x20, blocks_used)
            struct.pack_into(">I", entry, 0x24, self.TS)  # dtm
            struct.pack_into(">I", entry, 0x28, self.TS)  # dtu
            entry[0x2C:0x34] = parent
            for i, daddr in enumerate(direct):
                struct.pack_into(">I", entry, 0x40 + 4 * i, daddr)
            struct.pack_into(">I", entry, 0xC0, l1)
            struct.pack_into(">I", entry, 0xC4, l2)
            block_buf[offset : offset + 204] = entry
        if block_buf is not None:
            self._write_block(vtoc_blocks[-1], block_buf)

        # BAT: 1 = free, LSB-first within BE u32 words, covering
        # [first_covered, first_covered + bits).
        bat_daddr = self._alloc()
        first_covered = 2
        bits = (self.TOTAL_BLOCKS - self.LV_BASE) - first_covered
        bat = bytearray(self.BLOCK)
        free_count = 0
        for word_index in range(-(-bits // 32)):
            value = 0
            for bit in range(32):
                i = word_index * 32 + bit
                if i >= bits:
                    break
                if first_covered + i not in self._allocated:
                    value |= 1 << bit
                    free_count += 1
            struct.pack_into(">I", bat, word_index * 4, value)
        self._write_block(bat_daddr, bat)

        # PV label (abs block 0)
        pv = bytearray(self.BLOCK)
        pv[2:8] = b"APOLLO"
        pv[0x08:0x10] = b"TEST.SR9"
        pv[0x28:0x30] = struct.pack(">II", self.TS, 0x00001111)
        struct.pack_into(">I", pv, 0x34, self.TOTAL_BLOCKS)
        struct.pack_into(">H", pv, 0x38, 8)
        struct.pack_into(">H", pv, 0x3A, 2)
        struct.pack_into(">I", pv, 0x3C, self.LV_BASE)
        self._img[0 : self.BLOCK] = pv

        # LV label (abs block 1 = LV daddr 0): bat_hdr +0x2C, vtoc_hdr +0x4C
        lv = bytearray(self.BLOCK)
        lv[0x04:0x0C] = b"TEST.SR9"
        lv[0x24:0x2C] = struct.pack(">II", self.TS, 0x00002222)
        struct.pack_into(">I", lv, 0x2C, bits)
        struct.pack_into(">I", lv, 0x30, free_count)
        struct.pack_into(">I", lv, 0x34, bat_daddr)
        struct.pack_into(">I", lv, 0x38, first_covered)
        struct.pack_into(">H", lv, 0x40, 1)  # bat_step
        struct.pack_into(">H", lv, 0x4C, 0)  # vtoc version
        struct.pack_into(">H", lv, 0x4E, 1)  # bucket count
        struct.pack_into(">I", lv, 0x50, len(vtoc_blocks))
        struct.pack_into(">I", lv, 0x54, vtocx_by_uid[net_uid])
        struct.pack_into(">I", lv, 0x58, vtocx_by_uid[root_uid])
        struct.pack_into(">HI", lv, 0x64, 1, vtoc_blocks[0])
        struct.pack_into(">I", lv, 0xB0, self.TS)  # label_written
        self._img[self.BLOCK : 2 * self.BLOCK] = lv

        return bytes(self._img)


class TestSyntheticStructures:
    # Builder-backed pins for structures absent from disk5 (multi-block
    # directories, L2 maps, sparse pages) -- these pin OUR documented
    # interpretation of the Internals/handbook layouts.

    def test_builder_volume_parses_cleanly(self):
        img = AegisVolumeBuilder().with_file("data", size=100).build()
        cat = build_catalog(img)
        assert cat.warnings == []
        assert cat.counts == {"files": 1, "dirs": 2, "acl": 0}
        assert cat.read(cat.lookup("/data")) == _page_pattern(100)

    def test_multi_block_directory(self):
        img = AegisVolumeBuilder().with_dir_entries("big", 40).build()
        cat = build_catalog(img)
        big = cat.lookup("/big")
        assert [c.name for c in big.children] == [f"E{i:02d}" for i in range(40)]
        assert all(not c.missing for c in big.children)
        assert cat.warnings == []

    def test_l2_indirect_file(self):
        content = _page_pattern(350_000)  # 342 pages: 32 direct + 256 L1 + 54 L2
        img = AegisVolumeBuilder().with_file("huge", content=content).build()
        cat = build_catalog(img)
        assert cat.read(cat.lookup("/huge")) == content

    def test_sparse_pages_read_as_zeros(self):
        size = 5 * 1024 + 100
        img = (
            AegisVolumeBuilder()
            .with_file("holey", size=size, sparse_pages=(1, 3))
            .build()
        )
        cat = build_catalog(img)
        data = cat.read(cat.lookup("/holey"))
        assert len(data) == size
        expected = bytearray(_page_pattern(size))
        expected[1024:2048] = bytes(1024)
        expected[3072:4096] = bytes(1024)
        assert data == bytes(expected)
        assert data[1024:2048] == bytes(1024)

    def test_storage_header_stripped_for_uasc(self):
        body = b"Hello AEGIS world.\n" * 100
        managed = _storage_header(32 + len(body)) + body
        img = (
            AegisVolumeBuilder()
            .with_file("note", content=managed, type_uid_hi=0x311)
            .with_file("bare", content=body, type_uid_hi=0x311)  # no magic
            .with_file("prog", content=managed, type_uid_hi=0x302)  # not managed
            .build()
        )
        cat = build_catalog(img)
        note = cat.lookup("/note")
        assert note.size == len(body)
        assert cat.read(note) == body
        bare = cat.lookup("/bare")  # 0x311 without the magic: NOT stripped
        assert bare.size == len(body)
        assert cat.read(bare) == body
        prog = cat.lookup("/prog")  # 0x302 carries the magic but is kept
        assert prog.size == 32 + len(body)
        assert cat.read(prog) == managed

    def test_dir_header_variants_tolerated(self):
        img = bytearray(AegisVolumeBuilder().with_file("data", size=100).build())
        vtoc = read_vtoc(bytes(img), lv_base=1)
        lv = parse_lv_label(bytes(img), lv_base=1)
        root = vtoc.entries[lv.root_dir_vtocx]
        dir_block = (root.direct_daddrs[0] + 1) * 1024
        struct.pack_into(">H", img, dir_block + 2, 0x002C)  # hash prime 43 -> 44
        cat = build_catalog(bytes(img))
        assert [c.name for c in cat.root.children] == ["data"]
        assert any("header" in w.lower() for w in cat.warnings)

    def test_truncated_vtoc_chain_graceful(self):
        # Corrupt the head VTOC block's next pointer: build_catalog still
        # returns everything that parsed, plus a warning -- no exception.
        # The head block holds 5 VTOCEs (the net root, the volume entry
        # dir, "big" and its first two children E00/E01); the other 38
        # children lose their VTOCEs and must surface as missing (size 0)
        # entries.
        img = bytearray(AegisVolumeBuilder().with_dir_entries("big", 40).build())
        lv = parse_lv_label(bytes(img), lv_base=1)
        n_blocks, head = lv.vtoc_map[0]
        struct.pack_into(">I", img, (head + 1) * 1024, 0xFFFF)
        cat = build_catalog(bytes(img))
        assert cat.reachable_objects == 5  # net root, root, big, E00, E01
        big = cat.lookup("/big")
        assert len(big.children) == 40
        missing = [c for c in big.children if c.missing]
        assert len(missing) == 38
        assert all(c.size == 0 for c in missing)
        assert cat.warnings
