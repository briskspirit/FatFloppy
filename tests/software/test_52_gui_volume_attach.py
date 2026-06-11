"""
GUI insert-next-volume prompt tests (Apollo wbak cross-volume reassembly).

Extracting an entry that was cut at end-of-volume must prompt to open the
next volume image of the backup set, attach it, and deliver the stitched
file; declining (or cancelling the file dialog) extracts the available
prefix exactly as before.  The loop keys on the tree's PENDING
CONTINUATION, never on the PARTIAL flag alone: a stitched tree can
complete while the file stays short (bytes never written to the set), and
prompting must stop then.

FileManager is driven with a real opened controller in the same
lightweight style as test_48; QMessageBox/QFileDialog statics are
monkeypatched so no dialog ever opens.  The synthetic volumes come from
test_49's builders (full 1,261,568-byte images the APOLLO driver claims).
"""

import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import pytest  # noqa: E402
from PyQt6.QtCore import QObject  # noqa: E402
from PyQt6.QtWidgets import QFileDialog, QMessageBox  # noqa: E402

from fatfloppy.core.cbm_layout import layout_for_variant  # noqa: E402
from fatfloppy.core.controller import DiskController  # noqa: E402
from fatfloppy.gui.managers.file_manager import FileManager  # noqa: E402

from .test_42_cbm_filesystem_read import d64_with_file  # noqa: E402
from .test_49_apollo_wbak_parser import (  # noqa: E402
    build_continuation_volume,
    build_two_volume_set,
)

YES = QMessageBox.StandardButton.Yes
NO = QMessageBox.StandardButton.No


def open_file_manager(data: bytes, tmp_path, *, mount="com", name="primary.img"):
    """Writes `data` to tmp and builds a FileManager around a real controller,
    browsing the given mount directory (the synthetic tree's prefix)."""
    image = tmp_path / name
    image.write_bytes(data)
    ctrl = DiskController()
    assert ctrl.open_disk(str(image), disk_type="auto")
    fm = FileManager.__new__(FileManager)
    QObject.__init__(fm)  # bind the signals without requiring a QMainWindow
    prefix = f"/{mount}" if mount else ""
    fm.parent = SimpleNamespace(
        controller=ctrl,
        current_node=object(),
        current_path=prefix or "/",
        _build_full_path=lambda name: f"{prefix}/{name}",
    )
    fm.logger = logging.getLogger("test")
    return fm


def select(fm, *names):
    """Points the harness file list at the named (non-dir) entries."""
    items = [
        SimpleNamespace(node=SimpleNamespace(name=name, is_dir=False)) for name in names
    ]
    fm.parent.file_list = SimpleNamespace(selectedItems=lambda: items)


def patch_question(monkeypatch, answers):
    """Scripted QMessageBox.question: records (title, text) per call and
    returns the next answer; an exhausted script fails the test instead of
    hanging a runaway prompt loop."""
    calls = []
    script = iter(answers)

    def fake_question(_parent, title, text, *_a, **_k):
        calls.append((title, text))
        try:
            return next(script)
        except StopIteration:
            pytest.fail(f"unexpected extra prompt: {title!r} {text!r}")

    monkeypatch.setattr(QMessageBox, "question", staticmethod(fake_question))
    return calls


def patch_open_dialog(monkeypatch, paths):
    """Scripted QFileDialog.getOpenFileName: records (caption, filter) and
    returns the next path ('' simulates Cancel)."""
    calls = []
    script = iter(paths)

    def fake_open(_parent, caption, _directory="", file_filter="", **_k):
        calls.append((caption, file_filter))
        try:
            return next(script), ""
        except StopIteration:
            pytest.fail("unexpected extra volume file dialog")

    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(fake_open))
    return calls


def patch_warning(monkeypatch):
    calls = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        staticmethod(
            lambda _parent, title, text, *_a, **_k: calls.append((title, text))
        ),
    )
    return calls


@pytest.fixture
def two_vol(tmp_path):
    """Synthetic 2-volume set opened as volume A, volume B on tmp disk."""
    s = build_two_volume_set()
    vol_b_path = tmp_path / "vol_b.img"
    vol_b_path.write_bytes(s.vol_b)
    fm = open_file_manager(s.vol_a, tmp_path)
    yield SimpleNamespace(s=s, fm=fm, vol_b_path=vol_b_path)
    fm.parent.controller.close_disk()


