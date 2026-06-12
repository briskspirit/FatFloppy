"""Teledisk TD0 tests: LZHUF decompression (driver/detection tests follow later).

The load-bearing test is the oracle byte-equality sweep: every "advanced"
(``'td'``-signature) sample in the local corpus must decompress byte-identical
to greaseweazle's C reference decoder (``greaseweazle.optimised.td0_unpack``,
verified signature: single bytes-like argument, returns ``bytes``).

Synthetic tests pin the documented edge contracts:

- empty input -> ``b""`` (deliberate, documented divergence from the C
  reference, which fabricates one byte from fake zero bits);
- truncated input -> graceful short output, never an exception.  The C
  reference zero-fills the code in flight when input runs out, so the FINAL
  byte of a truncated decode may diverge from the full decode; everything
  before it is an exact prefix.  We pin exactly that;
- window-spaces preset: crafted minimal streams whose first code is a match
  into the untouched ring buffer must yield 0x20 bytes.  The streams are
  built by an independent test-local encoder: it replicates only the
  *initial* LZHUF Huffman tree (a fixed constant of the format, per
  ``docs/superpowers/refs`` tdlzhuf.c StartHuff()) to derive the bit code of
  the first symbol, and uses the d_code/d_len definition for the position
  bits (upper 6 bits 0 -> 3-bit prefix ``000`` + 6 verbatim bits).
"""

from pathlib import Path

import pytest

from fatfloppy.core.td0_compression import lzhuf_decompress

LOCAL_TD0 = Path(__file__).parent.parent.parent / "local_images" / "TD0"

# LZHUF tree constants (fixed by the format; duplicated here independently so
# the tests do not trust the production module's own constants).
N_CHAR = 256 - 2 + 60  # 314
T = N_CHAR * 2 - 1  # 627
R = T - 1  # 626


def _advanced_samples() -> list[Path]:
    if not LOCAL_TD0.is_dir():
        return []
    return sorted(
        p
        for p in LOCAL_TD0.iterdir()
        if p.suffix.lower() == ".td0" and p.read_bytes()[:2] == b"td"
    )


# ---------------------------------------------------------------------------
# Test-local minimal encoder pieces (initial tree only)
# ---------------------------------------------------------------------------


def _initial_tree() -> tuple[list[int], list[int]]:
    """Independent replica of LZHUF StartHuff(): the deterministic initial tree."""
    son = [0] * T
    prnt = [0] * (T + N_CHAR)
    for i in range(N_CHAR):
        son[i] = i + T
        prnt[i + T] = i
    i, j = 0, N_CHAR
    while j <= R:
        son[j] = i
        prnt[i] = prnt[i + 1] = j
        i += 2
        j += 1
    prnt[R] = 0
    return son, prnt


def _initial_code(symbol: int) -> list[int]:
    """Bits (MSB first) that decode to `symbol` against the initial tree.

    The decoder walks ``c = son[R]; while c < T: c = son[c + bit]``; we walk
    the parent chain upwards from the leaf and reverse the collected bits.
    """
    son, prnt = _initial_tree()
    bits = []
    s = prnt[symbol + T]
    while s != R:
        p = prnt[s]
        bits.append(s - son[p])
        s = p
    return bits[::-1]


def _pack_bits(bits: list[int]) -> bytes:
    out = bytearray()
    for k in range(0, len(bits), 8):
        chunk = bits[k : k + 8]
        chunk += [0] * (8 - len(chunk))
        out.append(int("".join(map(str, chunk)), 2))
    return bytes(out)


# ---------------------------------------------------------------------------
# Oracle tests (the load-bearing ones)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not LOCAL_TD0.is_dir(), reason="local TD0 corpus not present")
def test_local_corpus_has_advanced_samples():
    # Guard so the parametrized oracle sweep below can never silently shrink
    # to nothing on a machine that does have the corpus.
    assert len(_advanced_samples()) >= 12


class TestLzhufOracle:
    @pytest.mark.parametrize("sample", _advanced_samples(), ids=lambda p: p.name)
    def test_byte_equal_to_greaseweazle(self, sample):
        from greaseweazle import optimised  # test oracle only, never production

        raw = sample.read_bytes()[12:]
        expected = optimised.td0_unpack(raw)
        got = lzhuf_decompress(raw)
        assert isinstance(got, bytes)
        assert got == expected


# ---------------------------------------------------------------------------
# Synthetic / contract tests
# ---------------------------------------------------------------------------


class TestLzhufSynthetic:
    def test_empty_input(self):
        assert lzhuf_decompress(b"") == b""

    def test_truncated_stream_graceful(self):
        samples = _advanced_samples()
        if not samples:
            pytest.skip("no local TD0 samples")
        raw = samples[0].read_bytes()[12:]
        full = lzhuf_decompress(raw)
        for frac in (0.25, 0.5, 0.75):
            cut = lzhuf_decompress(raw[: int(len(raw) * frac)])  # must not raise
            assert 0 < len(cut) <= len(full)
            # Everything decoded from real input bits is an exact prefix of
            # the full decode.  The final byte is the reference C decoders'
            # zero-bit completion of the code in flight when input ran out
            # (greaseweazle does the same; pinned by the oracle sweep), so it
            # is excluded from the prefix comparison.
            assert cut[:-1] == full[: len(cut) - 1]

    def test_window_spaces_preset_single_byte(self):
        # First code: a match (symbol 256 = length 3) at position 58 ->
        # ring offset (4036 - 58 - 1) = 3977, deep inside the untouched
        # space-preset window.  Position 58's last bit lands in the final
        # input byte, which the decoder (like the C references) reads as
        # zero while flagging end-of-input -- so exactly ONE byte of the
        # in-flight match is emitted.  It must be 0x20 from the preset.
        code = _initial_code(256)
        assert len(code) == 8  # fixed property of the initial tree
        bits = code + [0, 0, 0] + [1, 1, 1, 0, 1, 0]  # pos 58: 000 + 111010
        stream = _pack_bits(bits)
        assert len(stream) == 3
        assert lzhuf_decompress(stream) == b"\x20"

    def test_window_spaces_preset_full_match(self):
        # Same match (symbol 256 = length 3) but at position 59 (ring offset
        # 3976) and with a trailing dummy byte so every meaningful bit is
        # backed by real input: the full 3-byte copy from the untouched
        # window must yield three 0x20 bytes.  The decoder then completes
        # exactly one more in-flight code from zero bits (the reference C
        # decoders' end-of-input rule), emitting exactly one trailing byte
        # whose value is not part of this test's contract.
        code = _initial_code(256)
        bits = code + [0, 0, 0] + [1, 1, 1, 0, 1, 1]  # pos 59: 000 + 111011
        stream = _pack_bits(bits) + b"\x00"
        assert len(stream) == 4
        out = lzhuf_decompress(stream)
        assert out[:3] == b"\x20\x20\x20"
        assert len(out) == 4
