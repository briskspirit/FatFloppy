"""
GUI name-policy delegation tests.

The GUI's import/export naming must follow the active filesystem's name
policy (suggest_import_name / suggest_host_name / name_hint) instead of
hardcoding FAT 8.3 rules. FileManager is driven with a real opened
controller in the same lightweight style as the other GUI tests; the
QInputDialog/QFileDialog statics are monkeypatched so no dialog ever opens.
"""

import logging
import os
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import pytest  # noqa: E402
from PyQt6.QtCore import QObject  # noqa: E402
from PyQt6.QtWidgets import QFileDialog, QInputDialog, QMessageBox  # noqa: E402

from fatfloppy.core.controller import DiskController  # noqa: E402
from fatfloppy.gui.managers.file_manager import (  # noqa: E402
    FileManager,
    _unique_host_name,
)

RES = Path(__file__).parent.parent / "resources"


def _open_file_manager(image, tmp_path, disk_type="auto", format_info=None):
    """Copies `image` to tmp and builds a FileManager around a real controller."""
    if not Path(image).exists():
        pytest.skip(f"resource missing: {image}")
    dst = tmp_path / Path(image).name
    shutil.copy(image, dst)
    ctrl = DiskController()
    assert ctrl.open_disk(str(dst), disk_type=disk_type, format_info=format_info)
    fm = FileManager.__new__(FileManager)
    QObject.__init__(fm)  # bind the signals without requiring a QMainWindow
    fm.parent = SimpleNamespace(
        controller=ctrl,
        current_node=object(),
        current_path="/",
        _build_full_path=lambda name: "/" + name,
    )
    fm.logger = logging.getLogger("test")
    return fm


@pytest.fixture
def cbm_fm(tmp_path):
    fm = _open_file_manager(RES / "CBM" / "vic1541_bam.d64", tmp_path)
    yield fm
    fm.parent.controller.close_disk()


@pytest.fixture
def fat_fm(tmp_path):
    fm = _open_file_manager(RES / "empty_formatted_144m.img", tmp_path)
    yield fm
    fm.parent.controller.close_disk()


@pytest.fixture
def cpm_fm(tmp_path):
    fm = _open_file_manager(
        RES / "CPM" / "disk1.img",
        tmp_path,
        disk_type="IMG",
        format_info={"format_name": "cpm_8_sssd_250k"},
    )
    yield fm
    fm.parent.controller.close_disk()


# --------------------------------------------------------------------------- #
# Import naming delegates to the active filesystem
# --------------------------------------------------------------------------- #


def test_import_name_delegates_to_cbm(cbm_fm):
    name = cbm_fm._generate_unique_83_name("notes file,s", "/", is_dir=False)
    assert name == "NOTES FILE.S"


def test_import_name_unique_against_cbm_directory(cbm_fm):
    # vic1541_bam.d64 already contains "DIR": importing host "dir" must
    # uniquify against the real directory listing.
    name = cbm_fm._generate_unique_83_name("dir", "/", is_dir=False)
    assert name != "DIR" and len(name) <= 16


def test_import_name_still_83_on_fat12(fat_fm):
    name = fat_fm._generate_unique_83_name("notes file,s", "/", is_dir=False)
    assert name == "NOTES_FI"


def test_format_filename_delegates_to_active_fs(cbm_fm):
    # The legacy-named single-file entry point must follow the policy too.
    assert cbm_fm._format_83_filename("notes file,s") == "NOTES FILE.S"


# --------------------------------------------------------------------------- #
# Export naming: host-safe names and collision uniquification
# --------------------------------------------------------------------------- #


def test_export_copy_all_lands_safe(cbm_fm, tmp_path, monkeypatch):
    """Multi-item extraction maps 'COPY/ALL' to host name 'COPY_ALL'."""
    ctrl = cbm_fm.parent.controller
    dest = tmp_path / "out"
    dest.mkdir()
    monkeypatch.setattr(
        QFileDialog, "getExistingDirectory", staticmethod(lambda *_a, **_k: str(dest))
    )
    items = [
        SimpleNamespace(node=SimpleNamespace(name="COPY/ALL", is_dir=False)),
        SimpleNamespace(node=SimpleNamespace(name="DIR", is_dir=False)),
    ]
    cbm_fm.parent.file_list = SimpleNamespace(selectedItems=lambda: items)

    cbm_fm.extract_selected_items()

    extracted = dest / "COPY_ALL"
    assert extracted.exists(), sorted(p.name for p in dest.iterdir())
    assert extracted.read_bytes() == ctrl.read_file("/COPY/ALL")
    assert (dest / "DIR").exists()


