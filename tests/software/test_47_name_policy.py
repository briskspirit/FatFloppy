"""Per-filesystem import/export name policy tests."""

import pytest

from fatfloppy.core.filesystems.fs_base import Filesystem


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
