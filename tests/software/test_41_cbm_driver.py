"""Tests for the CBM image driver and shared CBM layout module."""

import pytest

from fatfloppy.core.cbm_layout import (
    CBM_SIZE_TABLE,
    CBMDiskLayout,
    build_physical_format,
    layout_for_variant,
)
from fatfloppy.core.controller import DiskController
from fatfloppy.core.driver_factory import DriverFactory
from fatfloppy.core.drivers.cbm_image import CBMImageDriver


def make_blank_d64(tracks=35, error_block=False) -> bytes:
    layout = layout_for_variant("D64", tracks)
    data = bytearray(layout.total_sectors * 256)
    # Minimal plausible BAM at 18/0 so content probes pass where needed.
    off = layout.sectors_before(18) * 256
    data[off + 0], data[off + 1], data[off + 2] = 18, 1, 0x41
    data[off + 0x90 : off + 0xAB] = b"\xa0" * 0x1B
    if error_block:
        data += bytes([0x01]) * layout.total_sectors
    return bytes(data)


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


class TestCBMImageDriver:
    def test_open_d64_from_data_geometry(self):
        drv = CBMImageDriver("mem.d64", image_data=make_blank_d64())
        assert drv.has_embedded_geometry
        assert drv.physical_format.cylinders == 35
        assert drv.physical_format.get_sectors_per_track(0, 0) == 21

    def test_read_write_sector_round_trip(self, tmp_path):
        p = tmp_path / "t.d64"
        p.write_bytes(make_blank_d64())
        drv = CBMImageDriver(str(p))
        payload = bytes(range(256))
        drv.write_sector(17, 0, 0, payload)  # CBM 18/0
        assert drv.read_sector(17, 0, 0) == payload
        drv.flush()
        raw = p.read_bytes()
        assert raw[0x16500:0x16600] == payload

    def test_sector_bounds_checked(self):
        drv = CBMImageDriver("mem.d64", image_data=make_blank_d64())
        with pytest.raises(OSError):
            drv.read_sector(0, 0, 21)  # track 1 has 21 sectors: 0-20
        with pytest.raises(OSError):
            drv.read_sector(35, 0, 0)  # only 35 cylinders
        with pytest.raises(OSError):
            drv.read_sector(0, 1, 0)  # single-head

    def test_error_block_parsed_and_preserved(self, tmp_path):
        p = tmp_path / "err.d64"
        p.write_bytes(make_blank_d64(error_block=True))
        drv = CBMImageDriver(str(p))
        assert drv.has_error_block
        assert drv.get_sector_error(0, 0, 0) == 0x01
        drv.write_sector(0, 0, 0, bytes(256))
        drv.flush()
        assert p.stat().st_size == 175531
        assert p.read_bytes()[-683:] == bytes([0x01]) * 683

    def test_42_track_is_read_only(self):
        layout = layout_for_variant("D64", 42)
        drv = CBMImageDriver("mem.d64", image_data=bytes(layout.total_sectors * 256))
        assert drv.physical_format.cylinders == 42
        with pytest.raises(OSError):
            drv.write_sector(0, 0, 0, bytes(256))

    def test_initialize_new_image_d81(self, tmp_path):
        p = tmp_path / "new.d81"
        drv = CBMImageDriver(str(p))
        drv.initialize_new_image(build_physical_format("D81", 80))
        drv.flush()
        assert p.stat().st_size == 819200

    def test_rejects_wrong_size(self, tmp_path):
        p = tmp_path / "bad.d64"
        p.write_bytes(bytes(100000))
        with pytest.raises(ValueError):
            CBMImageDriver(str(p))

    @pytest.mark.parametrize("size", sorted(CBM_SIZE_TABLE))
    def test_open_every_size_table_entry(self, size):
        family, tracks, has_errors = CBM_SIZE_TABLE[size]
        drv = CBMImageDriver("mem.img", image_data=bytes(size))
        assert drv.physical_format.cylinders == tracks
        assert drv.has_error_block == has_errors
        layout = layout_for_variant(family, tracks)
        # Last sector addressable; error block excluded from data.
        last_track = tracks
        last_sector = layout.spt(last_track) - 1
        assert len(drv.read_sector(last_track - 1, 0, last_sector)) == 256

    def test_initialize_new_image_rejects_non_cbm_geometry(self):
        from fatfloppy.core.physical_format import PhysicalFormat, TrackFormat

        fat_pf = PhysicalFormat(
            cylinders=40,
            heads=2,
            rpm=300,
            heads_inverted=False,
            bytes_per_sector=512,
            track_formats=[
                TrackFormat(
                    track_start=0,
                    track_end=39,
                    head_start=0,
                    head_end=1,
                    sectors_per_track=9,
                    encoding="MFM",
                    rate=250,
                )
            ],
        )
        drv = CBMImageDriver("mem.img", image_data=bytes(174848))
        with pytest.raises(ValueError):
            drv.initialize_new_image(fat_pf)

    def test_initialize_new_image_rejects_42_track(self):
        drv = CBMImageDriver("mem.img", image_data=bytes(174848))
        with pytest.raises(ValueError):
            drv.initialize_new_image(build_physical_format("D64", 42))

    def test_validate_accepts_unique_size(self, tmp_path):
        p = tmp_path / "x.d64"
        p.write_bytes(bytes(174848))  # all-zero but size is CBM-unique
        drv = CBMImageDriver(str(p))
        ok, err = drv.validate_for_opening(str(p))
        assert ok, err

    def test_validate_rejects_random_196608(self, tmp_path):
        p = tmp_path / "x.img"
        p.write_bytes(bytes([0x55]) * 196608)  # plausible raw IMG, no CBM BAM
        drv = CBMImageDriver.__new__(CBMImageDriver)
        ok, _ = CBMImageDriver.validate_for_opening(drv, str(p))
        assert not ok

    def test_validate_accepts_real_40_track_bam(self, tmp_path):
        data = bytearray(196608)
        data[0x16500], data[0x16501], data[0x16502] = 18, 1, 0x41
        data[0x16500 + 0x90 : 0x16500 + 0xAB] = b"\xa0" * 0x1B
        p = tmp_path / "x.d64"
        p.write_bytes(bytes(data))
        drv = CBMImageDriver.__new__(CBMImageDriver)
        ok, err = CBMImageDriver.validate_for_opening(drv, str(p))
        assert ok, err

    def test_validate_rejects_mac_800k(self, tmp_path):
        p = tmp_path / "x.d81"
        p.write_bytes(bytes([0xAA]) * 819200)  # no 1581 header/BAM signatures
        drv = CBMImageDriver.__new__(CBMImageDriver)
        ok, _ = CBMImageDriver.validate_for_opening(drv, str(p))
        assert not ok

    def test_validate_accepts_real_d81_header(self, tmp_path):
        data = bytearray(819200)
        h = 0x61800
        data[h + 2] = 0x44
        data[h + 0x19], data[h + 0x1A] = ord("3"), ord("D")
        data[h + 0x100 + 2], data[h + 0x100 + 3] = 0x44, 0xBB  # BAM 40/1
        p = tmp_path / "x.d81"
        p.write_bytes(bytes(data))
        drv = CBMImageDriver.__new__(CBMImageDriver)
        ok, err = CBMImageDriver.validate_for_opening(drv, str(p))
        assert ok, err

    def test_validate_rejects_unknown_size(self, tmp_path):
        p = tmp_path / "x.d64"
        p.write_bytes(bytes(180000))
        drv = CBMImageDriver.__new__(CBMImageDriver)
        ok, _ = CBMImageDriver.validate_for_opening(drv, str(p))
        assert not ok

    def test_validate_rejects_random_196608_error_message(self, tmp_path):
        p = tmp_path / "x.img"
        p.write_bytes(bytes([0x55]) * 196608)
        drv = CBMImageDriver.__new__(CBMImageDriver)
        ok, err = CBMImageDriver.validate_for_opening(drv, str(p))
        assert not ok
        assert err is not None and "CBM" in err

    def test_validate_rejects_mac_800k_error_message(self, tmp_path):
        p = tmp_path / "x.d81"
        p.write_bytes(bytes([0xAA]) * 819200)
        drv = CBMImageDriver.__new__(CBMImageDriver)
        ok, err = CBMImageDriver.validate_for_opening(drv, str(p))
        assert not ok
        assert err is not None and "CBM" in err


class TestCBMAutoDetection:
    """Tests for CBM auto-detection via DriverFactory and DiskController."""

    def test_factory_picks_cbm_driver_for_d64(self, tmp_path):
        """A valid D64 image must be claimed by the CBM driver when disk_type='auto'."""
        p = tmp_path / "auto.d64"
        p.write_bytes(make_blank_d64())
        drv = DriverFactory.create("auto", source=str(p))
        assert drv.driver_type == "CBM"

    def test_controller_opens_d64_with_cbm_driver(self, tmp_path):
        """DiskController.open_disk with disk_type='auto' must select the CBM driver."""
        p = tmp_path / "auto.d64"
        p.write_bytes(make_blank_d64())
        controller = DiskController()
        result = controller.open_disk(str(p), disk_type="auto")
        assert result, "open_disk should succeed for a valid blank D64"
        assert controller.driver is not None
        assert controller.driver.driver_type == "CBM"
        controller.close_disk()

    def test_img_fallback_not_shadowed(self, tmp_path):
        """A 360 K raw IMG must not be claimed by the CBM driver."""
        p = tmp_path / "fat.img"
        p.write_bytes(bytes(368640))
        drv = DriverFactory.create("auto", source=str(p))
        assert drv.driver_type != "CBM"
