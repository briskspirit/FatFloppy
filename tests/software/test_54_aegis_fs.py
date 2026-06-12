"""ApolloAegisFilesystem tests: listing, reading, read-only gate, detection,
disk map, display info and the BAT-reconciling check() over the real disk5
resource (plus synthetic volumes from the test_53 builder).

All literals are pinned from the empirical dissection of disk5
(docs/superpowers/research/apollo/aegis_empirical/) and from the established
test_53 pins (root names UPPERCASE, /SYS/DM/DM 242,406 bytes, SYSBOOT ==
physical blocks 2..11, oracle sha256 values).
"""

import datetime
import hashlib
import logging
import random
import struct
from pathlib import Path

import pytest

from fatfloppy.core.apollo_wbak import IMAGE_SIZE
from fatfloppy.core.controller import DiskController
from fatfloppy.core.filesystem_registry import FilesystemRegistry
from fatfloppy.core.filesystems.apollo_aegis_fs import (
    AegisConfig,
    ApolloAegisFilesystem,
)
from fatfloppy.core.filesystems.apollo_wbak_fs import ApolloWbakFilesystem

from .test_53_aegis_parser import AegisVolumeBuilder

RESOURCES = Path(__file__).parent.parent / "resources" / "APOLLO"
FAT_RESOURCES = Path(__file__).parent.parent / "resources"
DISK5 = RESOURCES / "disk5.img"

# test_53 oracle values (header-stripped for managed uasc types)
STARTUP_SPM_SHA256 = "e978b375bc37f872936ffad482c40c539b8588cf5f5f3f9d5bcc5c5316ea3f03"
DM_SHA256 = "dba56a943f2bff3dba8b418681e08be39e8464f62545ab6ea7f73fd38625544c"

# disk5 empirical pins (out_01..out_04 + the BAT reconciliation):
#   BAT block abs 617 (LV 0x268), 1220 bits covered from LV 0xB, 217 free;
#   root dir page abs 618, network-root dir page abs 619;
#   /SYS/DM/DM's L1 index block LV 0x2DE -> abs 735;
#   alternate LV label abs 1231 (PV alt_lv_list[1] = 0x4CF).
TOTAL_SECTORS = 1232
BAT_FREE = 217
BAT_COVERED = 1220
MAP_SUMS = {"system": 38, "directory": 17, "file": 960, "free": 217, "damaged": 0}


def open_image(tmp_path: Path, data: bytes, name: str = "vol.img") -> DiskController:
    path = tmp_path / name
    path.write_bytes(data)
    controller = DiskController()
    assert controller.open_disk(str(path), disk_type="auto")
    return controller


@pytest.fixture(scope="module")
def disk5():
    controller = DiskController()
    assert controller.open_disk(str(DISK5), disk_type="auto")
    yield controller
    controller.close_disk()


@pytest.fixture(scope="module")
def disk5_bytes() -> bytes:
    return DISK5.read_bytes()


def build_hostile_aegis() -> bytes:
    """APOLLO magic at offset 2 + random garbage: must never be claimed."""
    rng = random.Random(0xAE615)
    img = bytearray(b"\x00\x00APOLLO")
    img += bytes(rng.randrange(256) for _ in range(IMAGE_SIZE - len(img)))
    return bytes(img)


