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
    WbakCatalog,
    WbakEntry,
    build_catalog,
)
from fatfloppy.core.controller import DiskController
from fatfloppy.core.filesystems.apollo_wbak_fs import (
    ApolloWbakConfig,
    ApolloWbakFilesystem,
)

from .test_49_apollo_wbak_parser import (
    SYN_UID,
    StreamBuilder,
    build_block,
    data_rec,
    file_rec,
    label80,
    mark_rec,
    storage_header,
    sub_rec,
)

RESOURCES = Path(__file__).parent.parent / "resources" / "APOLLO"
CBM_RESOURCES = Path(__file__).parent.parent / "resources" / "CBM"

CLEANUP_SHA256 = "3e4f699d17b9b08936e1f1d501bec3c3a56c4d2e978eb64134a32ac4186539e1"


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
        # 37 direct children (33 files incl. the "?" placeholder, the net
        # dir, 3 links) + the synthesized "templates" intermediate (its
        # entries appear under templates/ but no DIR record names it here)
        assert len(infos) == 38
        names = {i.name for i in infos}
        assert {"releaselog", "motd", "rc", "net", "templates", "?"} <= names
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

    def test_disk8_root_merges_trees(self, disk8):
        names = {i.name for i in disk8.filesystem.list_directory("/")}
        assert names == {"install", "com"}

    def test_disk8_partial_file_attribute(self, disk8):
        infos = disk8.filesystem.list_directory("/com")
        assert [i.name for i in infos] == ["?", "ftn_sr9.2"]
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
        # 0x20202000 (ASCII spaces) bytes; the recoverable-content figure
        # must count recovered content, not declared sizes, or the info
        # panel shows 514 MB on a 1.2 MB floppy.
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
        assert fs.read_file("/treeq/?") == b"one"
        assert fs.read_file("/treeq/?~1") == b"two"
        controller.close_disk()
