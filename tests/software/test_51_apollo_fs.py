"""ApolloWbakFilesystem tests: listing, reading, read-only gate, detection,
disk map and display info over the real disk2/disk8 resources."""

import datetime
import hashlib
import logging
import random
from pathlib import Path

import pytest

from fatfloppy.core.apollo_wbak import (
    IMAGE_SIZE,
    STREAM_START,
    ContinuationSpec,
    WbakCatalog,
    WbakEntry,
    build_catalog,
)
from fatfloppy.core.controller import DiskController
from fatfloppy.core.filesystems.apollo_wbak_fs import (
    ApolloWbakConfig,
    ApolloWbakFilesystem,
    AttachResult,
)
from fatfloppy.core.filesystems.fs_base import Filesystem

from .test_49_apollo_wbak_parser import (
    SYN_UID,
    StreamBuilder,
    build_block,
    build_continuation_volume,
    build_three_volume_set,
    build_two_volume_set,
    data_rec,
    file_rec,
    label80,
    mark_rec,
    storage_header,
    sub_rec,
)

RESOURCES = Path(__file__).parent.parent / "resources" / "APOLLO"
CBM_RESOURCES = Path(__file__).parent.parent / "resources" / "CBM"
# Local, gitignored copies of the external-volume Apollo images (the volume
# is removable); populated from /Volumes/EXTERNAL/BACKUPS/ZED/VINTAGE_STUFF_70s/.
REAL_VOLUMES = Path(__file__).parent.parent.parent / "local_images" / "APOLLO"

# local_images/APOLLO is organized by backup SET (see its README.md); the
# tests keep using the historical dump-order names.  This table is the ONE
# place mapping logical name -> current location; a future re-layout only
# needs this dict (tests/resources/APOLLO keeps the original names).
REAL_VOLUME_PATHS = {
    "disk1.img": "set_FT0003_1986-12/vol2_FT0003__disk1.img",
    "disk2.img": "set_5ETC_1987-01/vol1_5ETC__disk2.img",
    "disk3.img": "set_5BIN-5BIO_1987-01/vol2_5BIO__disk3.img",
    "disk4.img": "set_5BIN-5BIO_1987-01/vol1_5BIN__disk4.img",
    "disk5.img": "aegis/disk5.img",
    "disk6.img": "set_DS0008_1987-05/vol2_DS0008__disk6.img",
    "disk7.img": "set_DS0008_1987-05/vol3_DS0008__disk7.img",
    "disk7-1.img": "duplicates/disk7-1.img",
    "disk8.img": "set_FT0003_1986-12/vol1_FT0003__disk8.img",
    "disk9.img": "set_FT0006_1987-06/vol1_FT0006__disk9.img",
    "disk10.img": "set_FT0006_1987-06/vol2_FT0006__disk10.img",
}

CLEANUP_SHA256 = "3e4f699d17b9b08936e1f1d501bec3c3a56c4d2e978eb64134a32ac4186539e1"
# Stitched-content pins, derived from the independent reference dumper's
# cross-volume extraction (wbak_dump.py, not part of this repo): sha256 of the
# reference file with its 32-byte storage header stripped.
#   COM.seq2.31F49BD4/COM/FTN_SR9.2 -- 409,074 raw bytes (130,218 on disk8
#   + 278,856 on disk1); 439,276 were declared, so 30,202 were never
#   written to the set and the stitched file stays PARTIAL.
FTN_STITCHED_SHA256 = "6b19bd9fd46f13baa879eb7c6e37adf056b7284043e3f54fc138c48e2a0b6101"
#   SYS5_BIN.seq1.32A33654/SYS5/BIN/CC -- 29,708 raw bytes (15,664 on disk4
#   + disk3's 14,044-byte tail); 30,500 declared -> stays PARTIAL too.
CC_STITCHED_SHA256 = "e4da40612da40d6350ecfe23a1397c3fb669ef79a8df574479c8e5ad6af386f7"
#   COM.seq3.356E4C29/COM/FTN_SR9.2 (the FT0006 set's own ftn_sr9.2) --
#   424,944 content bytes (40,822 on disk9 + disk10's 384,122-byte tail);
#   439,244 were declared, so 14,300 were never written to the set and the
#   stitched file stays PARTIAL even though the tree is complete.
FT0006_FTN_SR92_SHA256 = (
    "06f89b10029cfb4de3619871a8cf6c8cb207c452ff32684cc78826513b6fd3ce"
)
#   COM.seq3.356E4C29/COM/FTN -- 549,306 content bytes, entirely on disk9
#   (damaged: 586,994 declared); the stitch must leave it untouched.
FT0006_FTN_SHA256 = "544b1164a9e73fd72638fffbcba8546b41102ca64b98c18bf3d36b20a51d4c20"


def real_volume_path(name: str) -> Path:
    """Resolve a logical dump-order name to its set-layout path, or skip."""
    path = REAL_VOLUMES / REAL_VOLUME_PATHS[name]
    if not path.exists():
        pytest.skip(f"real volume {name} not present under {REAL_VOLUMES}")
    return path


def real_volume_bytes(name: str) -> bytes:
    return real_volume_path(name).read_bytes()


def open_volume(tmp_path: Path, data: bytes, name: str = "primary.img"):
    path = tmp_path / name
    path.write_bytes(data)
    controller = DiskController()
    assert controller.open_disk(str(path), disk_type="auto")
    return controller