class TestListing:
    def test_disk5_claims_aegis(self, disk5):
        fs = disk5.filesystem
        assert fs is not None
        assert fs.filesystem_type == "APOLLO_AEGIS"

    def test_root_names_verbatim_uppercase(self, disk5):
        infos = disk5.filesystem.list_directory("/")
        assert [i.name for i in infos] == [
            "SYSBOOT",
            "SYS",
            "COM",
            "BSCOM",
            "DOMAIN_EXAMPLES",
        ]
        sysboot = infos[0]
        assert not sysboot.is_dir
        assert sysboot.attributes == "SYSBOOT"
        assert sysboot.size == 10240
        sys_dir = infos[1]
        assert sys_dir.is_dir
        assert sys_dir.attributes == "DIR"
        assert sys_dir.size == 0

    def test_sys_dm_walk(self, disk5):
        infos = disk5.filesystem.list_directory("/SYS/DM")
        names = {i.name for i in infos}
        assert {"DM", "FONTS", "COLOR_MAP", "STD_KEYS", "STARTUP_TEMPLATES"} <= names
        dm = next(i for i in infos if i.name == "DM")
        assert dm.size == 242406  # test_53 pin: obj type, raw VTOCE length
        assert dm.attributes == "OBJ"
        std_keys = next(i for i in infos if i.name == "STD_KEYS")
        assert std_keys.attributes == "REC"
        color_map = next(i for i in infos if i.name == "COLOR_MAP")
        assert color_map.attributes == "TEXT"

    def test_dtm_datetimes_naive_utc_1985(self, disk5):
        infos = disk5.filesystem.list_directory("/")
        sysboot = next(i for i in infos if i.name == "SYSBOOT")
        # VTOCE 0x2670 dtm (out_02_vtoc.txt): 1985-11-25 14:21:31 UTC
        assert sysboot.datetime.tzinfo is None  # naive UTC, house convention
        assert sysboot.datetime.replace(microsecond=0) == datetime.datetime(
            1985, 11, 25, 14, 21, 31
        )

    def test_managed_file_size_excludes_storage_header(self, disk5):
        infos = disk5.filesystem.list_directory("/SYS/SPM/STARTUP_TEMPLATES")
        spm = next(i for i in infos if i.name == "STARTUP.SPM")
        assert spm.size == 1735 - 32  # uasc: VTOCE length minus storage header
        assert spm.attributes == "TEXT"

    def test_extra_data_per_spec(self, disk5):
        infos = disk5.filesystem.list_directory("/")
        sysboot = next(i for i in infos if i.name == "SYSBOOT")
        extra = sysboot.extra_data
        assert extra["uid"] == "2A58CCF7.C0002FC2"  # test_53 pin
        assert extra["parent_uid"] == "2A58CE0A.D0002FC2"
        assert extra["kind"] == 0
        assert extra["type_uid"].startswith("00000315.")
        assert extra["created"].year == 1985  # from the UID's high 32 bits
        assert extra["dtu"].year == 1985
        assert "acl_uid" in extra

    def test_case_insensitive_fallback_and_slash_tolerance(self, disk5):
        infos = disk5.filesystem.list_directory("/sys/dm/")
        assert {i.name for i in infos} >= {"DM", "FONTS"}

    def test_missing_directory_raises(self, disk5):
        with pytest.raises(FileNotFoundError):
            disk5.filesystem.list_directory("/NO/SUCH/DIR")

    def test_listing_a_file_raises(self, disk5):
        with pytest.raises(NotADirectoryError):
            disk5.filesystem.list_directory("/SYSBOOT")


class TestRead:
    def test_sysboot_is_physical_blocks_2_to_11(self, disk5, disk5_bytes):
        # test_53 pin: SYSBOOT == abs blocks 2..11 (LV daddrs 1..0xA)
        assert disk5.filesystem.read_file("/SYSBOOT") == disk5_bytes[2048:12288]

    def test_uasc_file_matches_oracle_hash(self, disk5):
        content = disk5.filesystem.read_file("/SYS/SPM/STARTUP_TEMPLATES/STARTUP.SPM")
        assert len(content) == 1735 - 32
        assert hashlib.sha256(content).hexdigest() == STARTUP_SPM_SHA256

    def test_l1_indirect_file_matches_oracle_hash(self, disk5):
        content = disk5.filesystem.read_file("/SYS/DM/DM")
        assert len(content) == 242406
        assert hashlib.sha256(content).hexdigest() == DM_SHA256

    def test_directory_read_raises(self, disk5):
        with pytest.raises(IsADirectoryError):
            disk5.filesystem.read_file("/SYS")
        with pytest.raises(IsADirectoryError):
            disk5.filesystem.read_file("/")

    def test_missing_file_raises(self, disk5):
        with pytest.raises(FileNotFoundError):
            disk5.filesystem.read_file("/SYS/NONEXISTENT")


class TestReadOnly:
    @pytest.mark.parametrize(
        "call",
        [
            lambda fs: fs.write_file("/SYS/NEW", b"x"),
            lambda fs: fs.delete("/SYSBOOT"),
            lambda fs: fs.delete_recursive("/SYS"),
            lambda fs: fs.create_directory("/NEWDIR"),
            lambda fs: fs.format_fs(None),
        ],
    )
    def test_mutators_raise_read_only(self, disk5, call):
        with pytest.raises(OSError, match="read-only"):
            call(disk5.filesystem)

    def test_name_hint(self, disk5):
        assert disk5.filesystem.name_hint() == "read-only volume"


