"""Per-filesystem import/export name policy tests."""

import shutil
from pathlib import Path

import pytest

from fatfloppy.core.controller import DiskController
from fatfloppy.core.filesystems.fs_base import Filesystem

RESOURCE_DIR = Path(__file__).parent.parent / "resources"


class _StubFS(Filesystem):
    """Concrete shell exposing the base-class name-policy defaults."""

    filesystem_type = "STUB"

    @staticmethod
    def configs_match(config1, config2):  # noqa: ARG004
        return False

    def create_directory(self, path):
        raise NotImplementedError

    def delete(self, path):
        raise NotImplementedError

    def delete_recursive(self, path):
        raise NotImplementedError

    def format_fs(self, profile, volume_label=None):
        raise NotImplementedError

    def get_allocated_units(self):
        raise NotImplementedError

    def get_disk_map_layout(self):
        raise NotImplementedError

    def get_display_info(self):
        raise NotImplementedError

    def get_file_allocation_units(self, path):
        raise NotImplementedError

    def get_free_space(self):
        raise NotImplementedError

    def get_specific_config(self):
        raise NotImplementedError

    def get_validity_score(self):
        raise NotImplementedError

    def list_directory(self, path):
        raise NotImplementedError

    def read_file(self, path):
        raise NotImplementedError

    def write_file(self, path, data):
        raise NotImplementedError


@pytest.fixture
def stub():
    return _StubFS(disk=None)


class TestBaseSuggestImportName:
    def test_plain_name_uppercased(self, stub):
        assert stub.suggest_import_name("readme.txt", set()) == "README.TXT"

    def test_multidot_matches_legacy_contract(self, stub):
        assert stub.suggest_import_name("my.file.tar.gz", set()) == "MY_FILE_.GZ"

    def test_illegal_chars_replaced(self, stub):
        n = stub.suggest_import_name("my file:v2.txt", set())
        assert n == "MY_FILE_.TXT"

    def test_long_name_truncated_83(self, stub):
        assert (
            stub.suggest_import_name("averylongfilename.markdown", set())
            == "AVERYLON.MAR"
        )

    def test_dotfile_gets_placeholder_base(self, stub):
        # ".gitignore": no interior dot after strip(".") -> else-branch.
        # name = ".GITIGNORE" -> replace(".", "_") = "_GITIGNORE"[:8] = "_GITIGNO"
        # strip("_") = "GITIGNO" (truthy) -> base stays "_GITIGNO"
        name = stub.suggest_import_name(".gitignore", set())
        assert name == "_GITIGNO"
        # Generic guards: non-empty and doesn't start with "."
        assert name and not name.startswith(".")
        b, _, _ = name.partition(".")
        assert len(b) <= 8

    def test_directory_has_no_extension(self, stub):
        assert (
            stub.suggest_import_name("my.folder.name", set(), is_dir=True) == "MY_FOLDE"
        )

    def test_uniqueness_tilde_suffix(self, stub):
        n = stub.suggest_import_name("readme.txt", {"README.TXT"})
        b, _, e = n.partition(".")
        assert len(b) <= 8 and len(e) <= 3 and "~" in b
        assert n.upper() != "README.TXT"

    def test_uniqueness_case_insensitive(self, stub):
        n = stub.suggest_import_name("readme.txt", {"readme.txt"})
        assert n.upper() != "README.TXT"

    def test_uniqueness_directory(self, stub):
        n = stub.suggest_import_name("folder", {"FOLDER"}, is_dir=True)
        assert n != "FOLDER" and len(n) <= 8 and "~" in n

    def test_never_raises_on_hostile_input(self, stub):
        for hostile in ["", "...", "中文文件.dat", "a" * 300, "x/y\\z", "🙂.txt"]:
            n = stub.suggest_import_name(hostile, set())
            assert n
            b, _, e = n.partition(".")
            assert len(b) <= 8 and len(e) <= 3

    def test_uniqueness_survives_many_collisions(self, stub):
        existing = {"README.TXT"}
        generated = []
        for _ in range(30):
            n = stub.suggest_import_name("readme.txt", existing)
            generated.append(n)
            existing.add(n.upper())
        assert len(generated) == len({n.upper() for n in generated}), "duplicates found"
        for n in generated:
            b, _, _ = n.partition(".")
            assert len(b) <= 8, f"base too long: {n!r}"