@pytest.fixture(scope="module")
def disk2():
    controller = DiskController()
    assert controller.open_disk(str(RESOURCES / "disk2.img"), disk_type="auto")
    yield controller
    controller.close_disk()


@pytest.fixture(scope="module")
def disk8():
    controller = DiskController()
    assert controller.open_disk(str(RESOURCES / "disk8.img"), disk_type="auto")
    yield controller
    controller.close_disk()


def build_aegis_stub() -> bytes:
    """APOLLO container without a wbak tape stream (disk5 analogue)."""
    rng = random.Random(0x5EED)
    img = bytearray(b"APOLLO\x00\x01")
    img += bytes(STREAM_START - len(img))
    img += bytes(rng.randrange(256) for _ in range(IMAGE_SIZE - STREAM_START))
    return bytes(img)


def build_nameless_image() -> bytes:
    """One tree with TWO files whose NAME records never arrived: both keep
    the catalog placeholder "?" and must be uniquified filesystem-side."""
    b = StreamBuilder()
    b.add_record(label80("VOL1", volume_id="SYNQ01", owner="APOLLO"))
    b.add_record(label80("UVL1", text="31F49BD4.200071FA"))
    b.add_record(label80("HDR1", file_id="TREEQ", section=1, sequence=1))
    b.add_record(label80("HDR2"))
    b.add_record(label80("UHL1", text="31F49BD4.200071FA"))
    b.add_tapemark()
    records = (
        sub_rec()
        + mark_rec()
        + file_rec(3 + 32)
        + data_rec(storage_header(3 + 32) + b"one")
        + mark_rec()
        + file_rec(3 + 32)
        + data_rec(storage_header(3 + 32) + b"two")
    )
    b.add_record(build_block(1, SYN_UID, records))
    b.add_tapemark()
    b.add_record(label80("EOF1", file_id="TREEQ", section=1, sequence=1))
    b.add_record(label80("EOF2"))
    b.add_tapemark()
    return b.finish()


class TestCatalogPlumbing:
    def test_catalog_exposes_frame_events_and_eot(self):
        cat = build_catalog((RESOURCES / "disk2.img").read_bytes())
        assert cat.eot_found is True
        kinds = {e.kind for e in cat.frame_events}
        assert "record" in kinds and "tapemark" in kinds

    def test_storage_headerless_raw_data_passes_through(self):
        # Contract (binding): raw data WITHOUT the 0x00200001 storage-header
        # magic is returned unchanged (only size-truncated), never stripped.
        entry = WbakEntry(path="x", size=5, raw_data=b"hello world")
        assert WbakCatalog().read(entry) == b"hello"


class TestListing:
    def test_disk2_root_is_mounted_tree_prefix(self, disk2):
        fs = disk2.filesystem
        assert fs.filesystem_type == "APOLLO_WBAK"
        infos = fs.list_directory("/")
        assert [i.name for i in infos] == ["sys5"]
        assert infos[0].is_dir
        assert infos[0].datetime.tzinfo is None  # naive UTC, documented

    def test_disk2_walk_to_tree_root(self, disk2):
        fs = disk2.filesystem
        assert [i.name for i in fs.list_directory("/sys5")] == ["etc"]
        infos = fs.list_directory("/sys5/etc")
        # 37 direct children (33 files incl. the recovered "fix_cache~1",
        # the net dir, 3 links) + the synthesized "templates" intermediate
        # (its entries appear under templates/ but no DIR record names it
        # here).  DELIBERATE update: the former "?" placeholder is now the
        # UID-overlap-recovered "fix_cache", uniquified against the
        # earlier clean copy as "fix_cache~1".
        assert len(infos) == 38
        names = {i.name for i in infos}
        assert {"releaselog", "motd", "rc", "net", "templates", "fix_cache~1"} <= names
        assert "?" not in names
        net = next(i for i in infos if i.name == "net")
        assert net.is_dir
        templates = next(i for i in infos if i.name == "templates")
        assert templates.is_dir  # synthesized intermediate component

    def test_disk2_real_datetimes_and_attributes(self, disk2):
        infos = disk2.filesystem.list_directory("/sys5/etc")
        releaselog = next(i for i in infos if i.name == "releaselog")
        assert releaselog.size == 42
        assert releaselog.attributes == ""
        # genuine Apollo mtime, naive UTC
        assert releaselog.datetime == datetime.datetime(1987, 1, 20, 15, 48, 37, 360640)
        motd = next(i for i in infos if i.name == "motd")
        assert motd.attributes == "DMG"

    def test_disk2_link_entry(self, disk2):
        infos = disk2.filesystem.list_directory("/sys5/etc")
        rc = next(i for i in infos if i.name == "rc")
        assert rc.attributes == "LINK"
        assert not rc.is_dir
        assert rc.extra_data["link_target"] == "`node_data/etc.rc"
        assert rc.extra_data["tree_id"] == "SYS5/ETC"

    def test_disk2_recovered_name_listing_and_content(self, disk2):
        # The clipped-NAME entry (formerly "?") lists under its recovered
        # name, uniquified against the earlier clean copy; its size, DMG
        # attribute and CONTENT are exactly what "?" read before recovery
        # (sha256 pins captured pre-change).
        fs = disk2.filesystem
        infos = fs.list_directory("/sys5/etc")
        recovered = next(i for i in infos if i.name == "fix_cache~1")
        assert recovered.size == 2344
        assert "DMG" in recovered.attributes
        assert recovered.extra_data["name_recovered"] is True
        assert recovered.extra_data["raw_name"] == b"SYS5/ETC/FIX_CACHE"
        content = fs.read_file("/sys5/etc/fix_cache~1")
        assert (
            hashlib.sha256(content).hexdigest()
            == "37c6a763f95c766d392dcf7b918b7944b80bc0e3b5b134d896dd4375c3e86e3e"
        )
        # the clean copy keeps its name, content and a False marker
        clean = next(i for i in infos if i.name == "fix_cache")
        assert clean.extra_data["name_recovered"] is False
        assert (
            hashlib.sha256(fs.read_file("/sys5/etc/fix_cache")).hexdigest()
            == "30d446e5bf59404ef934bf777ba11c30b5c0a0fd848914e4d28fe89d58eef282"
        )

    def test_disk8_root_merges_trees(self, disk8):
        names = {i.name for i in disk8.filesystem.list_directory("/")}
        assert names == {"install", "com"}

    def test_disk8_partial_file_attribute(self, disk8):
        infos = disk8.filesystem.list_directory("/com")
        # com/? STAYS the placeholder: its NAME record's zero word and
        # path were overwritten by file text, so the UID-overlap recovery
        # has no proof to act on
        assert [i.name for i in infos] == ["?", "ftn_sr9.2"]
        orphan = next(i for i in infos if i.name == "?")
        assert orphan.extra_data["name_recovered"] is False
        ftn = next(i for i in infos if i.name == "ftn_sr9.2")
        assert "PARTIAL" in ftn.attributes
        assert "DMG" in ftn.attributes
        assert ftn.size == 439276 - 32

    def test_case_insensitive_and_slash_tolerant(self, disk2):
        infos = disk2.filesystem.list_directory("/SYS5/Etc/")
        assert len(infos) == 38

    def test_missing_directory_raises(self, disk2):
        with pytest.raises(FileNotFoundError):
            disk2.filesystem.list_directory("/no/such/dir")

    def test_listing_a_file_raises(self, disk2):
        with pytest.raises(NotADirectoryError):
            disk2.filesystem.list_directory("/sys5/etc/releaselog")


