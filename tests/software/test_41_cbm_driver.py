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

    def test_infer_rejects_wrong_interior_zones(self):
        from fatfloppy.core.physical_format import PhysicalFormat, TrackFormat

        # Build a D64/35 physical format but corrupt the interior zone (tracks
        # 18-24, zone index 1) to report 21 SPT instead of the correct 19 SPT.
        # infer_from_geometry must return None because the interior zone mismatches.
        good_pf = build_physical_format("D64", 35)
        bad_track_formats = []
        for tf in good_pf.track_formats:
            if tf.track_start == 17:  # zone covering CBM tracks 18-24 (0-based 17-23)
                bad_track_formats.append(
                    TrackFormat(
                        track_start=tf.track_start,
                        track_end=tf.track_end,
                        head_start=tf.head_start,
                        head_end=tf.head_end,
                        sectors_per_track=21,  # wrong: should be 19
                        encoding=tf.encoding,
                        rate=tf.rate,
                        interleave=tf.interleave,
                        bytes_per_sector=tf.bytes_per_sector,
                        id_start=tf.id_start,
                        iam_present=tf.iam_present,
                    )
                )
            else:
                bad_track_formats.append(tf)
        bad_pf = PhysicalFormat(
            cylinders=good_pf.cylinders,
            heads=good_pf.heads,
            rpm=good_pf.rpm,
            heads_inverted=good_pf.heads_inverted,
            bytes_per_sector=good_pf.bytes_per_sector,
            track_formats=bad_track_formats,
        )
        assert CBMDiskLayout.infer_from_geometry(bad_pf) is None

        # Corrupt STRICTLY INSIDE zone 0 (tracks 1-17, 21 SPT): split into
        # track_start=0..9 (21 SPT) and track_start=10..16 (19 SPT wrong).
        # Every-track check must catch the deviation at track 11 (0-based 10).
        good_pf2 = build_physical_format("D64", 35)
        # Replace the first zone (0-based 0..16) with two sub-zones
        split_formats = []
        for tf in good_pf2.track_formats:
            if tf.track_start == 0 and tf.track_end == 16:
                split_formats.append(
                    TrackFormat(
                        track_start=0,
                        track_end=9,
                        head_start=0,
                        head_end=0,
                        sectors_per_track=21,
                        encoding=tf.encoding,
                        rate=tf.rate,
                        interleave=tf.interleave,
                        bytes_per_sector=tf.bytes_per_sector,
                        id_start=tf.id_start,
                        iam_present=tf.iam_present,
                    )
                )
                split_formats.append(
                    TrackFormat(
                        track_start=10,
                        track_end=16,
                        head_start=0,
                        head_end=0,
                        sectors_per_track=19,  # wrong: should be 21
                        encoding=tf.encoding,
                        rate=tf.rate,
                        interleave=tf.interleave,
                        bytes_per_sector=tf.bytes_per_sector,
                        id_start=tf.id_start,
                        iam_present=tf.iam_present,
                    )
                )
            else:
                split_formats.append(tf)
        bad_pf2 = PhysicalFormat(
            cylinders=good_pf2.cylinders,
            heads=good_pf2.heads,
            rpm=good_pf2.rpm,
            heads_inverted=good_pf2.heads_inverted,
            bytes_per_sector=good_pf2.bytes_per_sector,
            track_formats=split_formats,
        )
        assert CBMDiskLayout.infer_from_geometry(bad_pf2) is None

    def test_layout_for_variant_unknown_raises_value_error(self):
        with pytest.raises(ValueError):
            layout_for_variant("D64", 36)

    def test_layout_for_variant_extended_track_counts(self):
        assert layout_for_variant("D64", 40).total_sectors == 768
        assert layout_for_variant("D64", 42).total_sectors == 802

    def test_size_table_extra_entries(self):
        assert CBM_SIZE_TABLE[197376] == ("D64", 40, True)
        assert CBM_SIZE_TABLE[205312] == ("D64", 42, False)
        assert CBM_SIZE_TABLE[206114] == ("D64", 42, True)

    def test_linear_index_happy_path(self):
        layout = layout_for_variant("D64", 35)
        # sectors_before(18) == 357, sector 1 -> index 358
        assert layout.linear_index(18, 1) == 358

    def test_linear_index_out_of_bounds(self):
        layout = layout_for_variant("D64", 35)
        with pytest.raises(ValueError):
            layout.linear_index(18, 19)  # track 18 has only 19 sectors (0-18)

    def test_sectors_before_track_zero_raises(self):
        layout = layout_for_variant("D64", 35)
        with pytest.raises(ValueError):
            layout.sectors_before(0)
