"""Commodore CBM DOS filesystem (1541/1571/1581)."""

PETSCII_PAD = 0xA0

# Deterministic, bijective display mapping:
#   $20-$3F, $41-$5A as ASCII; $40 '@'; $5B '['; $5D ']'; $5C £; $5E ↑; $5F ←;
#   shifted letters $C1-$DA -> 'a'-'z' (proxy, keeps round-trips unique);
#   anything else -> '~hh' escape (two lowercase hex digits).
_P2U: dict[int, str] = {}
for _b in range(0x20, 0x40):
    _P2U[_b] = chr(_b)
_P2U[0x40] = "@"
for _b in range(0x41, 0x5B):
    _P2U[_b] = chr(_b)
_P2U.update({0x5B: "[", 0x5C: "£", 0x5D: "]", 0x5E: "↑", 0x5F: "←"})
for _b in range(0xC1, 0xDB):
    _P2U[_b] = chr(_b - 0xC1 + ord("a"))
_U2P: dict[str, int] = {v: k for k, v in _P2U.items()}

# Bijectivity assertion: every PETSCII byte maps to a unique display character.
assert len(_U2P) == len(_P2U), (
    f"PETSCII codec is not bijective: {len(_P2U)} byte entries but only "
    f"{len(_U2P)} reverse entries (collision detected)"
)


def petscii_to_unicode(raw: bytes) -> str:
    """Decode PETSCII filename bytes to a display string ($A0 padding stripped)."""
    out = []
    for b in bytes(raw).rstrip(bytes([PETSCII_PAD])):
        ch = _P2U.get(b)
        out.append(ch if ch is not None else f"~{b:02x}")
    return "".join(out)


def unicode_to_petscii(name: str) -> bytes:
    """Encode a display string back to PETSCII bytes; ValueError on unmappable."""
    out, i = bytearray(), 0
    while i < len(name):
        ch = name[i]
        if ch == "~":
            if len(name) - i < 3:
                raise ValueError(f"Truncated ~hh escape in {name!r}")
            try:
                out.append(int(name[i + 1 : i + 3], 16))
            except ValueError as exc:
                raise ValueError(f"Bad ~hh escape in {name!r}") from exc
            i += 3
            continue
        if ch not in _U2P:
            raise ValueError(f"Character {ch!r} has no PETSCII mapping")
        out.append(_U2P[ch])
        i += 1
    return bytes(out)