class TestRead:
    def test_cleanup_pinned_sha256(self, disk8):
        content = disk8.filesystem.read_file("/install/com/cleanup_v1.0")
        assert len(content) == 1226 - 32
        assert hashlib.sha256(content).hexdigest() == CLEANUP_SHA256

    def test_damaged_read_succeeds_with_warning(self, disk8, caplog):
        with caplog.at_level(logging.WARNING):
            content = disk8.filesystem.read_file("/install/com/cpt")
        # damaged entry: the assembled data is shorter than the declared
        # 33672 bytes -- reading yields exactly what the catalog recovers
        cat = build_catalog((RESOURCES / "disk8.img").read_bytes())
        cpt = next(e for e in cat.trees[0].entries if e.path == "com/cpt")
        assert cpt.damaged
        assert content == cat.read(cpt)
        assert len(content) == 33212
        assert any("damaged" in r.message.lower() for r in caplog.records)

    def test_partial_read_returns_prefix_with_warning(self, disk8, caplog):
        with caplog.at_level(logging.WARNING):
            content = disk8.filesystem.read_file("/com/ftn_sr9.2")
        assert len(content) == 130218 - 32  # available prefix only
        assert any("volume" in r.message.lower() for r in caplog.records)

    def test_directory_read_raises(self, disk8):
        with pytest.raises(IsADirectoryError):
            disk8.filesystem.read_file("/install/com")  # real DIR entry
        with pytest.raises(IsADirectoryError):
            disk8.filesystem.read_file("/install")  # synthetic mount component

    def test_missing_file_raises(self, disk8):
        with pytest.raises(FileNotFoundError):
            disk8.filesystem.read_file("/install/com/nonexistent")

    def test_duplicate_path_reads_first_clean_copy(self, disk8):
        # disk8 INSTALL carries com/srf TWICE: first clean, second damaged.
        # Path lookup is FIRST match -- the clean copy wins.
        cat = build_catalog((RESOURCES / "disk8.img").read_bytes())
        srf = [e for e in cat.trees[0].entries if e.path == "com/srf"]
        assert len(srf) == 2
        assert not srf[0].damaged and srf[1].damaged
        content = disk8.filesystem.read_file("/install/com/srf")
        assert content == cat.read(srf[0])
        assert content != cat.read(srf[1])
        # the listing shows the entry once, clean
        infos = disk8.filesystem.list_directory("/install/com")
        assert [i.attributes for i in infos if i.name == "srf"] == [""]

    def test_link_read_returns_empty(self, disk2):
        assert disk2.filesystem.read_file("/sys5/etc/rc") == b""