class TestBaseSuggestHostName:
    def test_slash_replaced(self, stub):
        assert stub.suggest_host_name("COPY/ALL") == "COPY_ALL"

    def test_windows_illegal_set_replaced(self, stub):
        assert stub.suggest_host_name('A:B*C?"D<E>F|G\\H') == "A_B_C__D_E_F_G_H"

    def test_control_chars_replaced(self, stub):
        assert stub.suggest_host_name("BAD\x01NAME") == "BAD_NAME"

    def test_edge_dots_spaces_stripped(self, stub):
        assert stub.suggest_host_name("  ..NAME.. ") == "NAME"

    def test_empty_becomes_unnamed(self, stub):
        assert stub.suggest_host_name("///") == "___"

    def test_genuinely_empty_becomes_unnamed(self, stub):
        for hostile in ["", ".", "..", "...", "   "]:
            assert stub.suggest_host_name(hostile) == "_unnamed_", repr(hostile)

    def test_unicode_glyphs_kept(self, stub):
        assert stub.suggest_host_name("£UP↑LEFT←") == "£UP↑LEFT←"


class TestNameHint:
    def test_default_hint(self, stub):
        assert stub.name_hint() == "8.3 format"


class TestFat12Names:
    def _fs(self, tmp_path):
        src = RESOURCE_DIR / "empty_formatted_144m.img"
        dst = tmp_path / "fat.img"
        shutil.copy(src, dst)
        controller = DiskController()
        assert controller.open_disk(str(dst), disk_type="auto")
        return controller.filesystem

    def test_reserved_name_avoided(self, tmp_path):
        fs = self._fs(tmp_path)
        n = fs.suggest_import_name("con.txt", set())
        assert n.split(".")[0] not in {"CON", "PRN", "AUX", "NUL"}
        assert fs._is_valid_83_filename(n)

    def test_existing_names_generator_safe(self, tmp_path):
        # existing_names may be any Iterable, including a one-shot generator;
        # the reserved-name collision re-check must not silently see an
        # exhausted (empty) iterable and return a colliding name.
        fs = self._fs(tmp_path)
        n = fs.suggest_import_name("con.txt", (x for x in ["CON_.TXT"]))
        assert n.upper() != "CON_.TXT"
        assert fs._is_valid_83_filename(n)

    def test_every_suggestion_is_valid_and_writable(self, tmp_path):
        # Every suggestion must survive the FULL round trip: the writer encodes
        # names cp437 errors="replace", so a non-cp437 suggestion silently
        # becomes "????.DAT" on disk -- an entry the lister skips: invisible,
        # unreadable, undeletable. Read-back + listing is what catches that.
        fs = self._fs(tmp_path)
        existing = set()
        battery = [
            "con",
            "aux.c",
            "my file.txt",
            "...",
            "中.dat",
            "a" * 99,
            "lpt1.bin",
            "中文文件.dat",
            "naïve£.txt",  # ï is cp437 but its uppercase Ï is NOT
            "🙂.txt",
            "a\x01b.txt",
        ]
        for h in battery:
            n = fs.suggest_import_name(h, existing)
            assert fs._is_valid_83_filename(n), (h, n)
            fs.write_file("/" + n, b"x")
            assert fs.read_file("/" + n) == b"x", (h, n)
            listed = {fi.name for fi in fs.list_directory("/")}
            assert n in listed, (h, n, listed)
            existing.add(n)


