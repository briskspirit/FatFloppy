"""Apollo floppy driver tests."""

from pathlib import Path

import pytest

from fatfloppy.core.apollo_wbak import IMAGE_SIZE
from fatfloppy.core.controller import DiskController
from fatfloppy.core.driver_factory import DriverFactory
from fatfloppy.core.drivers.apollo_floppy import ApolloFloppyDriver

RESOURCES = Path(__file__).parent.parent / "resources" / "APOLLO"


class TestApolloDriver:
    def test_open_disk2_geometry(self):
        drv = ApolloFloppyDriver(str(RESOURCES / "disk2.img"))
        pf = drv.physical_format
        assert pf.cylinders == 77 and pf.heads == 2
        assert pf.get_sectors_per_track(0, 0) == 8
        assert pf.bytes_per_sector == 1024
        assert drv.read_sector(0, 0, 0)[:6] == b"APOLLO"

    def test_linear_sector_addressing(self):
        drv = ApolloFloppyDriver(str(RESOURCES / "disk2.img"))
        data = (RESOURCES / "disk2.img").read_bytes()
        # cylinder-major, head, sector: offset = ((cyl*2 + head)*8 + sector)*1024
        assert drv.read_sector(0, 1, 0) == data[8 * 1024 : 9 * 1024]
        assert drv.read_sector(1, 0, 0) == data[16 * 1024 : 17 * 1024]
        assert drv.read_sector(76, 1, 7) == data[-1024:]

    def test_read_only(self):
        drv = ApolloFloppyDriver(str(RESOURCES / "disk2.img"))
        with pytest.raises(OSError, match="read-only"):
            drv.write_sector(0, 0, 0, bytes(1024))
        assert drv.supports_new_image_creation is False
        assert drv.supports_in_place_formatting is False
        with pytest.raises(NotImplementedError):
            drv.initialize_new_image(None)

    def test_geometry_builder_is_shared(self):
        # The driver and the format profile must use the SAME geometry
        # builder (single source of truth in core.apollo_wbak).
        from fatfloppy.core.apollo_wbak import build_apollo_physical_format
        from fatfloppy.core.filesystems.formats.apollo_formats import APOLLO_FORMATS

        canonical = build_apollo_physical_format()
        drv = ApolloFloppyDriver(str(RESOURCES / "disk2.img"))
        assert drv.physical_format == canonical
        assert APOLLO_FORMATS["apollo_1.2m_wbak"].physical_format == canonical

    def test_bounds(self):
        drv = ApolloFloppyDriver(str(RESOURCES / "disk2.img"))
        with pytest.raises(OSError):
            drv.read_sector(0, 0, 8)
        with pytest.raises(OSError):
            drv.read_sector(0, 2, 0)
        with pytest.raises(OSError):
            drv.read_sector(77, 0, 0)

    def test_validate_for_opening(self, tmp_path):
        ok, _ = ApolloFloppyDriver.__new__(ApolloFloppyDriver).validate_for_opening(
            str(RESOURCES / "disk2.img")
        )
        assert ok
        p = tmp_path / "wrongmagic.img"
        p.write_bytes(bytes(IMAGE_SIZE))
        drv = ApolloFloppyDriver.__new__(ApolloFloppyDriver)
        ok, err = drv.validate_for_opening(str(p))
        assert not ok and "APOLLO" in err
        p2 = tmp_path / "wrongsize.img"
        p2.write_bytes(b"APOLLO" + bytes(1000))
        ok, _ = drv.validate_for_opening(str(p2))
        assert not ok

    def test_rejects_wrong_size_on_open(self, tmp_path):
        p = tmp_path / "bad.img"
        p.write_bytes(b"APOLLO" + bytes(1000))
        with pytest.raises(ValueError):
            ApolloFloppyDriver(str(p))


class TestApolloAutoDetect:
    def test_factory_picks_apollo(self):
        drv = DriverFactory.create("auto", source=str(RESOURCES / "disk2.img"))
        assert drv.driver_type == "APOLLO"

    def test_no_shadowing_of_plain_img(self, tmp_path):
        p = tmp_path / "plain.img"
        p.write_bytes(bytes(IMAGE_SIZE))  # right size, no magic
        drv = DriverFactory.create("auto", source=str(p))
        assert drv.driver_type != "APOLLO"

    def test_controller_opens_disk8(self, tmp_path):
        import shutil

        p = tmp_path / "d8.img"
        shutil.copy(RESOURCES / "disk8.img", p)
        controller = DiskController()
        assert controller.open_disk(str(p), disk_type="auto")
        assert controller.driver.driver_type == "APOLLO"
        controller.close_disk()


class TestApolloFormatProfile:
    def test_apollo_formats_importable(self):
        from fatfloppy.core.filesystems.formats.apollo_formats import APOLLO_FORMATS

        assert "apollo_1.2m_wbak" in APOLLO_FORMATS

    def test_profile_geometry(self):
        from fatfloppy.core.filesystems.formats.apollo_formats import APOLLO_FORMATS

        profile = APOLLO_FORMATS["apollo_1.2m_wbak"]
        pf = profile.physical_format
        assert pf.cylinders == 77
        assert pf.heads == 2
        assert pf.get_sectors_per_track(0, 0) == 8
        assert pf.bytes_per_sector == 1024
        assert pf.rpm == 360
        assert profile.filesystem_config is None