class TestDebrisSizes:
    """FILE records whose attribute block was overwritten by old medium
    debris declare garbage sizes (e.g. b'    ' = 538,976,288 bytes on a
    1.2 MB floppy).  FileInfo must report the honest recoverable length,
    keep DMG, and retain the truth in extra_data."""

    def test_disk8_table_rec_ftn_reports_zero(self, disk8):
        fs = disk8.filesystem
        infos = fs.list_directory("/install/ftn")
        info = next(i for i in infos if i.name == "table_rec_ftn")
        assert info.size == 0  # was 538,976,256 (b'    ' minus storage header)
        assert "DMG" in info.attributes
        assert info.extra_data["size_unreliable"] is True
        assert info.extra_data["declared_size_raw"] == 538_976_288
        assert fs.read_file("/install/ftn/table_rec_ftn") == b""

    def test_disk8_reliable_sizes_carry_no_markers(self, disk8):
        infos = disk8.filesystem.list_directory("/install/com")
        info = next(i for i in infos if i.name == "cleanup_v1.0")
        assert info.size == 1226 - 32
        assert info.extra_data["size_unreliable"] is False
        assert info.extra_data["declared_size_raw"] is None

    def test_real_disk4_du_df_report_zero(self):
        # disk4's SYS5/BIN carries TWO debris-size entries, neither with
        # any recoverable DATA: du declares b'LAG ' (1,279,346,464) and df
        # 0x019A0038 (26,869,816 -- the smallest debris size corpus-wide,
        # 21.3x the volume capacity).  Pre-change sizes: 1,279,346,432 and
        # 26,869,784.
        controller = DiskController()
        assert controller.open_disk(
            str(real_volume_path("disk4.img")), disk_type="auto"
        )
        try:
            fs = controller.filesystem
            infos = fs.list_directory("/sys5/bin")
            du = next(i for i in infos if i.name == "du")
            df = next(i for i in infos if i.name == "df")
            assert du.size == 0 and df.size == 0
            assert du.extra_data["size_unreliable"] is True
            assert df.extra_data["size_unreliable"] is True
            assert du.extra_data["declared_size_raw"] == 1_279_346_464
            assert df.extra_data["declared_size_raw"] == 26_869_816
            assert "DMG" in du.attributes and "DMG" in df.attributes
            assert fs.read_file("/sys5/bin/du") == b""
            assert fs.read_file("/sys5/bin/df") == b""
        finally:
            controller.close_disk()

    def test_real_disk10_getftn_reports_recoverable(self):
        # disk10's getftn declares b'lack' (1,818,321,771 -- the largest
        # debris size corpus-wide) but 7,130 content bytes were assembled:
        # the size must report those, and reading must still return them
        # (read_file slices data[:size] -- naturally correct now).
        # Pre-change size: 1,818,321,739.
        controller = DiskController()
        assert controller.open_disk(
            str(real_volume_path("disk10.img")), disk_type="auto"
        )
        try:
            fs = controller.filesystem
            infos = fs.list_directory("/domain_examples/ftn_examples")
            info = next(i for i in infos if i.name == "getftn")
            assert info.size == 7130
            assert "DMG" in info.attributes
            assert info.extra_data["size_unreliable"] is True
            assert info.extra_data["declared_size_raw"] == 1_818_321_771
            content = fs.read_file("/domain_examples/ftn_examples/getftn")
            assert len(content) == 7130
        finally:
            controller.close_disk()

    def test_real_disk6_genuine_large_size_unchanged(self):
        # lib/dseelib is a REAL multi-volume library: 1,417,878 declared
        # raw bytes, the largest GENUINE declared size in the corpus at
        # 1.12x one volume's 1,261,568-byte capacity (a multi-volume file
        # legitimately exceeds one volume).  It must NOT be flagged; its
        # size stays as-declared (pinned pre-change: 1,417,846 displayed).
        controller = DiskController()
        assert controller.open_disk(
            str(real_volume_path("disk6.img")), disk_type="auto"
        )
        try:
            fs = controller.filesystem
            info = next(i for i in fs.list_directory("/lib") if i.name == "dseelib")
            assert info.size == 1_417_878 - 32
            assert info.extra_data["size_unreliable"] is False
            assert info.extra_data["declared_size_raw"] is None
            # pre-existing flags, pinned: cut at EOV and damaged
            assert "PARTIAL" in info.attributes
            assert "DMG" in info.attributes
        finally:
            controller.close_disk()


class TestReadOnly:
    @pytest.mark.parametrize(
        "call",
        [
            lambda fs: fs.write_file("/sys5/etc/new", b"x"),
            lambda fs: fs.delete("/sys5/etc/releaselog"),
            lambda fs: fs.delete_recursive("/sys5"),
            lambda fs: fs.create_directory("/newdir"),
            lambda fs: fs.format_fs(None),
        ],
    )
    def test_mutators_raise_read_only(self, disk2, call):
        with pytest.raises(OSError, match="read-only"):
            call(disk2.filesystem)

    def test_name_hint(self, disk2):
        assert disk2.filesystem.name_hint() == "read-only volume"


