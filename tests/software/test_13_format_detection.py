"""
Tests for format_detection.py and format_profile.py modules.

This module tests format detection strategies, profile management,
and the detector factory.
"""

import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from fatfloppy.core.detector_registry import DetectorRegistry
from fatfloppy.core.filesystem_registry import FilesystemRegistry
from fatfloppy.core.filesystems.fat12_fs import (
    FATFilesystem,
    FATVolumeInfo,
)
from fatfloppy.core.format_detection import (
    FormatDetector,
    MetadataBasedDetector,
    create_format_detector,
)
from fatfloppy.core.format_profile import FormatProfile
from fatfloppy.core.physical_format import PhysicalFormat, TrackFormat

_ALL_FORMATS = FilesystemRegistry.get_all_formats()
FMT_144 = _ALL_FORMATS["ibm_3.5_1.44m"]
FMT_720 = _ALL_FORMATS["ibm_3.5_720k"]


class ConcreteDetector(FormatDetector):
    """Concrete detector for testing abstract base class."""

    detector_for_driver = "test_driver"

    def detect(self):
        """Detect format (returns None for testing)."""
        return None, None, None


class ConcreteMetadataDetector(MetadataBasedDetector):
    """Concrete implementation for testing."""

    detector_for_driver = "test_metadata"


def test_format_profile_basic_instantiation() -> None:
    """Tests basic FormatProfile creation."""
    profile = FormatProfile(
        name="test_profile",
        description="Test Format",
        physical_format=FMT_144.physical_format,
    )

    assert profile.name == "test_profile"
    assert profile.description == "Test Format"
    assert profile.physical_format == FMT_144.physical_format
    assert profile.filesystem_config is None
    assert profile.is_bootable is False
    assert profile.notes is None


def test_format_profile_with_all_fields() -> None:
    """Tests FormatProfile with all optional fields."""
    config = FATVolumeInfo()

    profile = FormatProfile(
        name="full_profile",
        description="Full Test",
        physical_format=FMT_144.physical_format,
        filesystem_config=config,
        is_bootable=True,
        notes="Test notes",
    )

    assert profile.filesystem_config is config
    assert profile.is_bootable is True
    assert profile.notes == "Test notes"


def test_format_profile_get_filesystem_type_with_fat12() -> None:
    """Tests get_filesystem_type with FAT12 config."""
    profile = FormatProfile(
        name="fat12_test",
        description="FAT12 Test",
        physical_format=FMT_144.physical_format,
        filesystem_config=FATVolumeInfo(),
    )

    fs_type = profile.get_filesystem_type()
    assert fs_type == "FAT12"


def test_format_profile_get_filesystem_type_none() -> None:
    """Tests get_filesystem_type when no config."""
    profile = FormatProfile(
        name="no_fs",
        description="No Filesystem",
        physical_format=FMT_144.physical_format,
        filesystem_config=None,
    )

    fs_type = profile.get_filesystem_type()
    assert fs_type is None


def test_format_profile_get_filesystem_type_unknown_config() -> None:
    """Tests get_filesystem_type with unknown config type."""
    unknown_config = object()

    profile = FormatProfile(
        name="unknown",
        description="Unknown FS",
        physical_format=FMT_144.physical_format,
        filesystem_config=unknown_config,
    )

    fs_type = profile.get_filesystem_type()
    assert fs_type is None


def test_format_detector_initialization() -> None:
    """Tests FormatDetector initialization with required fields."""
    mock_disk = MagicMock()
    mock_driver = MagicMock()
    known_formats: dict[str, Any] = {"test": FMT_144}

    detector = ConcreteDetector(mock_disk, mock_driver, known_formats)

    assert detector.disk is mock_disk
    assert detector.driver is mock_driver
    assert detector.known_formats == known_formats


