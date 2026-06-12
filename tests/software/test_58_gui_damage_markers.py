# tests/software/test_58_gui_damage_markers.py
"""
GUI feedback Task 3: PARTIAL/DMG entries must be visually obvious in the
file browser, not just a token in the Attr column.

update_file_list colors every column of a row whose attribute string
carries the PARTIAL (cut at end of volume) or DMG (damaged on the medium)
token -- a word match on the space-separated markers, never a substring --
and explains the marker in a tooltip on the name cell.  DMG outranks
PARTIAL when both are present (red wins, tooltip mentions both).  The
treatment is filesystem-agnostic: wbak emits both tokens, AEGIS emits DMG
(including on directories), and the directory tree gets the same marking.

Colors are fixed mid-tones (orange 200,120,0 / red 200,60,60): the QSS
themes (themes.py) swap white-on-#3c3c3c and black-on-#ffffff at runtime,
and an item's setForeground brush overrides the QSS text color, so a
single color must stay readable on both backgrounds.  Clean rows are
asserted against a clean sibling's brush, never an absolute color.
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
from fatfloppy.gui.models import FileSystemNode  # noqa: E402

from .test_49_apollo_wbak_parser import build_two_volume_set  # noqa: E402

RESOURCES = Path(__file__).parent.parent / "resources" / "APOLLO"

ORANGE = (200, 120, 0)
RED = (200, 60, 60)
COLUMNS = 4  # name, size, modified, attributes


@pytest.fixture(scope="module")
def qapp():
    """Provides a single offscreen QApplication for the GUI tests."""
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


# QTreeWidgetItems die with their QTreeWidget; keep the harness widgets
# alive for the duration of the module so row assertions stay valid.
_keepalive = []


def make_window(node, path="/"):
    """Minimal main-window stand-in around a real QTreeWidget, driven
    through the real (unbound) FileBrowserApp.update_file_list."""
    from PyQt6.QtWidgets import QTreeWidget

    file_list = QTreeWidget()
    _keepalive.append(file_list)
    file_list.setColumnCount(COLUMNS)
    window = SimpleNamespace(
        file_list=file_list,
        current_node=node,
        current_path=path,
        logger=logging.getLogger("test_damage_markers"),
    )
    FileBrowserApp.update_file_list(window)
    return window


def rows(window):
    fl = window.file_list
    return {
        fl.topLevelItem(i).text(0): fl.topLevelItem(i)
        for i in range(fl.topLevelItemCount())
    }


def rgb(item, column=0):
    color = item.foreground(column).color()
    return (color.red(), color.green(), color.blue())


def build_rows(*entries):
    """entries: (name, attributes, is_dir) -> {name: QTreeWidgetItem}."""
    root = FileSystemNode("Root", is_dir=True, attributes="-")
    for name, attrs, is_dir in entries:
        root.appendChild(
            FileSystemNode(
                name,
                size=0 if is_dir else 123,
                is_dir=is_dir,
                attributes=attrs,
                parent=root,
            )
        )
    return rows(make_window(root))


@pytest.mark.usefixtures("qapp")
class TestAttributeMarkers:
    def test_partial_row_orange_on_all_columns_with_tooltip(self):
        items = build_rows(("clean", "F", False), ("cut", "F PARTIAL", False))
        cut, clean = items["cut"], items["clean"]
        for column in range(COLUMNS):
            assert rgb(cut, column) == ORANGE, f"column {column} not orange"
            assert cut.foreground(column) != clean.foreground(column)
        assert "next volume" in cut.toolTip(0)
        assert clean.toolTip(0) == ""

    def test_dmg_row_red_on_all_columns_with_tooltip(self):
        items = build_rows(("clean", "F", False), ("bad", "F DMG", False))
        bad = items["bad"]
        for column in range(COLUMNS):
            assert rgb(bad, column) == RED, f"column {column} not red"
        assert "zero-filled" in bad.toolTip(0)

    def test_both_tokens_dmg_color_wins_tooltip_mentions_both(self):
        items = build_rows(("worst", "F PARTIAL DMG", False))
        worst = items["worst"]
        assert rgb(worst) == RED
        assert "zero-filled" in worst.toolTip(0)
        assert "next volume" in worst.toolTip(0)

    def test_word_match_not_substring(self):
        # Tokens merely containing the markers must not light up.
        items = build_rows(("clean", "F", False), ("nearmiss", "DMGS PARTIALLY", False))
        near, clean = items["nearmiss"], items["clean"]
        assert near.foreground(0) == clean.foreground(0)
        assert near.toolTip(0) == ""

    def test_clean_rows_share_the_default_brush(self):
        items = build_rows(("a", "F", False), ("b", "-", False))
        assert items["a"].foreground(0) == items["b"].foreground(0)
        assert items["a"].toolTip(0) == ""

    def test_empty_attributes_directory_no_crash_default_brush(self):
        items = build_rows(("subdir", "", True), ("clean", "F", False))
        assert items["subdir"].foreground(0) == items["clean"].foreground(0)
        assert items["subdir"].toolTip(0) == ""

    def test_damaged_directory_row_is_red(self):
        # AEGIS emits DMG on directories whose VTOCE is missing/damaged;
        # the shared row-construction path must treat dir rows identically.
        items = build_rows(("baddir", "DIR DMG", True))
        assert rgb(items["baddir"]) == RED
        assert "zero-filled" in items["baddir"].toolTip(0)


@pytest.mark.usefixtures("qapp")
class TestDirectoryTreeMarking:
    def test_damaged_directory_marked_in_tree_widget(self):
        from PyQt6.QtWidgets import QTreeWidget, QTreeWidgetItem

        root = FileSystemNode("Root", is_dir=True, attributes="-")
        for name, attrs in (("ok", "DIR"), ("bad", "DIR DMG")):
            root.appendChild(
                FileSystemNode(name, is_dir=True, attributes=attrs, parent=root)
            )
        tree = QTreeWidget()
        root_item = QTreeWidgetItem(tree, ["/"])
        window = SimpleNamespace(logger=logging.getLogger("test_damage_markers"))
        window._recursive_populate_tree_widget = lambda node, parent: (
            FileBrowserApp._recursive_populate_tree_widget(window, node, parent)
        )
        FileBrowserApp._recursive_populate_tree_widget(window, root, root_item)

        children = {
            root_item.child(i).text(0): root_item.child(i)
            for i in range(root_item.childCount())
        }
        assert rgb(children["bad"]) == RED
        assert "zero-filled" in children["bad"].toolTip(0)
        assert children["ok"].foreground(0) != children["bad"].foreground(0)
        assert children["ok"].toolTip(0) == ""


@pytest.mark.usefixtures("qapp")
class TestRealVolumes:
    def _file_list_for(self, controller, *path_parts):
        fake = MagicMock()
        fake.controller = controller
        root = FileBrowserApp._build_fs_tree(fake)
        assert root is not None
        node = root
        for part in path_parts:
            node = next(c for c in node.children if c.name == part)
        return rows(make_window(node, "/" + "/".join(path_parts)))

    def test_wbak_cut_file_row_is_orange(self, tmp_path):
        volumes = build_two_volume_set()
        image = tmp_path / "vol_a.img"
        image.write_bytes(volumes.vol_a)
        controller = DiskController()
        assert controller.open_disk(str(image), disk_type="auto")
        try:
            items = self._file_list_for(controller, "com")
            big = items["bigfile"]
            assert "PARTIAL" in big.text(3).split()  # fixture sanity
            assert rgb(big) == ORANGE
            assert "next volume" in big.toolTip(0)
        finally:
            controller.close_disk()

    def test_disk2_dmg_rows_are_red_clean_siblings_default(self):
        path = RESOURCES / "disk2.img"
        if not path.exists():
            pytest.skip(f"resource missing: {path}")
        controller = DiskController()
        assert controller.open_disk(str(path), disk_type="auto")
        try:
            items = self._file_list_for(controller, "sys5", "etc")
            fix = items["fix_cache~1"]
            assert "DMG" in fix.text(3).split()  # fixture sanity
            assert rgb(fix) == RED
            assert "zero-filled" in fix.toolTip(0)

            clean = next(
                item
                for item in items.values()
                if "DMG" not in item.text(3).split()
                and "PARTIAL" not in item.text(3).split()
            )
            assert clean.foreground(0) != fix.foreground(0)
            assert clean.toolTip(0) == ""
        finally:
            controller.close_disk()
