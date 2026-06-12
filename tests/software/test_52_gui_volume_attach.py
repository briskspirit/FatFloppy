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
    build_three_volume_set,
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


def record_refresh(fm):
    """Records refresh_needed emissions (the signal main_window connects to
    refresh_filesystem_ui) as (path, /com listing AT EMIT TIME): capturing the
    listing when the signal fires proves the rebuilt view would already show
    the post-attach state, not the stale pre-attach one."""
    calls = []

    def on_refresh(path):
        fs = fm.parent.controller.filesystem
        names = sorted(
            (info.name, info.attributes) for info in fs.list_directory("/com")
        )
        calls.append((path, names))

    fm.refresh_needed.connect(on_refresh)
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
            "'bigfile' is cut at the end of volume SYNVA0. The backup set "
            "continues with section 2 of 'COM' seq 2 "
            "(uid 31F49BD4.200071FA) on the next volume. "
            "Open the next volume image?"
        )
        assert len(dialogs) == 1
        assert "*.img" in dialogs[0][1] and "*.afd" in dialogs[0][1]
        # the attach is visible filesystem-wide (the follow-on file joined)
        fs = fm.parent.controller.filesystem
        assert fs.read_file("/com/followon") == s.follow_content

    def test_prompt_and_rejection_share_expectation(
        self, two_vol, tmp_path, monkeypatch
    ):
        # The user pain this guards (FT0003's ftn_sr9.2): the prompt used
        # to omit WHICH volume the set expects, so unrelated disks from
        # the same machine looked plausible and the rejection message was
        # the first place the identity appeared.  The prompt, the file-
        # picker title and the wrong-volume rejection must all name the
        # expectation via the SAME helper (ContinuationSpec.expectation),
        # so the texts can never drift apart.
        s, fm = two_vol.s, two_vol.fm
        fs = fm.parent.controller.filesystem
        (spec,) = fs.pending_continuations()
        expectation = spec.expectation()
        # the shared helper names tree, seq, section and the set uid
        assert expectation == "section 2 of 'COM' seq 2 (uid 31F49BD4.200071FA)"
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
        questions = patch_question(monkeypatch, [YES, NO])  # wrong pick, give up
        dialogs = patch_open_dialog(monkeypatch, [str(wrong_path)])
        warnings = patch_warning(monkeypatch)
        select(fm, "bigfile")

        fm.extract_selected_items()

        assert len(questions) == 2 and len(warnings) == 1
        # the initial prompt and the rejection carry the IDENTICAL substring
        assert expectation in questions[0][1]
        assert expectation in warnings[0][1]
        assert expectation in questions[1][1]  # the re-prompt names it too
        # the file picker's caption is enriched the same way
        assert len(dialogs) == 1
        assert expectation in dialogs[0][0]

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

    def test_three_volume_chain_prompts_per_volume(self, tmp_path, monkeypatch):
        # N-volume chains, end to end: A (EOV) -> B (section 2, EOV) -> C
        # (section 3, EOF).  Attaching B stitches a longer prefix but the
        # tree still ends EOV, so the loop must prompt AGAIN for C; after C
        # the tree completes and prompting stops (the two-answer script
        # fails the test on a third question).
        s = build_three_volume_set()
        vol_a, vol_b, vol_c = s.volumes
        vol_b_path = tmp_path / "vol_b.img"
        vol_b_path.write_bytes(vol_b)
        vol_c_path = tmp_path / "vol_c.img"
        vol_c_path.write_bytes(vol_c)
        fm = open_file_manager(vol_a, tmp_path)
        try:
            dest = tmp_path / "bigfile.out"
            monkeypatch.setattr(
                QFileDialog,
                "getSaveFileName",
                staticmethod(lambda *_a, **_k: (str(dest), "")),
            )
            questions = patch_question(monkeypatch, [YES, YES])
            dialogs = patch_open_dialog(monkeypatch, [str(vol_b_path), str(vol_c_path)])
            warnings = patch_warning(monkeypatch)
            select(fm, "bigfile")

            fm.extract_selected_items()

            assert dest.read_bytes() == s.content  # all 9000 bytes, byte-exact
            assert len(questions) == 2  # one prompt per missing volume
            assert "section 2 of 'COM'" in questions[0][1]
            assert "section 3 of 'COM'" in questions[1][1]
            assert len(dialogs) == 2
            assert warnings == []
            fs = fm.parent.controller.filesystem
            assert fs.pending_continuations() == []
        finally:
            fm.parent.controller.close_disk()

    def test_physical_disk_never_prompts(self, two_vol, tmp_path, monkeypatch):
        # _extract_file runs on a WORKER thread for physical-category
        # drivers (see the audit note in _extract_file): modal dialogs are
        # GUI-thread-only, so the attach prompt must never fire there --
        # the cut entry extracts the available prefix silently, exactly
        # like drag-out.  The Apollo wbak filesystem itself mounts over
        # the Greaseweazle driver too (apollo_1.2m_wbak profile), so the
        # capability flag alone cannot gate this.
        s, fm = two_vol.s, two_vol.fm
        monkeypatch.setattr(fm.parent.controller.driver, "driver_category", "physical")
        # mirror the worker invocation: run the op synchronously
        fm.parent._run_threaded_operation = lambda op, _name, **_k: op()
        dest = tmp_path / "bigfile.out"
        monkeypatch.setattr(
            QFileDialog,
            "getSaveFileName",
            staticmethod(lambda *_a, **_k: (str(dest), "")),
        )
        questions = patch_question(monkeypatch, [])  # any prompt fails
        dialogs = patch_open_dialog(monkeypatch, [])
        select(fm, "bigfile")

        fm.extract_selected_items()

        assert dest.read_bytes() == s.prefix  # silent prefix, like drag-out
        assert questions == [] and dialogs == []
        assert fm.parent.controller.filesystem.pending_continuations() != []

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

    def test_successful_attach_refreshes_browser(self, two_vol, tmp_path, monkeypatch):
        # attach_volume changes what the browser shows ('followon' joins the
        # listing, bigfile's PARTIAL flag clears), so a successful attach
        # must emit refresh_needed -- the same signal imports and deletions
        # emit, which main_window connects to refresh_filesystem_ui.
        # Before the fix the view kept the pre-attach state until a manual
        # reopen.
        s, fm = two_vol.s, two_vol.fm
        fs = fm.parent.controller.filesystem
        pre = sorted((i.name, i.attributes) for i in fs.list_directory("/com"))
        assert pre == [("bigfile", "PARTIAL")]  # the stale view's content
        refreshes = record_refresh(fm)
        dest = tmp_path / "bigfile.out"
        monkeypatch.setattr(
            QFileDialog,
            "getSaveFileName",
            staticmethod(lambda *_a, **_k: (str(dest), "")),
        )
        patch_question(monkeypatch, [YES])
        patch_open_dialog(monkeypatch, [str(two_vol.vol_b_path)])
        select(fm, "bigfile")

        fm.extract_selected_items()

        assert dest.read_bytes() == s.content
        assert len(refreshes) == 1  # one refresh for the one attached volume
        path, listing_at_emit = refreshes[0]
        assert path == "/com"  # preserve the user's place, like import does
        # emitted AFTER the fs mutated: the rebuild shows the attached state
        assert listing_at_emit == [("bigfile", ""), ("followon", "")]

    def test_decline_does_not_refresh(self, two_vol, tmp_path, monkeypatch):
        # No attach happened, nothing changed: declining the prompt must not
        # trigger a rebuild (same for the physical/drag-out early returns,
        # which never reach the attach call at all).
        s, fm = two_vol.s, two_vol.fm
        refreshes = record_refresh(fm)
        dest = tmp_path / "bigfile.out"
        monkeypatch.setattr(
            QFileDialog,
            "getSaveFileName",
            staticmethod(lambda *_a, **_k: (str(dest), "")),
        )
        patch_question(monkeypatch, [NO])
        patch_open_dialog(monkeypatch, [])
        select(fm, "bigfile")

        fm.extract_selected_items()

        assert dest.read_bytes() == s.prefix
        assert refreshes == []

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