class TestGuiVolumeAttach:
    def test_extract_partial_prompts_and_stitches(self, two_vol, tmp_path, monkeypatch):
        s, fm = two_vol.s, two_vol.fm
        dest = tmp_path / "bigfile.out"
        monkeypatch.setattr(
            QFileDialog,
            "getSaveFileName",
            staticmethod(lambda *_a, **_k: (str(dest), "")),
        )
        questions = patch_question(monkeypatch, [YES])
        dialogs = patch_open_dialog(monkeypatch, [str(two_vol.vol_b_path)])
        select(fm, "bigfile")

        fm.extract_selected_items()

        assert dest.read_bytes() == s.content  # the full 9000 stitched bytes
        assert len(questions) == 1  # exactly ONE prompt for the one volume
        _title, text = questions[0]
        assert text == (
            "'bigfile' continues on the next volume of backup set SYNVA0 "
            "(section 2 of 'COM'). Open the next volume image?"
        )
        assert len(dialogs) == 1
        assert "*.img" in dialogs[0][1] and "*.afd" in dialogs[0][1]
        # the attach is visible filesystem-wide (the follow-on file joined)
        fs = fm.parent.controller.filesystem
        assert fs.read_file("/com/followon") == s.follow_content

    def test_decline_extracts_prefix(self, two_vol, tmp_path, monkeypatch):
        s, fm = two_vol.s, two_vol.fm
        dest = tmp_path / "bigfile.out"
        monkeypatch.setattr(
            QFileDialog,
            "getSaveFileName",
            staticmethod(lambda *_a, **_k: (str(dest), "")),
        )
        questions = patch_question(monkeypatch, [NO])
        dialogs = patch_open_dialog(monkeypatch, [])  # must never be reached
        select(fm, "bigfile")

        fm.extract_selected_items()

        assert dest.read_bytes() == s.prefix  # current behavior: the prefix
        assert len(questions) == 1
        assert dialogs == []  # no file dialog after No
        assert fm.parent.controller.filesystem.pending_continuations() != []

    def test_wrong_volume_reprompts(self, two_vol, tmp_path, monkeypatch):
        s, fm = two_vol.s, two_vol.fm
        wrong_path = tmp_path / "wrong.img"
        wrong_path.write_bytes(
            build_continuation_volume(
                volume_id="SYNVC9", sequence=7, section=4, tail=s.tail
            )
        )
        dest = tmp_path / "bigfile.out"
        monkeypatch.setattr(
            QFileDialog,
            "getSaveFileName",
            staticmethod(lambda *_a, **_k: (str(dest), "")),
        )
        questions = patch_question(monkeypatch, [YES, YES])
        dialogs = patch_open_dialog(
            monkeypatch, [str(wrong_path), str(two_vol.vol_b_path)]
        )
        warnings = patch_warning(monkeypatch)
        select(fm, "bigfile")

        fm.extract_selected_items()

        assert dest.read_bytes() == s.content  # complete after the right pick
        assert len(dialogs) == 2  # wrong volume, then the right one
        assert len(warnings) == 1
        assert "Wrong volume 'SYNVC9'" in warnings[0][1]
        assert len(questions) == 2  # the loop re-prompts after the mismatch

    def test_cancel_file_dialog_extracts_prefix(self, two_vol, tmp_path, monkeypatch):
        s, fm = two_vol.s, two_vol.fm
        dest = tmp_path / "bigfile.out"
        monkeypatch.setattr(
            QFileDialog,
            "getSaveFileName",
            staticmethod(lambda *_a, **_k: (str(dest), "")),
        )
        questions = patch_question(monkeypatch, [YES])
        dialogs = patch_open_dialog(monkeypatch, [""])  # user cancels the dialog
        warnings = patch_warning(monkeypatch)
        select(fm, "bigfile")

        fm.extract_selected_items()

        assert dest.read_bytes() == s.prefix
        assert len(questions) == 1 and len(dialogs) == 1
        assert warnings == []

    def test_batch_prompts_once_per_volume(self, two_vol, tmp_path, monkeypatch):
        # A tree carries at most ONE partial file, so the once-per-volume
        # batch semantics is exercised by extracting the same cut entry
        # twice in one multi-select: the first extraction attaches the
        # volume, the second finds the entry already complete -- no second
        # prompt may fire (the scripted question fails the test if it does).
        s, fm = two_vol.s, two_vol.fm
        dest_dir = tmp_path / "out"
        dest_dir.mkdir()
        monkeypatch.setattr(
            QFileDialog,
            "getExistingDirectory",
            staticmethod(lambda *_a, **_k: str(dest_dir)),
        )
        questions = patch_question(monkeypatch, [YES])
        dialogs = patch_open_dialog(monkeypatch, [str(two_vol.vol_b_path)])
        select(fm, "bigfile", "bigfile")

        fm.extract_selected_items()

        assert len(questions) == 1  # one prompt for the whole batch
        assert len(dialogs) == 1
        assert (dest_dir / "bigfile").read_bytes() == s.content
        assert (dest_dir / "bigfile (2)").read_bytes() == s.content

    def test_non_apollo_fs_never_prompts(self, tmp_path, monkeypatch):
        # CBM splat file (PRG never closed): a "cut-like" entry on a
        # filesystem without supports_volume_attach must never touch the
        # attach machinery -- the scripted dialogs fail the test if opened.
        img = d64_with_file(b"\x01\x08" + bytes(300))
        layout = layout_for_variant("D64", 35)
        img[(layout.sectors_before(18) + 1) * 256 + 2] = 0x02  # PRG, not closed
        fm = open_file_manager(bytes(img), tmp_path, mount="", name="splat.d64")
        try:
            fs = fm.parent.controller.filesystem
            assert fs.filesystem_type == "CBMDOS"
            assert getattr(fs, "supports_volume_attach", False) is False
            assert fs.list_directory("/")[0].attributes == "*PRG"

            dest = tmp_path / "hello.out"
            monkeypatch.setattr(
                QFileDialog,
                "getSaveFileName",
                staticmethod(lambda *_a, **_k: (str(dest), "")),
            )
            questions = patch_question(monkeypatch, [])
            dialogs = patch_open_dialog(monkeypatch, [])
            select(fm, "HELLO")

            fm.extract_selected_items()

            assert dest.read_bytes() == fs.read_file("/HELLO")
            assert questions == [] and dialogs == []
        finally:
            fm.parent.controller.close_disk()

    def test_partial_without_pending_continuation_never_prompts(
        self, tmp_path, monkeypatch
    ):
        # Review semantics: real files can stay PARTIAL forever (FT0006's
        # ftn_sr9.2: 14,300 declared bytes were never written to the set).
        # Synthetic analogue: the continuation ends the tree (EOF) but its
        # tail is 500 bytes short of the declared size.  With the tree
        # complete and nothing pending, extraction must NOT prompt and must
        # deliver everything the set holds.
        s = build_two_volume_set()
        short_b = build_continuation_volume(tail=s.tail[:500], first_seq=3)
        fm = open_file_manager(s.vol_a, tmp_path)
        try:
            fs = fm.parent.controller.filesystem
            fs.attach_volume(short_b)  # tree completes, file stays short
            assert fs.pending_continuations() == []
            big = next(i for i in fs.list_directory("/com") if i.name == "bigfile")
            assert "PARTIAL" in big.attributes  # still short, forever

            dest = tmp_path / "bigfile.out"
            monkeypatch.setattr(
                QFileDialog,
                "getSaveFileName",
                staticmethod(lambda *_a, **_k: (str(dest), "")),
            )
            questions = patch_question(monkeypatch, [])
            dialogs = patch_open_dialog(monkeypatch, [])
            select(fm, "bigfile")

            fm.extract_selected_items()

            assert dest.read_bytes() == s.content[:8500]  # all the set holds
            assert questions == [] and dialogs == []
        finally:
            fm.parent.controller.close_disk()

    def test_attach_that_leaves_file_short_stops_prompting(self, tmp_path, monkeypatch):
        # The corrected loop condition, end to end: the user attaches the
        # final volume, the tree completes, but the file stays short --
        # prompting must STOP (a partial-flag-only loop would re-prompt;
        # the one-answer script fails the test on a second question).
        s = build_two_volume_set()
        short_b_path = tmp_path / "short_b.img"
        short_b_path.write_bytes(
            build_continuation_volume(tail=s.tail[:500], first_seq=3)
        )
        fm = open_file_manager(s.vol_a, tmp_path)
        try:
            dest = tmp_path / "bigfile.out"
            monkeypatch.setattr(
                QFileDialog,
                "getSaveFileName",
                staticmethod(lambda *_a, **_k: (str(dest), "")),
            )
            questions = patch_question(monkeypatch, [YES])
            dialogs = patch_open_dialog(monkeypatch, [str(short_b_path)])
            warnings = patch_warning(monkeypatch)
            select(fm, "bigfile")

            fm.extract_selected_items()

            assert dest.read_bytes() == s.content[:8500]
            assert len(questions) == 1 and len(dialogs) == 1
            assert warnings == []
            fs = fm.parent.controller.filesystem
            assert fs.pending_continuations() == []
        finally:
            fm.parent.controller.close_disk()