class TestCpmNames:
    def _fs(self, tmp_path):
        # Mirrors test_09's write fixture: copy the committed image to tmp and
        # open it with the explicit (non-interleave) write profile.
        src = RESOURCE_DIR / "CPM" / "disk1.img"
        dst = tmp_path / "disk1.img"
        shutil.copy(src, dst)
        controller = DiskController()
        assert controller.open_disk(
            str(dst),
            disk_type="IMG",
            format_info={"format_name": "cpm_8_sssd_250k"},
        )
        return controller.filesystem

    def test_cpm_name_hint(self, tmp_path):
        fs = self._fs(tmp_path)
        assert fs.name_hint() == "8.3 format (CP/M)"

    def test_cpm_illegal_chars_replaced(self, tmp_path):
        fs = self._fs(tmp_path)
        n = fs.suggest_import_name("a[b]c=d,e.txt", set())
        assert not any(ch in n for ch in "<>,;:=?*[] ")
        base, _, ext = n.partition(".")
        assert len(base) <= 8 and len(ext) <= 3

    def test_cpm_suggestion_ascii_only(self, tmp_path):
        fs = self._fs(tmp_path)
        n = fs.suggest_import_name("naïve£.txt", set())
        assert n.isascii()

    def test_cpm_write_rejects_overlong_base(self, tmp_path):
        fs = self._fs(tmp_path)
        with pytest.raises(ValueError, match="8.3"):
            fs.write_file("/TOOLONGNAME.TXT", b"x")

    def test_cpm_write_rejects_overlong_ext(self, tmp_path):
        fs = self._fs(tmp_path)
        with pytest.raises(ValueError, match="8.3"):
            fs.write_file("/NAME.TEXT", b"x")

    def test_cpm_write_rejects_illegal_chars(self, tmp_path):
        fs = self._fs(tmp_path)
        with pytest.raises(ValueError):
            fs.write_file("/BAD*NAME.TXT", b"x")

    def test_cpm_write_valid_name_still_works(self, tmp_path):
        fs = self._fs(tmp_path)
        fs.write_file("/GOOD.TXT", b"hello")
        assert fs.read_file("/GOOD.TXT") == b"hello"

    def test_cpm_user_prefix_still_accepted(self, tmp_path):
        fs = self._fs(tmp_path)
        fs.write_file("/U1:USERFILE.TXT", b"u1")
        assert fs.read_file("/U1:USERFILE.TXT") == b"u1"

    def test_cpm_user_number_bounds_on_write(self, tmp_path):
        # Real CP/M user areas are 0-15. Writing 'U229:' would stamp 0xE5 into
        # the user byte -- the deleted-entry marker -- creating an entry that
        # is free space the moment it is written. Bound writes; lookups stay
        # lenient so weird on-disk entries remain addressable.
        fs = self._fs(tmp_path)
        fs.write_file("/U15:HIUSER.TXT", b"ok")
        assert fs.read_file("/U15:HIUSER.TXT") == b"ok"
        for bad in ("/U16:FILE.TXT", "/U229:FILE.TXT"):
            with pytest.raises(ValueError, match="0-15"):
                fs.write_file(bad, b"x")
        # Lookup parsing stays lenient for out-of-range users.
        assert fs._parse_cpm_path("/U99:FILE.TXT") == (99, "FILE.TXT")

    def test_cpm_extensionless_round_trip(self, tmp_path):
        # Extension-less names render dot-less ("NOEXT", the CP/M convention),
        # and every consumer of the rendered name (list/read/delete/rewrite)
        # must agree. Historically the entry rendered "NOEXT." while the path
        # parser produced "NOEXT": the file listed but could not be read or
        # deleted, and a rewrite duplicated extent 0.
        fs = self._fs(tmp_path)
        fs.write_file("/NOEXT", b"v1")

        names = {fi.name for fi in fs.list_directory("/")}
        assert "NOEXT" in names
        assert "NOEXT." not in names

        # Empty extension is non-text; CP/M pads records to 128 bytes.
        assert fs.read_file("/NOEXT")[:2] == b"v1"
        # The dotted spelling of the same name must resolve too.
        assert fs.read_file("/NOEXT.")[:2] == b"v1"

        fs.delete("/NOEXT")
        assert "NOEXT" not in {fi.name for fi in fs.list_directory("/")}

        # Rewrite must scratch-and-replace, not pile up stale extent-0 entries.
        fs.write_file("/NOEXT", b"v1")
        fs.write_file("/NOEXT", b"v2")
        assert fs.read_file("/NOEXT")[:2] == b"v2"
        matches = [
            e
            for e in fs._read_directory_entries()
            if not e.is_deleted()
            and e.user == 0
            and e.get_filename().upper() == "NOEXT"
        ]
        assert len(matches) == 1, [e.get_filename() for e in matches]

    def test_cpm_dotted_name_still_round_trips(self, tmp_path):
        # Regression guard for the extension-less fix: a normal dotted name
        # must keep its exact rendering and full round trip.
        fs = self._fs(tmp_path)
        fs.write_file("/GOOD.TXT", b"hello")
        assert "GOOD.TXT" in {fi.name for fi in fs.list_directory("/")}
        # .TXT is a text extension; EOF padding is stripped on read.
        assert fs.read_file("/GOOD.TXT") == b"hello"
        fs.write_file("/GOOD.TXT", b"hello2")
        assert fs.read_file("/GOOD.TXT") == b"hello2"
        matches = [
            e
            for e in fs._read_directory_entries()
            if not e.is_deleted() and e.get_filename().upper() == "GOOD.TXT"
        ]
        assert len(matches) == 1
        fs.delete("/GOOD.TXT")
        assert "GOOD.TXT" not in {fi.name for fi in fs.list_directory("/")}

    def test_cpm_lenient_lookup_reads_weird_existing_names(self, tmp_path):
        # Leniency is pinned at the parser level: delete (and the allocation
        # lookup) go through _parse_cpm_path, which must keep resolving names
        # legacy tools wrote (spaces, over-long bases), while read_file matches
        # the on-disk name verbatim with no validation at all. Poking raw
        # directory sectors would mean reimplementing the skew/track math in
        # the test, so instead we assert the parser accepts what strict write
        # validation rejects, plus a functional probe that an over-long path
        # still resolves on delete.
        fs = self._fs(tmp_path)
        user, parsed = fs._parse_cpm_path("/GO OD.TXT")
        assert (user, parsed) == (0, "GO OD.TXT")
        with pytest.raises(ValueError):
            fs._validate_write_filename("/GO OD.TXT")
        # Over-long base truncates to the existing on-disk name on lookup.
        names = {fi.name for fi in fs.list_directory("/")}
        assert "2FBIOS24.ASM" in names
        fs.delete("/2FBIOS24EXTRA.ASM")
        names = {fi.name for fi in fs.list_directory("/")}
        assert "2FBIOS24.ASM" not in names


