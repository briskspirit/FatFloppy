"""Pure-Python LZHUF decompressor for Teledisk "advanced" compression.

Teledisk 2.x images whose signature is ``'td'`` (lower case) compress
everything after the 12-byte file header as ONE continuous LZSS +
adaptive-Huffman stream — an Okumura/Yoshizaki LZHUF derivative with
Teledisk-specific parameters (see
``docs/superpowers/specs/2026-06-12-td0-driver-design.md`` section 2):

- LZSS: 4096-byte ring buffer preset to ``0x20`` (spaces), 60-byte
  look-ahead, match threshold 2, ring pointer starting at 4036 (= N - F);
- adaptive Huffman: 314 character codes (256 literals + match lengths
  3..60 as codes 256..313), 627-node tree with root at index 626, leaf
  frequencies preset to 1, sibling-swap frequency update, full tree
  rebuild when the root frequency reaches 0x8000;
- match positions: one whole input byte indexes the static ``d_code``
  table for the upper 6 bits, then ``d_len - 2`` further bits complete a
  12-bit offset; match start = ``(r - position - 1) & 0xFFF``;
- bits are consumed MSB-first.

Ported mechanically from ``tdlzhuf.c`` in Will Kranz's wteledsk (itself
derived from LZHUF.C by Haruhiko Okumura / Haruyasu Yoshizaki, English
translation by Kenji Rikitake); preserved in
``docs/superpowers/research/td0/refs/wteledsk-master/src/tdlzhuf.c``.

End-of-input semantics follow Dave Dunfield's TD02IMD decoder (the same
code greaseweazle ships as ``td0_lzss.c`` / ``optimised.td0_unpack``),
making the output byte-identical to that reference for every non-empty
input: the FINAL input byte is read as zero (a quirk of the reference's
``GetChar``), the code in flight when input runs out is completed with
zero bits and emits exactly one byte (the literal, or the first byte of
the match), and decoding then stops.  Truncated input therefore never
raises — it yields the bytes decoded so far plus that one zero-completed
byte.  Empty input returns ``b""`` (deliberate divergence: the C
reference fabricates one byte out of nothing but fake zero bits).

OUTPUT IS UNBOUNDED relative to input: one match code can emit up to 60
bytes from as few as ~10 input bits (a worst case of roughly 48 output
bytes per input byte), so a small crafted stream can balloon without
limit.  Callers MUST size-gate by passing ``max_output`` (the TD0 driver
enforces a floppy-scale ceiling); decompression raises ``ValueError`` as
soon as the ceiling would be exceeded.

Test oracle: byte-equality against ``greaseweazle.optimised.td0_unpack``
over the real-image corpus (tests only; this module is pure stdlib).
"""

__all__ = ["LzhufError", "lzhuf_decompress"]


class LzhufError(ValueError):
    """Raised only on impossible internal decoder states (never expected)."""


# LZSS parameters.
N = 4096  # ring buffer size
F = 60  # look-ahead buffer size
THRESHOLD = 2  # minimum match length - 1

# Adaptive Huffman parameters.
N_CHAR = 256 - THRESHOLD + F  # 314 character codes (0..N_CHAR-1)
T = N_CHAR * 2 - 1  # 627: size of the tree table
R = T - 1  # 626: root position
MAX_FREQ = 0x8000  # rebuild the tree when the root frequency reaches this