class TestDetection:
    def test_disk5_detects_aegis_profile(self, disk5):
        format_name, _cfg, _pf = disk5.detect_format()
        assert format_name == "apollo_1.2m_aegis"
        assert disk5.filesystem.get_volume_label() == "FLPB.SR9"
        config = disk5.filesystem.get_specific_config()
        assert isinstance(config, AegisConfig)
        assert config.volume_name == "FLPB.SR9"
        assert config.lv_uid == "2A58CCF6.B0002FC2"

    def test_wbak_resources_still_detect_wbak(self):
        for name in ("disk2.img", "disk8.img"):
            controller = DiskController()
            assert controller.open_disk(str(RESOURCES / name), disk_type="auto")
            assert controller.filesystem.filesystem_type == "APOLLO_WBAK"
            format_name, _cfg, _pf = controller.detect_format()
            assert format_name == "apollo_1.2m_wbak"
            controller.close_disk()

    def test_validity_cross_matrix_both_directions(self, disk5):
        # AEGIS fs on a wbak volume: the PV magic sits at offset 0, not 2,
        # so not a single point is awarded.
        controller = DiskController()
        assert controller.open_disk(str(RESOURCES / "disk2.img"), disk_type="auto")
        try:
            aegis_on_wbak = ApolloAegisFilesystem(controller.disk)
            assert aegis_on_wbak.get_validity_score() == 0
        finally:
            controller.close_disk()
        # wbak fs on the AEGIS volume: its +25 magic check is offset-0-only
        # (apollo_wbak_fs.get_validity_score), so disk5 (00 00 APOLLO) scores
        # 0 -- NOT the 25 the design-time matrix sketched.  Stricter mutual
        # exclusion than specified; pinned at the implemented value.
        wbak_on_aegis = ApolloWbakFilesystem(disk5.disk)
        assert wbak_on_aegis.get_validity_score() == 0

    def test_disk5_score_is_full(self, disk5):
        fs = ApolloAegisFilesystem(disk5.disk)
        assert fs.get_validity_score() == 100  # 25 magic + 25 LV + 50 VTOC/root

    def test_hostile_magic_at_2_not_claimed(self, tmp_path):
        controller = open_image(tmp_path, build_hostile_aegis(), "hostile.img")
        try:
            assert controller.driver.driver_type == "APOLLO"
            assert controller.filesystem is None  # nothing claims garbage
            fs = ApolloAegisFilesystem(controller.disk)
            score = fs.get_validity_score()
            assert score < fs.validity_threshold
            assert score == 25  # PV magic only; garbage LV label is not sane
        finally:
            controller.close_disk()

    def test_sr10_version_guard(self, tmp_path, disk5_bytes, caplog):
        # apollofs (domainos-archeology) pkg/fs/logical_volume.go: LV label
        # version 0 = pre-SR10, 1 = SR10 (different VTOCE format).  A
        # version-1 copy of disk5 must be capped below the threshold.
        sr10 = bytearray(disk5_bytes)
        struct.pack_into(">H", sr10, 1024, 1)  # LV label +0: version
        controller = open_image(tmp_path, bytes(sr10), "sr10.img")
        try:
            fs = ApolloAegisFilesystem(controller.disk)
            with caplog.at_level(logging.WARNING):
                score = fs.get_validity_score()
            assert score < fs.validity_threshold
            assert any("SR10" in r.message for r in caplog.records)
            assert controller.filesystem is None  # never claimed at open
        finally:
            controller.close_disk()

    def test_fat12_resource_unchanged(self):
        controller = DiskController()
        assert controller.open_disk(
            str(FAT_RESOURCES / "populated_read_test_360k.img"), disk_type="auto"
        )
        assert controller.filesystem.filesystem_type == "FAT12"
        controller.close_disk()

    def test_profiles_registered_once_per_plugin(self):
        # Each Apollo filesystem registers ONLY its own profile slice; the
        # registry sees both exactly once.
        assert set(ApolloWbakFilesystem.get_format_definitions()) == {
            "apollo_1.2m_wbak"
        }
        assert set(ApolloAegisFilesystem.get_format_definitions()) == {
            "apollo_1.2m_aegis"
        }
        all_formats = FilesystemRegistry.get_all_formats()
        assert "apollo_1.2m_wbak" in all_formats
        assert "apollo_1.2m_aegis" in all_formats
        controller = DiskController()
        names = [name for name, _desc in controller.list_formats()]
        assert names.count("apollo_1.2m_aegis") == 1
        assert names.count("apollo_1.2m_wbak") == 1

    def test_configs_match_sentinel_semantics(self):
        # The shipped profile carries the lv_uid=None sentinel: LV UIDs are
        # per-disk, not per-format, so the sentinel matches any parsed volume.
        assert ApolloAegisFilesystem.configs_match(
            AegisConfig(), AegisConfig(volume_name="FLPB.SR9", lv_uid="X.Y")
        )
        assert ApolloAegisFilesystem.configs_match(
            AegisConfig(lv_uid="A"), AegisConfig(lv_uid="A")
        )
        assert not ApolloAegisFilesystem.configs_match(
            AegisConfig(lv_uid="A"), AegisConfig(lv_uid="B")
        )
        assert not ApolloAegisFilesystem.configs_match(AegisConfig(), object())

    def test_create_config_from_params_returns_none(self):
        assert ApolloAegisFilesystem.create_config_from_params({}, None) is None