class TestValidityAndDetection:
    def test_disk2_end_to_end(self, disk2):
        fs = disk2.filesystem
        assert fs is not None
        assert fs.filesystem_type == "APOLLO_WBAK"
        assert fs.get_volume_label() == "5ETC"
        config = fs.get_specific_config()
        assert isinstance(config, ApolloWbakConfig)
        assert config.volume_id == "5ETC"
        format_name, _cfg, _pf = disk2.detect_format()
        assert format_name == "apollo_1.2m_wbak"

    def test_disk8_end_to_end(self, disk8):
        assert disk8.filesystem.filesystem_type == "APOLLO_WBAK"
        format_name, _cfg, _pf = disk8.detect_format()
        assert format_name == "apollo_1.2m_wbak"

    def test_aegis_stub_scores_below_threshold(self, tmp_path):
        # APOLLO container, no wbak stream: driver claims it, filesystem
        # must NOT (validity below both its own threshold and the global
        # auto-detection floor).
        p = tmp_path / "aegis.img"
        p.write_bytes(build_aegis_stub())
        controller = DiskController()
        assert controller.open_disk(str(p), disk_type="auto")
        assert controller.driver.driver_type == "APOLLO"
        assert controller.filesystem is None
        fs = ApolloWbakFilesystem(controller.disk)
        score = fs.get_validity_score()
        assert score < fs.validity_threshold
        assert fs.check() is False  # no labels, no EOT
        controller.close_disk()

    def test_cbm_resource_still_detects_as_cbm(self):
        path = CBM_RESOURCES / "vic1541_bam.d64"
        if not path.exists():
            pytest.skip("CBM resource not present")
        controller = DiskController()
        assert controller.open_disk(str(path), disk_type="auto")
        assert controller.filesystem.filesystem_type == "CBMDOS"
        controller.close_disk()

    def test_configs_match_sentinel_semantics(self):
        # The shipped profile carries a volume-id-less sentinel: it matches
        # any parsed volume (volume IDs are per-disk, not per-format).
        assert ApolloWbakFilesystem.configs_match(
            ApolloWbakConfig(volume_id=None), ApolloWbakConfig(volume_id="5ETC")
        )
        assert ApolloWbakFilesystem.configs_match(
            ApolloWbakConfig(volume_id="A"), ApolloWbakConfig(volume_id="A")
        )
        assert not ApolloWbakFilesystem.configs_match(
            ApolloWbakConfig(volume_id="A"), ApolloWbakConfig(volume_id="B")
        )
        assert not ApolloWbakFilesystem.configs_match(
            ApolloWbakConfig(volume_id=None), object()
        )

    def test_create_config_from_params_returns_none(self):
        # Formatting is unsupported; no config can be built from parameters.
        assert ApolloWbakFilesystem.create_config_from_params({}, None) is None


class TestMapAndDisplay:
    def test_disk2_map_five_types(self, disk2):
        layout = disk2.filesystem.get_disk_map_layout()
        assert len(layout["legend"]) == 5
        assert any("Damaged" in name for name, _color in layout["legend"])
        assert layout["allocation_unit_size_sectors"] == 1
        assert layout["first_data_sector"] == 0
        assert len(layout["type_color_map"]) == 5
        get_type = layout["get_sector_type"]
        assert get_type(0) == "system"
        assert get_type(1) == "system"
        # sector 2 (0x800) holds the VOL1..UHL1 ANSI labels
        assert get_type(2) == "directory"
        # sector 3 holds block 1's middle segments
        assert get_type(3) == "file"
        # disk2's stale sectors (pinned in test_49): 43 and 117
        assert get_type(43) == "damaged"
        assert get_type(117) == "damaged"
        # post-EOT tail (EOT at 0x32564 -> sector 201)
        assert get_type(500) == "free"
        assert get_type(1231) == "free"

    def test_allocated_units_are_stream_sectors(self, disk2):
        units = disk2.filesystem.get_allocated_units()
        assert units == sorted(set(units))
        assert units[0] >= 2  # the PV label sectors are not stream content
        assert 2 in units and 3 in units
        assert 43 not in units and 117 not in units  # stale: not stream data
        assert units[-1] <= 201  # nothing past EOT

    def test_file_allocation_units(self, disk8):
        fs = disk8.filesystem
        units = fs.get_file_allocation_units("/install/com/cleanup_v1.0")
        assert units and units == sorted(set(units))
        assert set(units) <= set(fs.get_allocated_units())
        assert fs.get_file_allocation_units("/install/com") == []  # directory
        assert fs.get_file_allocation_units("/nope") == []

    def test_free_space_is_disk_capacity(self, disk2):
        # Semantics changed in review: (free, total) is the FAT12-style
        # "data area" pair, and for a tape-on-floppy the data area is the
        # whole medium.  Returning recoverable-content bytes as "total"
        # rendered as a misleading "131.1 KB" capacity in the GUI space
        # panel; the content figure now lives in get_display_info under
        # "Recoverable Content".
        assert disk2.filesystem.get_free_space() == (0, IMAGE_SIZE)

    def test_disk8_recoverable_content_is_recovered_not_declared(self, disk8):
        # disk8 carries an entry whose destroyed FILE header declares
        # 0x20202020 (four ASCII spaces, 538,976,288) bytes; the
        # recoverable-content figure must count recovered content, not
        # declared sizes, or the info panel shows 514 MB on a 1.2 MB floppy.
        info = disk8.filesystem.get_display_info()
        content = int(info["Recoverable Content"].split()[0].replace(",", ""))
        assert 0 < content < IMAGE_SIZE
        cat = build_catalog((RESOURCES / "disk8.img").read_bytes())
        expected = sum(
            len(cat.read(e)) for t in cat.trees for e in t.entries if not e.is_dir
        )
        assert content == expected

    def test_recoverable_content_is_cached(self, disk2):
        # The content figure re-read every file per call before review;
        # it must be computed once and cached.
        fs = disk2.filesystem
        original_read = fs._catalog.read
        calls = []

        def counting_read(entry):
            calls.append(entry)
            return original_read(entry)

        fs._catalog.read = counting_read
        try:
            fs.get_display_info()
            after_first = len(calls)
            fs.get_display_info()
            assert len(calls) == after_first  # second call hit the cache
        finally:
            fs._catalog.read = original_read

    def test_allocation_unit_size_for_gui_space_panel(self, disk2):
        # The GUI space panel divides by filesystem.allocation_unit_size
        # (disk_manager.update_space_info); without it the disk map shows
        # everything free (the CBM regression, test_26).
        assert disk2.filesystem.allocation_unit_size == 1024

    def test_disk8_display_info(self, disk8):
        info = disk8.filesystem.get_display_info()
        assert info["Filesystem"] == "Apollo wbak (read-only)"
        assert info["Volume ID"] == "FT0003"
        assert info["Owner"] == "APOLLO"
        assert info["Created"].startswith("1986-12-17")
        assert info["Trees"] == "2"
        assert info["Files"] == "74"  # 72 INSTALL + 2 COM
        assert info["Read-Only"] == "yes"
        assert "Recoverable Content" in info
        assert info["Partial Files"] == "1"
        assert int(info["Damaged Files"]) > 0
        sets = info["Backup Sets"]
        assert "INSTALL" in sets and "COM" in sets
        assert "EOF" in sets and "EOV" in sets

    def test_check_true_on_real_disks(self, disk2, disk8):
        assert disk2.filesystem.check() is True
        assert disk8.filesystem.check() is True