def test_export_collision_uniquified(tmp_path):
    """Same-batch collisions get ' (2)', ' (3)', ... suffixes; pre-existing
    files on disk are overwritten (legacy refresh workflow)."""
    used = set()
    assert _unique_host_name("NAME", used) == "NAME"
    assert _unique_host_name("NAME", used) == "NAME (2)"
    assert _unique_host_name("NAME", used) == "NAME (3)"
    # A pre-existing file does NOT force a suffix across independent batches.
    (tmp_path / "TAKEN").write_bytes(b"x")
    assert _unique_host_name("TAKEN", set()) == "TAKEN"


# --------------------------------------------------------------------------- #
# Dialog labels surface the filesystem's name hint
# --------------------------------------------------------------------------- #


def test_dialog_hint_cbm(cbm_fm, monkeypatch):
    assert "16" in cbm_fm.parent.controller.filesystem.name_hint()

    captured = {}

    def fake_get_text(_parent, _title, label, **_kwargs):
        captured["label"] = label
        return "", False  # cancel

    monkeypatch.setattr(QInputDialog, "getText", staticmethod(fake_get_text))
    cbm_fm.create_directory()
    assert "16" in captured["label"]
    assert "8.3" not in captured["label"]


# --------------------------------------------------------------------------- #
# Single-file import: re-prompt on collision and on filesystem rejection
# --------------------------------------------------------------------------- #


def test_single_import_reprompts_on_invalid_name(cpm_fm, tmp_path, monkeypatch):
    """A name CP/M's write validation rejects re-prompts with the reason."""
    host = tmp_path / "host.txt"
    host.write_bytes(b"payload")
    prompts = []
    answers = iter([("TOOLONGNAME.TXT", True), ("GOODNAME.TXT", True)])

    def fake_get_text(_parent, _title, label, **_kwargs):
        prompts.append(label)
        return next(answers)

    monkeypatch.setattr(QInputDialog, "getText", staticmethod(fake_get_text))
    cpm_fm.import_multiple_paths([str(host)], "/", auto_name=False)

    assert len(prompts) == 2
    assert "8.3" in prompts[1], prompts[1]  # rejection reason shown in the label
    assert cpm_fm.parent.controller.read_file("/GOODNAME.TXT") == b"payload"


def test_single_import_reprompts_on_collision(cbm_fm, tmp_path, monkeypatch):
    host = tmp_path / "host.txt"
    host.write_bytes(b"x")
    prompts = []
    answers = iter([("DIR", True), ("FRESH NAME", True)])

    def fake_get_text(_parent, _title, label, **_kwargs):
        prompts.append(label)
        return next(answers)

    monkeypatch.setattr(QInputDialog, "getText", staticmethod(fake_get_text))
    cbm_fm.import_multiple_paths([str(host)], "/", auto_name=False)

    assert len(prompts) == 2
    assert "16" in prompts[0]  # the CBM hint, not "(8.3 format)"
    assert "exists" in prompts[1]
    assert cbm_fm.parent.controller.read_file("/FRESH NAME") == b"x"


# --------------------------------------------------------------------------- #
# Folder import onto a no-directory filesystem surfaces failure, not a crash
# --------------------------------------------------------------------------- #


def test_folder_import_on_cpm_surfaces_failure_dialog(cpm_fm, tmp_path, monkeypatch):
    """Importing a host directory onto CP/M (which raises NotImplementedError
    for create_directory) must show an Import-Failed dialog rather than
    letting the exception escape the Qt slot and crash the application."""
    host_dir = tmp_path / "subdir"
    host_dir.mkdir()
    (host_dir / "file.txt").write_bytes(b"payload")

    boxes = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        staticmethod(lambda *a, **_k: boxes.append(a)),
    )
    cpm_fm.import_multiple_paths([str(host_dir)], "/", auto_name=True)

    assert boxes, "expected an Import Failed dialog, not a raised exception"
    _parent, title, text = boxes[0][:3]
    assert title == "Import Failed"
    # CP/M auto-naming uppercases the directory name; check case-insensitively.
    assert "subdir" in text.lower()
