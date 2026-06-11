"""CBM filesystem read-path tests."""

import pytest

from fatfloppy.core.filesystems.cbm_fs import petscii_to_unicode, unicode_to_petscii


class TestPetscii:
    def test_basic_round_trip(self):
        raw = b"HELLO WORLD 123"
        assert petscii_to_unicode(raw) == "HELLO WORLD 123"
        assert unicode_to_petscii("HELLO WORLD 123") == raw

    def test_strips_a0_padding_on_decode(self):
        assert petscii_to_unicode(b"GAME\xa0\xa0\xa0\xa0") == "GAME"

    def test_special_glyphs(self):
        assert petscii_to_unicode(b"\x5c\x5e\x5f") == "£↑←"
        assert unicode_to_petscii("£") == b"\x5c"

    def test_shifted_letters_map_to_lowercase(self):
        assert petscii_to_unicode(b"\xc1\xda") == "az"
        assert unicode_to_petscii("az") == b"\xc1\xda"

    def test_unmappable_bytes_escape_round_trip(self):
        s = petscii_to_unicode(b"\x12\x13")
        assert s == "~12~13"
        assert unicode_to_petscii(s) == b"\x12\x13"

    def test_encode_rejects_unmappable_char(self):
        with pytest.raises(ValueError):
            unicode_to_petscii("中")

    def test_truncated_escape_rejected(self):
        with pytest.raises(ValueError):
            unicode_to_petscii("NAME~1")

    def test_every_byte_round_trips(self):
        for b in range(256):
            if b == 0xA0:
                continue  # pad byte is stripped by design
            raw = bytes([b])
            assert unicode_to_petscii(petscii_to_unicode(raw)) == raw