class TestGuiReadOnly:
    def test_gui_import_surfaces_read_only_error(self, disk8, tmp_path):
        # The synchronous (non-physical) GUI import path must catch the
        # filesystem's OSError and show a dialog: an exception escaping a
        # Qt slot is fatal in PyQt, so without this the app would crash on
        # the first import attempt against a read-only volume.
        import os
        from types import SimpleNamespace
        from unittest.mock import patch

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtCore import QObject
        from PyQt6.QtWidgets import QApplication, QMessageBox

        _app = QApplication.instance() or QApplication([])
        from fatfloppy.gui.managers.file_manager import FileManager

        manager = FileManager.__new__(FileManager)
        QObject.__init__(manager)
        manager.parent = SimpleNamespace(controller=disk8)
        manager.logger = logging.getLogger("test")

        host = tmp_path / "host.txt"
        host.write_text("hello")
        boxes = []
        with patch.object(
            QMessageBox, "warning", side_effect=lambda *a, **_k: boxes.append(a)
        ):
            manager.import_multiple_paths([str(host)], "/install", auto_name=True)
        assert boxes, "expected an Import Failed dialog, not a raised exception"
        _parent, title, text = boxes[0][:3]
        assert title == "Import Failed"
        assert "read-only" in text


class TestPlaceholderUniquification:
    def test_nameless_entries_uniquified(self, tmp_path):
        p = tmp_path / "nameless.img"
        p.write_bytes(build_nameless_image())
        controller = DiskController()
        assert controller.open_disk(str(p), disk_type="auto")
        fs = controller.filesystem
        assert fs is not None and fs.filesystem_type == "APOLLO_WBAK"
        infos = fs.list_directory("/treeq")
        assert [i.name for i in infos] == ["?", "?~1"]
        # no junk remnant precedes these FILE records: nothing to recover
        assert all(i.extra_data["name_recovered"] is False for i in infos)
        assert fs.read_file("/treeq/?") == b"one"
        assert fs.read_file("/treeq/?~1") == b"two"
        controller.close_disk()

    def test_real_volumes_placeholders_stay(self):
        # Skip-guarded sweep over the local real images whose "?" names
        # are genuinely destroyed: the UID-overlap rule must not fire.
        # disk3: NAME overwritten by 68k code; disk4: zero word wrong
        # (junk reads 00 02 00 19 00 01 00 00 7c ff ...); disk6: three
        # remnants that keep their NAME headers / path fragments but
        # never present a uid-tail + zero-word + path shape.
        expected = {
            "disk3.img": ("SYS5/BIN", [("?", 3572, True)]),
            "disk4.img": ("SYS5/BIN", [("?", 6028, True)]),
            "disk6.img": (
                "DOMAIN_EXAMPLES",
                [("?", 91, False), ("?", 0, False), ("?", 26592, False)],
            ),
        }
        for name, (tree_id, placeholders) in expected.items():
            data = real_volume_bytes(name)
            cat = build_catalog(data)
            tree = next(t for t in cat.trees if t.file_id == tree_id)
            got = [
                (e.path, e.size, e.damaged) for e in tree.entries if e.raw_name == b"?"
            ]
            assert got == placeholders, name
            assert not any(e.name_recovered for t in cat.trees for e in t.entries), name


