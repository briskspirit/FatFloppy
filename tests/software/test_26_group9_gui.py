"""
Regression tests for Group 9: GUI correctness fixes.

Covers:
  * main_window.py:459 - filesystem tree never built subdirectory nodes
  * file_manager.py:788 - multi-dot/dotfile host names -> invalid 8.3 names
  * disk_manager.py:558 - update_space_info threw on CBM disks (filesystem
    lacked allocation_unit_size), zeroing busy units and the space display
"""

import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import pytest  # noqa: E402

from fatfloppy.core.controller import DiskController  # noqa: E402
from fatfloppy.gui.main_window import FileBrowserApp  # noqa: E402
from fatfloppy.gui.managers.disk_manager import DiskManager  # noqa: E402
from fatfloppy.gui.managers.file_manager import FileManager  # noqa: E402


def _fat_with_subdir(tmp_path):
    ctrl = DiskController()
    profile_name = "ibm_5.25_160k"
    profile = ctrl.get_format_by_name(profile_name)
    img = tmp_path / "d.img"
    img.write_bytes(b"\x00" * profile.physical_format.total_bytes)
    assert ctrl.open_disk(
        str(img), disk_type="IMG", format_info={"format_name": profile_name}
    )
    assert ctrl.format_disk_media(profile_name)
    assert ctrl.create_directory("/SUB")
    assert ctrl.write_file("/SUB/INNER.TXT", b"hello")
    return ctrl


def test_fs_tree_includes_subdirectories_and_their_files(tmp_path):
    ctrl = _fat_with_subdir(tmp_path)
    fake = MagicMock()
    fake.controller = ctrl

    root = FileBrowserApp._build_fs_tree(fake)
    assert root is not None

    names = [c.name for c in root.children]
    assert "SUB" in names, "subdirectory node missing from the tree"

    sub = next(c for c in root.children if c.name == "SUB")
    inner = [c.name for c in sub.children]
    assert any("INNER" in n for n in inner), "subdirectory's file missing from the tree"
    ctrl.close_disk()


def test_space_info_populates_for_cbm_disk():
    """update_space_info must not zero out busy units/space on a CBM disk.

    CBMFilesystem initially lacked the allocation_unit_size attribute the
    space panel divides by, so the AttributeError handler reset busy_units to
    [] and free/total to 0: the disk map showed everything free.
    """
    path = Path(__file__).parent.parent / "resources" / "CBM" / "1581_demo.d81"
    if not path.exists():
        pytest.skip(f"resource missing: {path}")

    ctrl = DiskController()
    assert ctrl.open_disk(str(path), disk_type="auto")

    manager = DiskManager.__new__(DiskManager)
    manager.logger = logging.getLogger("test")
    manager.parent = SimpleNamespace(controller=ctrl)
    manager.busy_units = []
    manager.free_space = 0
    manager.total_space = 0
    manager.update_space_info()
    ctrl.close_disk()

    assert manager.busy_units, "allocated blocks missing from the disk map data"
    assert manager.total_space == 3160, "1581 capacity should be 3160 blocks"
    assert 0 < manager.free_space < manager.total_space


def test_83_name_generation_handles_dotfiles_and_multidot():
    fmt = FileManager._format_83_filename
    # Dotfile: must not produce an empty base.
    name = fmt(None, ".gitignore")
    base = name.split(".")[0]
    assert base, f"empty 8.3 base for dotfile: {name!r}"
    # Multi-dot: interior dots removed from the base, last segment is the ext.
    assert fmt(None, "my.file.tar.gz") == "MY_FILE_.GZ"
    # Plain name unchanged.
    assert fmt(None, "readme.txt") == "README.TXT"