# Static tables for the upper 6 bits of the sliding-dictionary position,
# copied verbatim from tdlzhuf.c (d_code[256] / d_len[256]).
# fmt: off
_D_CODE = bytes((
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01,
    0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01,
    0x02, 0x02, 0x02, 0x02, 0x02, 0x02, 0x02, 0x02,
    0x02, 0x02, 0x02, 0x02, 0x02, 0x02, 0x02, 0x02,
    0x03, 0x03, 0x03, 0x03, 0x03, 0x03, 0x03, 0x03,
    0x03, 0x03, 0x03, 0x03, 0x03, 0x03, 0x03, 0x03,
    0x04, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04,
    0x05, 0x05, 0x05, 0x05, 0x05, 0x05, 0x05, 0x05,
    0x06, 0x06, 0x06, 0x06, 0x06, 0x06, 0x06, 0x06,
    0x07, 0x07, 0x07, 0x07, 0x07, 0x07, 0x07, 0x07,
    0x08, 0x08, 0x08, 0x08, 0x08, 0x08, 0x08, 0x08,
    0x09, 0x09, 0x09, 0x09, 0x09, 0x09, 0x09, 0x09,
    0x0A, 0x0A, 0x0A, 0x0A, 0x0A, 0x0A, 0x0A, 0x0A,
    0x0B, 0x0B, 0x0B, 0x0B, 0x0B, 0x0B, 0x0B, 0x0B,
    0x0C, 0x0C, 0x0C, 0x0C, 0x0D, 0x0D, 0x0D, 0x0D,
    0x0E, 0x0E, 0x0E, 0x0E, 0x0F, 0x0F, 0x0F, 0x0F,
    0x10, 0x10, 0x10, 0x10, 0x11, 0x11, 0x11, 0x11,
    0x12, 0x12, 0x12, 0x12, 0x13, 0x13, 0x13, 0x13,
    0x14, 0x14, 0x14, 0x14, 0x15, 0x15, 0x15, 0x15,
    0x16, 0x16, 0x16, 0x16, 0x17, 0x17, 0x17, 0x17,
    0x18, 0x18, 0x19, 0x19, 0x1A, 0x1A, 0x1B, 0x1B,
    0x1C, 0x1C, 0x1D, 0x1D, 0x1E, 0x1E, 0x1F, 0x1F,
    0x20, 0x20, 0x21, 0x21, 0x22, 0x22, 0x23, 0x23,
    0x24, 0x24, 0x25, 0x25, 0x26, 0x26, 0x27, 0x27,
    0x28, 0x28, 0x29, 0x29, 0x2A, 0x2A, 0x2B, 0x2B,
    0x2C, 0x2C, 0x2D, 0x2D, 0x2E, 0x2E, 0x2F, 0x2F,
    0x30, 0x31, 0x32, 0x33, 0x34, 0x35, 0x36, 0x37,
    0x38, 0x39, 0x3A, 0x3B, 0x3C, 0x3D, 0x3E, 0x3F,
))

_D_LEN = bytes((
    0x03, 0x03, 0x03, 0x03, 0x03, 0x03, 0x03, 0x03,
    0x03, 0x03, 0x03, 0x03, 0x03, 0x03, 0x03, 0x03,
    0x03, 0x03, 0x03, 0x03, 0x03, 0x03, 0x03, 0x03,
    0x03, 0x03, 0x03, 0x03, 0x03, 0x03, 0x03, 0x03,
    0x04, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04,
    0x04, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04,
    0x04, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04,
    0x04, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04,
    0x04, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04,
    0x04, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04,
    0x05, 0x05, 0x05, 0x05, 0x05, 0x05, 0x05, 0x05,
    0x05, 0x05, 0x05, 0x05, 0x05, 0x05, 0x05, 0x05,
    0x05, 0x05, 0x05, 0x05, 0x05, 0x05, 0x05, 0x05,
    0x05, 0x05, 0x05, 0x05, 0x05, 0x05, 0x05, 0x05,
    0x05, 0x05, 0x05, 0x05, 0x05, 0x05, 0x05, 0x05,
    0x05, 0x05, 0x05, 0x05, 0x05, 0x05, 0x05, 0x05,
    0x05, 0x05, 0x05, 0x05, 0x05, 0x05, 0x05, 0x05,
    0x05, 0x05, 0x05, 0x05, 0x05, 0x05, 0x05, 0x05,
    0x06, 0x06, 0x06, 0x06, 0x06, 0x06, 0x06, 0x06,
    0x06, 0x06, 0x06, 0x06, 0x06, 0x06, 0x06, 0x06,
    0x06, 0x06, 0x06, 0x06, 0x06, 0x06, 0x06, 0x06,
    0x06, 0x06, 0x06, 0x06, 0x06, 0x06, 0x06, 0x06,
    0x06, 0x06, 0x06, 0x06, 0x06, 0x06, 0x06, 0x06,
    0x06, 0x06, 0x06, 0x06, 0x06, 0x06, 0x06, 0x06,
    0x07, 0x07, 0x07, 0x07, 0x07, 0x07, 0x07, 0x07,
    0x07, 0x07, 0x07, 0x07, 0x07, 0x07, 0x07, 0x07,
    0x07, 0x07, 0x07, 0x07, 0x07, 0x07, 0x07, 0x07,
    0x07, 0x07, 0x07, 0x07, 0x07, 0x07, 0x07, 0x07,
    0x07, 0x07, 0x07, 0x07, 0x07, 0x07, 0x07, 0x07,
    0x07, 0x07, 0x07, 0x07, 0x07, 0x07, 0x07, 0x07,
    0x08, 0x08, 0x08, 0x08, 0x08, 0x08, 0x08, 0x08,
    0x08, 0x08, 0x08, 0x08, 0x08, 0x08, 0x08, 0x08,
))
# fmt: on