class TestAttachVolume:
    def test_capability_flag(self):
        assert ApolloWbakFilesystem.supports_volume_attach is True
        # absent (falsy) on the base class: the GUI probes via getattr
        assert getattr(Filesystem, "supports_volume_attach", False) is False

    def test_pending_continuations_disk8(self, disk8):
        # one EOV-cut tree (COM); the spec's uid is the per-tree UHL1 uid
        # read from the catalog at test time, not the per-volume UVL1
        cat = build_catalog((RESOURCES / "disk8.img").read_bytes())
        assert disk8.filesystem.pending_continuations() == [
            ContinuationSpec(
                file_id="COM",
                sequence=2,
                next_section=2,
                backup_uid=cat.trees[1].uid_text,
                volume_id="FT0003",
            )
        ]

    def test_attach_synthetic_continuation(self, tmp_path):
        s = build_two_volume_set()
        controller = open_volume(tmp_path, s.vol_a)
        try:
            fs = controller.filesystem
            assert fs.read_file("/com/bigfile") == s.prefix  # partial before
            result = fs.attach_volume(s.vol_b)
            assert isinstance(result, AttachResult)
            assert result.volume_id == "SYNVB0"
            assert result.stitched_tree_ids == ("COM",)
            assert result.still_incomplete == []
            # the previously-partial file now reads in full
            assert fs.read_file("/com/bigfile") == s.content
            big = next(i for i in fs.list_directory("/com") if i.name == "bigfile")
            assert "PARTIAL" not in big.attributes
            assert big.extra_data["partial"] is False
            # the continuation's follow-on entry joined the namespace
            assert fs.read_file("/com/followon") == s.follow_content
            info = fs.get_display_info()
            assert info["Attached Volumes"] == "SYNVB0"
            assert info["Partial Files"] == "0"
            assert "EOV" not in info["Backup Sets"]  # stitched -> complete
            assert fs.pending_continuations() == []
        finally:
            controller.close_disk()

    def test_attach_wrong_volume_rejected_state_unchanged(self, tmp_path):
        s = build_two_volume_set()
        controller = open_volume(tmp_path, s.vol_a)
        try:
            fs = controller.filesystem
            pending_before = fs.pending_continuations()
            prefix_before = fs.read_file("/com/bigfile")

            # identity mismatch (wrong sequence + section): no candidate
            # matches; the error names expected vs found, with volume ids
            wrong = build_continuation_volume(
                volume_id="SYNVC9", sequence=7, section=4, tail=s.tail
            )
            with pytest.raises(
                ValueError,
                match=(
                    r"Wrong volume 'SYNVC9'.*"
                    r"expected section 2 of 'COM' seq 2 \(uid 31F49BD4\.200071FA\).*"
                    r"contains 'COM' seq 7 section 4"
                ),
            ):
                fs.attach_volume(wrong)

            # uid mismatch on a structurally matching continuation: the
            # per-tree UHL1 validation in stitch_tree rejects it
            imposter = build_continuation_volume(
                uid_text="32A339B7.000071FA",
                uid=bytes.fromhex("32a339b7000071fa"),
                tail=s.tail,
            )
            with pytest.raises(ValueError, match=r"backup UID 32A339B7\.000071FA"):
                fs.attach_volume(imposter)

            # not a wbak backup volume at all
            with pytest.raises(ValueError, match=r"not a wbak backup volume"):
                fs.attach_volume(build_aegis_stub())

            # all three failures left the filesystem state untouched ...
            assert fs.pending_continuations() == pending_before
            assert fs.read_file("/com/bigfile") == prefix_before
            assert "Attached Volumes" not in fs.get_display_info()
            # ... and the RIGHT volume still attaches cleanly afterwards
            assert fs.attach_volume(s.vol_b).stitched_tree_ids == ("COM",)
            assert fs.read_file("/com/bigfile") == s.content
        finally:
            controller.close_disk()

    def test_attach_same_volume_twice_rejected(self, tmp_path):
        s = build_two_volume_set()
        controller = open_volume(tmp_path, s.vol_a)
        try:
            fs = controller.filesystem
            fs.attach_volume(s.vol_b)
            with pytest.raises(ValueError, match=r"already attached"):
                fs.attach_volume(s.vol_b)
            # the success state survives the rejected re-attach
            assert fs.read_file("/com/bigfile") == s.content
            assert fs.get_display_info()["Attached Volumes"] == "SYNVB0"
        finally:
            controller.close_disk()

    def test_three_volume_chain_still_incomplete(self, tmp_path):
        s = build_three_volume_set()
        vol_a, vol_b, vol_c = s.volumes
        controller = open_volume(tmp_path, vol_a)
        try:
            fs = controller.filesystem
            r1 = fs.attach_volume(vol_b)
            assert r1.stitched_tree_ids == ("COM",)
            # B ends EOV: the chain advertises the next (section 3) volume
            assert r1.still_incomplete == [
                ContinuationSpec(
                    file_id="COM",
                    sequence=2,
                    next_section=3,
                    backup_uid="31F49BD4.200071FA",
                    volume_id="SYNVA0",
                )
            ]
            assert r1.still_incomplete == fs.pending_continuations()
            # intermediate state: a longer prefix, still PARTIAL
            assert fs.read_file("/com/bigfile") == s.content[:8000]
            big = next(i for i in fs.list_directory("/com") if i.name == "bigfile")
            assert "PARTIAL" in big.attributes

            r2 = fs.attach_volume(vol_c)
            assert r2.stitched_tree_ids == ("COM",)
            assert r2.still_incomplete == []
            assert fs.read_file("/com/bigfile") == s.content
            assert fs.get_display_info()["Attached Volumes"] == "SYNVB0, SYNVC0"
        finally:
            controller.close_disk()

    def test_real_disk8_plus_disk1(self):
        # FT0003 COM seq 2: disk8 (section 1, EOV) -> disk1 (section 2,
        # EOF).  The two volumes carry DIFFERENT per-volume UVL1 uids; the
        # COM tree's UHL1 uid is identical on both -- the attach must
        # succeed on the per-tree invariant.
        disk1 = real_volume_bytes("disk1.img")
        disk9 = real_volume_bytes("disk9.img")
        controller = DiskController()
        assert controller.open_disk(str(RESOURCES / "disk8.img"), disk_type="auto")
        try:
            fs = controller.filesystem
            pending_before = fs.pending_continuations()
            assert [spec.file_id for spec in pending_before] == ["COM"]
            prefix = fs.read_file("/com/ftn_sr9.2")
            assert len(prefix) == 130218 - 32

            # disk9 belongs to FT0006: rejected, state unchanged
            with pytest.raises(ValueError, match=r"Wrong volume 'FT0006'"):
                fs.attach_volume(disk9)
            assert fs.pending_continuations() == pending_before
            assert fs.read_file("/com/ftn_sr9.2") == prefix

            result = fs.attach_volume(disk1)
            assert result.volume_id == "FT0003"
            assert result.stitched_tree_ids == ("COM",)
            assert result.still_incomplete == []

            # 30,202 of the declared 439,276 raw bytes were never written
            # to the set: the stitched file stays PARTIAL (and DMG), and
            # its content pins against the reference stitched extraction
            content = fs.read_file("/com/ftn_sr9.2")
            assert len(content) == 409042
            assert hashlib.sha256(content).hexdigest() == FTN_STITCHED_SHA256
            ftn = next(i for i in fs.list_directory("/com") if i.name == "ftn_sr9.2")
            assert "PARTIAL" in ftn.attributes
            assert "DMG" in ftn.attributes
            # disk1's foreign trees (DOMAIN_EXAMPLES, SYS/HELP, DOC) are
            # NOT merged: continuation-only, the namespace stays put
            assert {i.name for i in fs.list_directory("/")} == {"install", "com"}
            assert fs.get_display_info()["Attached Volumes"] == "FT0003"
        finally:
            controller.close_disk()

    def test_real_sys5_bin_disk4_plus_disk3(self):
        # Second real set, second proof of the per-tree uid invariant:
        # disk4 (SYS5/BIN seq 1 section 1, EOV) + disk3 (section 2, EOF).
        # 'cc' is the EOV-cut file itself: 15,664 raw bytes on disk4 +
        # disk3's 14,044-byte tail = 29,708 of 30,500 declared (792 never
        # written -> stays PARTIAL); content pins against the reference.
        disk3 = real_volume_bytes("disk3.img")
        controller = DiskController()
        assert controller.open_disk(
            str(real_volume_path("disk4.img")), disk_type="auto"
        )
        try:
            fs = controller.filesystem
            result = fs.attach_volume(disk3)
            assert result.volume_id == "5BIO"
            assert result.stitched_tree_ids == ("SYS5/BIN",)
            assert result.still_incomplete == []
            content = fs.read_file("/sys5/bin/cc")
            assert len(content) == 29676
            assert hashlib.sha256(content).hexdigest() == CC_STITCHED_SHA256
            # disk3's follow-on entries joined the tree's namespace
            names = {i.name for i in fs.list_directory("/sys5/bin")}
            assert {"touch", "who", "csh"} <= names
        finally:
            controller.close_disk()

    def test_real_disk9_plus_disk10(self):
        # Third real set: FT0006 COM seq 3, disk9 (section 1, EOV) ->
        # disk10 (section 2, EOF).  'ftn_sr9.2' is the EOV-cut file (FT0006
        # carries its OWN ftn_sr9.2, distinct from FT0003's); 'ftn' lives
        # entirely on disk9 and must survive the stitch untouched.  Both
        # pin against the reference stitched extraction.
        disk10 = real_volume_bytes("disk10.img")
        controller = DiskController()
        assert controller.open_disk(
            str(real_volume_path("disk9.img")), disk_type="auto"
        )
        try:
            fs = controller.filesystem
            pending = fs.pending_continuations()
            assert [(s.file_id, s.sequence, s.next_section) for s in pending] == [
                ("COM", 3, 2)
            ]
            assert pending[0].volume_id == "FT0006"
            prefix = fs.read_file("/com/ftn_sr9.2")
            assert len(prefix) == 40854 - 32  # disk9's available prefix

            result = fs.attach_volume(disk10)
            assert result.volume_id == "FT0006"
            assert result.stitched_tree_ids == ("COM",)
            assert result.still_incomplete == []
            assert fs.pending_continuations() == []

            ftn_sr = fs.read_file("/com/ftn_sr9.2")
            assert len(ftn_sr) == 424944
            assert hashlib.sha256(ftn_sr).hexdigest() == FT0006_FTN_SR92_SHA256
            ftn = fs.read_file("/com/ftn")
            assert len(ftn) == 549306
            assert hashlib.sha256(ftn).hexdigest() == FT0006_FTN_SHA256

            # the stitched COM namespace matches the reference directory
            infos = fs.list_directory("/com")
            assert {i.name for i in infos} == {"ftn", "ftn_sr9.2"}
            # 14,300 declared bytes were never written to the set: the
            # stitched file stays PARTIAL although nothing is pending
            ftn_sr_info = next(i for i in infos if i.name == "ftn_sr9.2")
            assert "PARTIAL" in ftn_sr_info.attributes
            assert "DMG" in ftn_sr_info.attributes
            assert fs.get_display_info()["Attached Volumes"] == "FT0006"
        finally:
            controller.close_disk()

    def test_behavior_unchanged_without_attach(self, disk8, caplog):
        # no attach performed: partial reads still return the available
        # prefix with a warning -- corpus sweeps and API callers see no
        # change from the attach machinery
        with caplog.at_level(logging.WARNING):
            content = disk8.filesystem.read_file("/com/ftn_sr9.2")
        assert len(content) == 130218 - 32
        assert any(
            "end of volume" in r.message and "prefix" in r.message
            for r in caplog.records
        )