class TestMapDisplayCheck:
    def test_disk_map_five_types_and_sums(self, disk5):
        layout = disk5.filesystem.get_disk_map_layout()
        assert len(layout["legend"]) == 5
        assert layout["allocation_unit_size_sectors"] == 1
        assert layout["first_data_sector"] == 0
        assert len(layout["type_color_map"]) == 5
        get_type = layout["get_sector_type"]
        sums = {"system": 0, "directory": 0, "file": 0, "free": 0, "damaged": 0}
        for lba in range(TOTAL_SECTORS):
            sums[get_type(lba)] += 1
        assert sums == MAP_SUMS
        assert sum(sums.values()) == TOTAL_SECTORS

    def test_disk_map_spot_classification(self, disk5):
        get_type = disk5.filesystem.get_disk_map_layout()["get_sector_type"]
        assert get_type(0) == "system"  # PV label
        assert get_type(1) == "system"  # LV label
        for lba in range(2, 12):  # SYSBOOT pages (file, incl. boot area)
            assert get_type(lba) == "file"
        assert get_type(617) == "system"  # BAT block (LV 0x268)
        assert get_type(618) == "directory"  # volume entry dir page
        assert get_type(619) == "directory"  # network root dir page
        assert get_type(1231) == "system"  # alternate LV label

    def test_dm_l1_index_block_is_system(self, disk5):
        # The L1 index block of /SYS/DM/DM is an allocated volume block that
        # node.page_daddrs deliberately omits; it must be classified system.
        fs = disk5.filesystem
        node = fs._catalog.lookup("/SYS/DM/DM")
        l1_abs = node.vtoce.l1_daddr + fs._catalog.lv_base
        assert l1_abs == 735  # LV 0x2DE, empirical pin
        get_type = fs.get_disk_map_layout()["get_sector_type"]
        assert get_type(l1_abs) == "system"
        assert l1_abs not in fs.get_file_allocation_units("/SYS/DM/DM")

    def test_allocation_unit_size_for_gui_space_panel(self, disk5):
        # The GUI space panel divides by filesystem.allocation_unit_size
        # (disk_manager.update_space_info); without it the disk map shows
        # everything free (the CBM regression, test_26).
        assert disk5.filesystem.allocation_unit_size == 1024

    def test_free_space_is_disk_capacity(self, disk5):
        # Read-only: 0 importable bytes; total = medium capacity (the wbak
        # precedent).  The BAT free count is a display-info statistic.
        assert disk5.filesystem.get_free_space() == (0, IMAGE_SIZE)

    def test_allocated_units(self, disk5):
        units = disk5.filesystem.get_allocated_units()
        assert units == sorted(set(units))
        assert len(units) == TOTAL_SECTORS - BAT_FREE  # 1015 in-use blocks
        assert {0, 1, 617, 618, 735, 1231} <= set(units)
        get_type = disk5.filesystem.get_disk_map_layout()["get_sector_type"]
        assert all(get_type(lba) != "free" for lba in units)

    def test_file_allocation_units(self, disk5):
        fs = disk5.filesystem
        assert fs.get_file_allocation_units("/SYSBOOT") == list(range(2, 12))
        units = fs.get_file_allocation_units("/SYS/DM/DM")
        assert len(units) == 237  # 32 direct + 205 L1 pages (index block apart)
        assert set(units) <= set(fs.get_allocated_units())
        assert fs.get_file_allocation_units("/SYS") == []  # directory
        assert fs.get_file_allocation_units("/NOPE") == []

    def test_display_info(self, disk5):
        info = disk5.filesystem.get_display_info()
        assert info["Filesystem"] == "Apollo AEGIS (read-only)"
        assert info["Volume"] == "FLPB.SR9"
        assert info["Node"] == "NODE_2FC2"
        assert info["PV UID"] == "2A58CC30.A0002FC2"
        assert info["LV UID"] == "2A58CCF6.B0002FC2"
        assert info["Label Written"] == "1985-11-25 14:25:45 UTC"
        assert info["Mounted"] == "1985-11-25 14:21:16 UTC"
        assert info["Dismounted"] == "1985-11-25 14:25:45 UTC"
        assert info["Files"] == "52"
        assert info["Directories"] == "17"
        assert info["ACL Objects"] == "11"
        assert info["Unreferenced Objects"] == "0"
        assert info["VTOC"] == "2 buckets, 17 blocks"
        assert info["Free Blocks (BAT)"] == str(BAT_FREE)
        assert info["Read-Only"] == "yes"

    def test_check_true_with_zero_bat_mismatches(self, disk5, caplog):
        with caplog.at_level(logging.INFO):
            assert disk5.filesystem.check() is True
        assert any(
            "0 mismatches" in r.message and str(BAT_COVERED) in r.message
            for r in caplog.records
        )

    def test_check_small_bat_corruption_warns_but_passes(
        self, tmp_path, disk5_bytes, caplog
    ):
        # Flip 8 BAT bits (<= 1% of 1220 covered): mismatches are logged,
        # check() stays True (the pinned rule).
        img = bytearray(disk5_bytes)
        img[617 * 1024] ^= 0xFF  # bits 0..7 of the bitmap (LV 0xB..0x12)
        controller = open_image(tmp_path, bytes(img), "smallbat.img")
        try:
            assert controller.filesystem.filesystem_type == "APOLLO_AEGIS"
            with caplog.at_level(logging.WARNING):
                assert controller.filesystem.check() is True
            assert any("8 mismatches" in r.message for r in caplog.records)
        finally:
            controller.close_disk()

    def test_check_large_bat_corruption_fails(self, tmp_path, disk5_bytes):
        # Flip 64 BAT bits (> 1% of 1220 covered): check() returns False.
        img = bytearray(disk5_bytes)
        for offset in range(8):
            img[617 * 1024 + offset] ^= 0xFF
        controller = open_image(tmp_path, bytes(img), "bigbat.img")
        try:
            assert controller.filesystem.filesystem_type == "APOLLO_AEGIS"
            assert controller.filesystem.check() is False
        finally:
            controller.close_disk()

    def test_corrupt_bat_shows_damaged_sectors(self, tmp_path, disk5_bytes):
        # Bits flipped free->allocated with no owner must surface as
        # "damaged" in the disk map (allocated-but-unaccounted).  BAT byte
        # 48 reads 0xFF on disk5 (8 free bits = LV 0x1A3..0x1AA, abs
        # 420..427: LSB-first within big-endian 32-bit words, so raw byte
        # 48 -- the MSB of word 12 -- carries bits 24..31 of that word).
        img = bytearray(disk5_bytes)
        assert img[617 * 1024 + 48] == 0xFF  # all 8 covered blocks are free
        img[617 * 1024 + 48] = 0x00  # now allocated, with no owner
        controller = open_image(tmp_path, bytes(img), "dmgmap.img")
        try:
            get_type = controller.filesystem.get_disk_map_layout()["get_sector_type"]
            damaged = [
                lba for lba in range(TOTAL_SECTORS) if get_type(lba) == "damaged"
            ]
            assert damaged == list(range(420, 428))
        finally:
            controller.close_disk()

    def test_synthetic_l2_volume_checks_clean(self, tmp_path):
        # An L2-mapped file's index blocks (the L2 block AND the L1 blocks
        # it points to) are allocated by the builder's BAT; check() must
        # account every one of them or mismatches appear.
        content = b"\xa5" * 350_000  # 342 pages: 32 direct + 256 L1 + 54 L2
        img = AegisVolumeBuilder().with_file("huge", content=content).build()
        controller = open_image(tmp_path, img, "l2vol.img")
        try:
            fs = controller.filesystem
            assert fs.filesystem_type == "APOLLO_AEGIS"
            assert fs.check() is True
            assert fs.read_file("/huge") == content
            # disk map: every sector classified, sums == total
            get_type = fs.get_disk_map_layout()["get_sector_type"]
            sums = {"system": 0, "directory": 0, "file": 0, "free": 0, "damaged": 0}
            for lba in range(TOTAL_SECTORS):
                sums[get_type(lba)] += 1
            assert sum(sums.values()) == TOTAL_SECTORS
            assert sums["damaged"] == 0
            assert sums["file"] == 342
        finally:
            controller.close_disk()

    def test_check_structural_failure_returns_false(self, tmp_path, disk5_bytes):
        # Destroy the VTOC map (no buckets): _initialize fails structurally.
        img = bytearray(disk5_bytes)
        struct.pack_into(">H", img, 1024 + 0x4E, 0)  # vtoc bucket count = 0
        path = tmp_path / "novtoc.img"
        path.write_bytes(bytes(img))
        controller = DiskController()
        assert controller.open_disk(str(path), disk_type="auto")
        try:
            fs = ApolloAegisFilesystem(controller.disk)
            assert fs.check() is False
        finally:
            controller.close_disk()