class TestHdosNames:
    def _fs(self, tmp_path):
        # Mirrors test_10's write fixture: copy the committed image to tmp.
        src = RESOURCE_DIR / "HDOS" / "HDOS_2-0_TEST.h8d"
        dst = tmp_path / "HDOS_2-0_TEST.h8d"
        shutil.copy(src, dst)
        controller = DiskController()
        assert controller.open_disk(str(dst), disk_type="IMG")
        return controller.filesystem

    def test_hdos_write_rejects_overlong(self, tmp_path):
        fs = self._fs(tmp_path)
        with pytest.raises(ValueError, match="8.3"):
            fs.write_file("/VERYLONGNAME.TXT", b"x")

    def test_hdos_write_valid_name_works(self, tmp_path):
        fs = self._fs(tmp_path)
        # The test image ships full; free a few sectors first.
        fs.delete("/DVDIO.ACM")
        fs.write_file("/OK.TXT", b"fine")
        assert fs.read_file("/OK.TXT") == b"fine"

    def test_hdos_suggestion_ascii_only(self, tmp_path):
        fs = self._fs(tmp_path)
        n = fs.suggest_import_name("naïve£.txt", set())
        assert n.isascii()

    def test_hdos_delete_of_existing_truncated_entry_unaffected(self, tmp_path):
        # Strict 8.3 applies to write_file only; delete/read lookups stay
        # lenient, so an over-long path still resolves (by truncation) to an
        # existing on-disk entry.
        fs = self._fs(tmp_path)
        fs.delete("/DVDIO.ACM")
        fs.write_file("/VERYLONG.TXT", b"data")
        fs.delete("/VERYLONGNAME.TXT")
        names = {fi.name for fi in fs.list_directory("/")}
        assert "VERYLONG.TXT" not in names
        # Parser-level pin: non-strict validation accepts what strict rejects.
        fs._validate_filename("/VERYLONGNAME.TXT")
        with pytest.raises(ValueError, match="8.3"):
            fs._validate_filename("/VERYLONGNAME.TXT", strict=True)
