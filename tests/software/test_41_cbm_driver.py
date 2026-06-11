"""Tests for the CBM image driver and shared CBM layout module."""

import pytest

from fatfloppy.core.cbm_layout import (
    CBM_SIZE_TABLE,
    CBMDiskLayout,
    build_physical_format,
    layout_for_variant,
)


class TestCBMLayout:
    def test_size_table_covers_all_variants(self):
        assert CBM_SIZE_TABLE[174848] == ("D64", 35, False)
        assert CBM_SIZE_TABLE[175531] == ("D64", 35, True)
        assert CBM_SIZE_TABLE[196608] == ("D64", 40, False)
        assert CBM_SIZE_TABLE[349696] == ("D71", 70, False)
        assert CBM_SIZE_TABLE[819200] == ("D81", 80, False)
        assert CBM_SIZE_TABLE[822400] == ("D81", 80, True)

    def test_d64_layout_geometry(self):
        layout = layout_for_variant("D64", 35)
        assert layout.variant == "1541"
        assert layout.total_sectors == 683
        assert layout.spt(1) == 21
        assert layout.spt(17) == 21
        assert layout.spt(18) == 19
        assert layout.spt(25) == 18
        assert layout.spt(31) == 17
        assert layout.sectors_before(18) == 357  # byte offset 0x16500 / 256
        assert layout.dir_track == 18
        assert layout.interleave == 10
        assert layout.reserved_tracks == (18,)

    def test_d71_layout_geometry(self):
        layout = layout_for_variant("D71", 70)
        assert layout.variant == "1571"
        assert layout.total_sectors == 1366
        assert layout.spt(36) == 21
        assert layout.spt(53) == 19
        assert layout.spt(70) == 17
        assert layout.sectors_before(53) == 0x41000 // 256
        assert layout.reserved_tracks == (18, 53)
        assert layout.interleave == 6

    def test_d81_layout_geometry(self):
        layout = layout_for_variant("D81", 80)
        assert layout.variant == "1581"
        assert layout.total_sectors == 3200
        assert layout.spt(1) == 40
        assert layout.sectors_before(40) == 0x61800 // 256
        assert layout.dir_track == 40
        assert layout.dir_sector == 3
        assert layout.max_dir_entries == 296

    def test_layout_rejects_bad_track(self):
        layout = layout_for_variant("D64", 35)
        with pytest.raises(ValueError):
            layout.spt(0)
        with pytest.raises(ValueError):
            layout.spt(36)

    def test_build_physical_format_d64(self):
        pf = build_physical_format("D64", 35)
        assert pf.cylinders == 35
        assert pf.heads == 1
        assert pf.bytes_per_sector == 256
        assert pf.get_sectors_per_track(0, 0) == 21  # CBM track 1
        assert pf.get_sectors_per_track(17, 0) == 19  # CBM track 18
        assert pf.get_sectors_per_track(34, 0) == 17  # CBM track 35
        assert pf.total_sectors == 683

    def test_build_physical_format_d81(self):
        pf = build_physical_format("D81", 80)
        assert pf.cylinders == 80
        assert pf.total_sectors == 3200

    def test_configs_match_semantics(self):
        a = layout_for_variant("D64", 35)
        b = layout_for_variant("D64", 35)
        c = layout_for_variant("D64", 40)
        assert CBMDiskLayout.matches(a, b)
        assert not CBMDiskLayout.matches(a, c)

    def test_infer_from_geometry(self):
        pf = build_physical_format("D71", 70)
        layout = CBMDiskLayout.infer_from_geometry(pf)
        assert layout is not None and layout.variant == "1571"
        pf64 = build_physical_format("D64", 35)
        assert CBMDiskLayout.infer_from_geometry(pf64).variant == "1541"