def test_format_detector_missing_detector_for_driver() -> None:
    """Tests FormatDetector validation fails without detector_for_driver."""

    class BadDetector(FormatDetector):
        def detect(self):
            return None, None, None

    with pytest.raises(ValueError, match="must define detector_for_driver"):
        BadDetector(MagicMock(), MagicMock(), {})


@pytest.fixture
def metadata_detector():
    """Provides a MetadataBasedDetector instance for testing."""
    mock_disk = MagicMock()
    mock_driver = MagicMock()
    mock_driver.physical_format = FMT_144.physical_format
    known_formats = {"ibm_3.5_1.44m": FMT_144, "ibm_3.5_720k": FMT_720}

    detector = ConcreteMetadataDetector(mock_disk, mock_driver, known_formats)
    return detector


def test_metadata_detector_no_physical_format(metadata_detector) -> None:
    """Tests detect when driver has no physical format."""
    metadata_detector.driver.physical_format = None

    result = metadata_detector.detect()
    assert result == (None, None, None)


def test_metadata_detector_no_filesystem(metadata_detector) -> None:
    """Tests detect when filesystem parsing fails."""
    with patch.object(metadata_detector, "_parse_filesystem", return_value=None):
        result = metadata_detector.detect()

        assert result[0] is None
        assert result[1] is None
        assert result[2] == FMT_144.physical_format


def test_metadata_detector_with_filesystem_no_match(metadata_detector) -> None:
    """Tests detect with parsed filesystem but no profile match."""
    mock_config = FATVolumeInfo()

    with (
        patch.object(metadata_detector, "_parse_filesystem", return_value=mock_config),
        patch.object(metadata_detector, "_match_to_known_profile", return_value=None),
    ):
        result = metadata_detector.detect()

        assert result[0] is None
        assert result[1] is mock_config
        assert result[2] == FMT_144.physical_format


def test_metadata_detector_complete_match(metadata_detector) -> None:
    """Tests detect with full profile match."""
    mock_config = FATVolumeInfo()

    with (
        patch.object(metadata_detector, "_parse_filesystem", return_value=mock_config),
        patch.object(
            metadata_detector, "_match_to_known_profile", return_value="ibm_3.5_1.44m"
        ),
    ):
        result = metadata_detector.detect()

        assert result[0] == "ibm_3.5_1.44m"
        assert result[1] is mock_config
        assert result[2] == FMT_144.physical_format


def test_parse_filesystem_success(metadata_detector) -> None:
    """Tests _parse_filesystem with valid filesystem."""
    mock_fs = MagicMock(spec=FATFilesystem)
    mock_fs.get_validity_score.return_value = 100
    mock_fs.validity_threshold = 30
    mock_fs.get_specific_config.return_value = FATVolumeInfo()

    with patch(
        "fatfloppy.core.format_detection.create_filesystem", return_value=mock_fs
    ):
        config = metadata_detector._parse_filesystem()

        assert config is not None
        assert isinstance(config, FATVolumeInfo)


def test_parse_filesystem_low_validity_score(metadata_detector) -> None:
    """Tests _parse_filesystem with low validity score."""
    mock_fs = MagicMock()
    mock_fs.get_validity_score.return_value = 10
    mock_fs.validity_threshold = 30

    with patch(
        "fatfloppy.core.format_detection.create_filesystem", return_value=mock_fs
    ):
        config = metadata_detector._parse_filesystem()

        assert config is None


def test_parse_filesystem_exception(metadata_detector) -> None:
    """Tests _parse_filesystem when filesystem creation raises."""
    with patch(
        "fatfloppy.core.format_detection.create_filesystem",
        side_effect=RuntimeError("FS creation failed"),
    ):
        config = metadata_detector._parse_filesystem()

        assert config is None


