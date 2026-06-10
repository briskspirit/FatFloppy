"""
Opt-in regression sweep over a corpus of real vintage disk images.

These images are not shipped with the repository (many are copyrighted vintage
software). Point the FATFLOPPY_TEST_IMAGES environment variable at a directory of
real images to run this sweep; it is skipped otherwise.

    FATFLOPPY_TEST_IMAGES=~/Downloads/TEST_IMGS_FATFLOPPY pytest tests/software/test_27_real_image_corpus.py

For every image whose filesystem is auto-detected, it asserts that the directory
lists and every file reads back without error - a guard that future changes do
not regress real-world disks.
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from fatfloppy.core.controller import DiskController  # noqa: E402

_IMAGE_DIR_ENV = os.environ.get("FATFLOPPY_TEST_IMAGES")
_IMAGE_DIR = Path(_IMAGE_DIR_ENV).expanduser() if _IMAGE_DIR_ENV else None

pytestmark = pytest.mark.skipif(
    not (_IMAGE_DIR and _IMAGE_DIR.is_dir()),
    reason="set FATFLOPPY_TEST_IMAGES to a directory of real disk images to run",
)

# Altair MITS 8" images (137-byte physical sectors).
_MITS_SIZES = {337568, 337664}


def _candidate_driver_types(path: Path) -> list[str]:
    size = path.stat().st_size
    suffix = path.suffix.lower()
    # .dsk covers MITS Altair 8" and 5.25" mini hard-sectored images; the MITS
    # driver's checksum validation rejects non-Altair .dsk, which then fall to IMG.
    if suffix == ".dsk":
        return ["MITS_DSK", "IMG", "IMD"]
    if size in _MITS_SIZES:
        return ["MITS_DSK", "IMG"]
    if suffix == ".imd":
        return ["IMD"]
    if suffix in (".h8d", ".h17", ".h17disk"):
        return ["H17", "IMG"]
    return ["IMG", "IMD"]


def _image_files() -> list[Path]:
    if not _IMAGE_DIR:
        return []
    return sorted(
        p for p in _IMAGE_DIR.rglob("*") if p.is_file() and not p.name.startswith(".")
    )


@pytest.mark.parametrize("image_path", _image_files(), ids=lambda p: p.name)
def test_real_image_reads_all_files(image_path):
    """Any image whose filesystem is detected must read every file cleanly."""
    for driver_type in _candidate_driver_types(image_path):
        controller = DiskController()
        try:
            opened = controller.open_disk(str(image_path), disk_type=driver_type)
            fs = controller.filesystem
            if (
                opened
                and fs is not None
                and fs.get_validity_score() >= fs.validity_threshold
            ):
                items = controller.list_directory("/")
                for item in items:
                    if item.get("is_dir"):
                        continue
                    data = controller.read_file("/" + item["name"])
                    assert data is not None, (
                        f"{image_path.name}: read of {item['name']} returned None"
                    )
                return
        finally:
            controller.close_disk()

    # No driver detected a filesystem; that is acceptable (non-standard/data
    # disk), not a failure of this regression guard.
    pytest.skip(f"{image_path.name}: no filesystem auto-detected")