# One byte -> its 8 bits, MSB first (input expansion for the hot loop).
_EXPAND = [
    bytes(
        (
            b >> 7 & 1,
            b >> 6 & 1,
            b >> 5 & 1,
            b >> 4 & 1,
            b >> 3 & 1,
            b >> 2 & 1,
            b >> 1 & 1,
            b & 1,
        )
    )
    for b in range(256)
]

# Zero-bit padding appended after the real input bits.  Once end-of-input is
# flagged, at most one code (Huffman walk + position) is still completed.  A
# Huffman walk visits at most N_CHAR - 1 = 313 internal nodes (tree depth for
# 314 leaves), and a position adds at most 8 + 6 = 14 bits, so the in-flight
# code consumes at most 327 padding bits; 4096 zero bits is a ~12.5x safety
# margin against ever indexing past the buffer.
_ZERO_PAD = 4096


def lzhuf_decompress(data: bytes, max_output: "int | None" = None) -> bytes:
    """Decompress a Teledisk advanced (LZHUF) stream.

    `data` is everything after the 12-byte TD0 file header.  Returns the
    decompressed bytes; byte-identical to greaseweazle's C reference for
    any non-empty input.  Never raises on truncated/garbage input (see
    module docstring for the exact end-of-input contract); `LzhufError`
    is reserved for impossible internal states.

    `max_output` is the caller's size gate against decompression bombs:
    output is unbounded relative to input (see module docstring), so when
    the decoded size would exceed `max_output` a `ValueError` is raised
    immediately instead of ballooning further.
    """
    if not data:
        return b""

    # Effectively-unbounded sentinel keeps the hot loop branch-cheap (a
    # plain int comparison) when no ceiling was requested.
    ceiling = max_output if max_output is not None else 1 << 62

    # --- bit reader setup -------------------------------------------------
    # The reference decoder's GetChar() reads the final byte as zero while
    # flagging end-of-input, so the real bits are those of data[:-1]; any
    # consumed bit at index >= limit means end-of-input has been flagged.
    expand = _EXPAND
    bits = b"".join(map(expand.__getitem__, data[:-1])) + bytes(_ZERO_PAD)
    limit = 8 * (len(data) - 1)
    pos = 0

    # --- StartHuff: deterministic initial tree -----------------------------
    # freq:  cumulative frequencies; freq[T] = 0xFFFF sentinel for update().
    # prnt:  parent links; prnt[T + c] locates the leaf of symbol c.
    # son:   child links; values >= T are leaves (symbol + T).
    freq = [0] * (T + 1)
    prnt = [0] * (T + N_CHAR)
    son = [0] * T
    for i in range(N_CHAR):
        freq[i] = 1
        son[i] = i + T
        prnt[i + T] = i
    i, j = 0, N_CHAR
    while j <= R:
        freq[j] = freq[i] + freq[i + 1]
        son[j] = i
        prnt[i] = prnt[i + 1] = j
        i += 2
        j += 1
    freq[T] = 0xFFFF
    prnt[R] = 0

    def _reconst() -> None:
        # Rebuild the tree, halving leaf frequencies (tdlzhuf.c reconst()).
        j = 0
        for i in range(T):
            if son[i] >= T:
                freq[j] = (freq[i] + 1) >> 1
                son[j] = son[i]
                j += 1
        # Connect children: insert each internal node at its sorted slot.
        i = 0
        for j in range(N_CHAR, T):
            f = freq[i] + freq[i + 1]
            k = j - 1
            while f < freq[k]:
                k -= 1
            k += 1
            freq[k + 1 : j + 1] = freq[k:j]
            freq[k] = f
            son[k + 1 : j + 1] = son[k:j]
            son[k] = i
            i += 2
        # Connect parents.
        for i in range(T):
            k = son[i]
            prnt[k] = i
            if k < T:
                prnt[k + 1] = i

    # --- main decode loop ---------------------------------------------------
    out = bytearray()
    out_append = out.append
    window = bytearray(b" " * N)  # ring buffer preset to spaces
    r = N - F  # 4036
    nmask = N - 1
    d_code = _D_CODE
    d_len = _D_LEN

    while True:
        # DecodeChar: walk the tree root -> leaf, one input bit per step.
        c = son[R]
        while c < T:
            c = son[c + bits[pos]]
            pos += 1
        c -= T
        if c >= N_CHAR:
            raise LzhufError(f"decoded symbol {c} out of range")  # unreachable

        # update(): increment the symbol's frequency, swapping nodes to keep
        # the tree frequency-ordered (tdlzhuf.c update(), inlined here as it
        # runs once per symbol and dominates the profile).
        if freq[R] == MAX_FREQ:
            _reconst()
        q = prnt[c + T]
        while True:
            k = freq[q] + 1
            freq[q] = k
            m = q + 1
            # If the order is violated, swap with the farthest node of equal
            # frequency (freq[T] = 0xFFFF guards the scan).
            if k > freq[m]:
                m += 1
                while k > freq[m]:
                    m += 1
                m -= 1
                freq[q] = freq[m]
                freq[m] = k
                i = son[q]
                prnt[i] = m
                if i < T:
                    prnt[i + 1] = m
                j = son[m]
                son[m] = i
                prnt[j] = q
                if j < T:
                    prnt[j + 1] = q
                son[q] = j
                q = m
            q = prnt[q]
            if q == 0:
                break  # reached the root

        if c < 256:
            out_append(c)
            if len(out) > ceiling:
                raise ValueError(
                    f"LZHUF output exceeds the {ceiling}-byte ceiling "
                    "(decompression bomb?)"
                )
            window[r] = c
            r = (r + 1) & nmask
            if pos > limit:
                break  # end of input: literal emitted, stop
        else:
            # DecodePosition: one whole byte indexes d_code/d_len for the
            # upper 6 bits, then d_len - 2 further bits complete the offset.
            i = (
                bits[pos] << 7
                | bits[pos + 1] << 6
                | bits[pos + 2] << 5
                | bits[pos + 3] << 4
                | bits[pos + 4] << 3
                | bits[pos + 5] << 2
                | bits[pos + 6] << 1
                | bits[pos + 7]
            )
            pos += 8
            upper = d_code[i] << 6
            for _ in range(d_len[i] - 2):
                i = (i << 1) + bits[pos]
                pos += 1
            ppos = (r - (upper | (i & 0x3F)) - 1) & nmask
            if pos > limit:
                # End of input mid-match: the reference emits exactly one
                # byte of the in-flight match, then stops.
                b = window[ppos]
                out_append(b)
                if len(out) > ceiling:
                    raise ValueError(
                        f"LZHUF output exceeds the {ceiling}-byte ceiling "
                        "(decompression bomb?)"
                    )
                break
            count = c - 255 + THRESHOLD
            if (
                ppos + count <= N
                and r + count <= N
                and not (ppos < r + count and r < ppos + count)
            ):
                # Fast path: neither range wraps and they do not overlap, so
                # a bulk copy is equivalent to the reference byte loop.
                seg = window[ppos : ppos + count]
                out += seg
                window[r : r + count] = seg
                r = (r + count) & nmask
            else:
                for k in range(count):
                    b = window[(ppos + k) & nmask]
                    out_append(b)
                    window[r] = b
                    r = (r + 1) & nmask
            if len(out) > ceiling:
                raise ValueError(
                    f"LZHUF output exceeds the {ceiling}-byte ceiling "
                    "(decompression bomb?)"
                )

    return bytes(out)