def test_parse_filesystem_with_volume_application(metadata_detector) -> None:
    """Tests _parse_filesystem calls apply_volume_to_driver if available."""
    mock_fs = MagicMock()
    mock_fs.get_validity_score.return_value = 100
    mock_fs.validity_threshold = 30
    mock_fs.get_specific_config.return_value = FATVolumeInfo()
    mock_fs.apply_volume_to_driver = MagicMock()

    with patch(
        "fatfloppy.core.format_detection.create_filesystem", return_value=mock_fs
    ):
        config = metadata_detector._parse_filesystem()

        mock_fs.apply_volume_to_driver.assert_called_once()
        assert config is not None


def test_parse_filesystem_volume_application_fails(metadata_detector) -> None:
    """Tests _parse_filesystem continues if apply_volume_to_driver fails."""
    mock_fs = MagicMock()
    mock_fs.get_validity_score.return_value = 100
    mock_fs.validity_threshold = 30
    mock_fs.get_specific_config.return_value = FATVolumeInfo()
    mock_fs.apply_volume_to_driver.side_effect = RuntimeError("Apply failed")

    with patch(
        "fatfloppy.core.format_detection.create_filesystem", return_value=mock_fs
    ):
        config = metadata_detector._parse_filesystem()

        assert config is not None


def test_physical_formats_match_identical(metadata_detector) -> None:
    """Tests _physical_formats_match with identical formats."""
    format1 = FMT_144.physical_format
    format2 = FMT_144.physical_format

    assert metadata_detector._physical_formats_match(format1, format2) is True


def test_physical_formats_match_different_cylinders(metadata_detector) -> None:
    """Tests _physical_formats_match with different cylinders."""
    format1 = FMT_144.physical_format
    format2 = PhysicalFormat(
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
                sectors_per_track=18,
                encoding="MFM",
                rate=500,
                gap3_bytes=84,
                interleave=1,
            )
        ],
    )

    assert metadata_detector._physical_formats_match(format1, format2) is False


def test_physical_formats_match_different_sectors_per_track(metadata_detector) -> None:
    """Tests _physical_formats_match with different sectors per track."""
    format1 = FMT_144.physical_format
    format2 = FMT_720.physical_format

    assert metadata_detector._physical_formats_match(format1, format2) is False


def test_filesystem_configs_match_different_types(metadata_detector) -> None:
    """Tests _filesystem_configs_match with different config types."""
    profile = FMT_144
    config1 = FATVolumeInfo()
    config2 = object()

    result = metadata_detector._filesystem_configs_match(profile, config1, config2)
    assert result is False


def test_filesystem_configs_match_no_filesystem_type(metadata_detector) -> None:
    """Tests _filesystem_configs_match when profile has no filesystem type."""
    mock_profile = MagicMock(spec=FormatProfile)
    mock_profile.get_filesystem_type.return_value = None
    config1 = FATVolumeInfo()
    config2 = FATVolumeInfo()

    result = metadata_detector._filesystem_configs_match(mock_profile, config1, config2)
    assert result is False


def test_filesystem_configs_match_no_configs_match_method(metadata_detector) -> None:
    """Tests _filesystem_configs_match when FS class has no configs_match."""
    profile = FMT_144
    config1 = FATVolumeInfo()
    config2 = FATVolumeInfo()

    mock_fs_class = MagicMock()
    delattr(mock_fs_class, "configs_match")

    with patch(
        "fatfloppy.core.format_detection.get_filesystem_class_by_type",
        return_value=mock_fs_class,
    ):
        result = metadata_detector._filesystem_configs_match(profile, config1, config2)
        assert result is False


def test_filesystem_configs_match_success(metadata_detector) -> None:
    """Tests _filesystem_configs_match with matching configs."""
    profile = FMT_144
    config1 = FATVolumeInfo()
    config2 = FATVolumeInfo()

    with patch.object(FATFilesystem, "configs_match", return_value=True):
        result = metadata_detector._filesystem_configs_match(profile, config1, config2)
        assert result is True


def test_match_to_known_profile_no_physical_format(metadata_detector) -> None:
    """Tests _match_to_known_profile skips profiles without physical format."""
    mock_profile = FormatProfile(
        name="no_phys", description="No Physical", physical_format=None
    )
    metadata_detector.known_formats = {"no_phys": mock_profile}

    result = metadata_detector._match_to_known_profile(
        FMT_144.physical_format, FATVolumeInfo()
    )
    assert result is None


def test_match_to_known_profile_physical_mismatch(metadata_detector) -> None:
    """Tests _match_to_known_profile with non-matching physical format."""
    metadata_detector.known_formats = {
        "no_phys": FormatProfile(
            name="no_phys", description="No Physical", physical_format=None
        )
    }

    result = metadata_detector._match_to_known_profile(
        FMT_144.physical_format, FATVolumeInfo()
    )
    assert result is None


def test_match_to_known_profile_success(metadata_detector) -> None:
    """Tests _match_to_known_profile with complete match."""
    with (
        patch.object(metadata_detector, "_physical_formats_match", return_value=True),
        patch.object(metadata_detector, "_filesystem_configs_match", return_value=True),
    ):
        result = metadata_detector._match_to_known_profile(
            FMT_144.physical_format, FATVolumeInfo()
        )
        assert result in ["ibm_3.5_1.44m", "ibm_3.5_720k"]


def test_create_format_detector_success() -> None:
    """Tests create_format_detector with registered driver."""
    mock_disk = MagicMock()
    mock_driver = MagicMock()
    mock_driver.__class__.__name__ = "IMGImageDriver"
    known_formats: dict[str, Any] = {"test": FMT_144}

    with patch(
        "fatfloppy.core.detector_registry.DetectorRegistry.get_detector",
        return_value=ConcreteMetadataDetector,
    ):
        detector = create_format_detector(mock_disk, mock_driver, known_formats)

        assert isinstance(detector, ConcreteMetadataDetector)
        assert detector.disk is mock_disk
        assert detector.driver is mock_driver


def test_create_format_detector_no_detector_registered() -> None:
    """Tests create_format_detector with unregistered driver."""
    mock_disk = MagicMock()
    mock_driver = MagicMock()
    mock_driver.__class__.__name__ = "UnknownDriver"
    known_formats: dict[str, Any] = {}

    with (
        patch(
            "fatfloppy.core.detector_registry.DetectorRegistry.get_detector",
            return_value=None,
        ),
        pytest.raises(ValueError, match="No format detector registered"),
    ):
        create_format_detector(mock_disk, mock_driver, known_formats)


def test_detector_registry_get_detector_known_driver() -> None:
    """Tests DetectorRegistry.get_detector with a known driver type."""
    mock_driver = MagicMock()
    mock_driver.__class__.__name__ = "IMGImageDriver"

    detector_class = DetectorRegistry.get_detector(mock_driver)

    assert detector_class is not None
    assert issubclass(detector_class, FormatDetector)


def test_detector_registry_get_detector_unknown_driver() -> None:
    """Tests DetectorRegistry.get_detector with unknown driver type."""
    mock_driver = MagicMock()
    mock_driver.__class__.__name__ = "NonExistentDriver12345"

    detector_class = DetectorRegistry.get_detector(mock_driver)
    assert detector_class is None


def test_detector_registry_list_registered_drivers() -> None:
    """Tests DetectorRegistry.list_registered_drivers."""
    drivers = DetectorRegistry.list_registered_drivers()

    assert isinstance(drivers, list)


def test_detector_registry_register_external() -> None:
    """Tests DetectorRegistry.register_external."""

    class ExternalTestDetector(FormatDetector):
        detector_for_driver = "ExternalDriver"

        def detect(self):
            return None, None, None

    DetectorRegistry.register_external("ExternalDriver", ExternalTestDetector)

    assert "ExternalDriver" in DetectorRegistry.list_registered_drivers()

    if "ExternalDriver" in DetectorRegistry._registry:
        del DetectorRegistry._registry["ExternalDriver"]
