"""Commodore CBM DOS filesystem (1541/1571/1581)."""

import datetime
from abc import ABC, abstractmethod
from typing import Any, ClassVar, Optional

from ..cbm_layout import CBMDiskLayout
from ..disk import Disk
from .fs_base import Filesystem

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


PAYLOAD = 254  # data bytes per chained sector (2 bytes are the T/S link)
CBM_EPOCH = datetime.datetime(1982, 1, 1)  # CBM DOS stores no timestamps

FILE_TYPES = {0: "DEL", 1: "SEQ", 2: "PRG", 3: "USR", 4: "REL", 5: "CBM"}


class _BamStrategy(ABC):
    """Per-variant BAM access. Operates on cached BAM sector buffers."""

    def __init__(self, fs: "CBMFilesystem"):
        self.fs = fs
        self.layout = fs.layout
        self._cache: dict[tuple[int, int], bytearray] = {}

    def _sector(self, t: int, s: int) -> bytearray:
        if (t, s) not in self._cache:
            self._cache[(t, s)] = bytearray(self.fs._read_ts(t, s))
        return self._cache[(t, s)]

    @abstractmethod
    def header_ts(self) -> tuple[int, int]:
        """Track/sector of the header (disk name) sector."""

    @abstractmethod
    def _entry(self, track: int) -> tuple[bytearray, int, int]:
        """Returns (bam sector buffer, count offset, bitmap offset) for track."""

    def is_free(self, t: int, s: int) -> bool:
        buf, _c, bm = self._entry(t)
        return bool((buf[bm + s // 8] >> (s % 8)) & 1)

    def free_count(self, t: int) -> int:
        buf, c, _bm = self._entry(t)
        return buf[c]

    def free_blocks(self) -> int:
        return sum(
            self.free_count(t)
            for t in range(1, self.layout.tracks + 1)
            if t not in self.layout.reserved_tracks
        )

    def verify_counts(self) -> bool:
        for t in range(1, self.layout.tracks + 1):
            bits = sum(1 for s in range(self.layout.spt(t)) if self.is_free(t, s))
            if bits != self.free_count(t):
                return False
        return True


class _Bam1541(_BamStrategy):
    def header_ts(self):
        return (18, 0)

    def _entry(self, track):
        buf = self._sector(18, 0)
        e = 0x04 + 4 * (track - 1)
        return buf, e, e + 1


class _Bam1571(_BamStrategy):
    def header_ts(self):
        return (18, 0)

    def _entry(self, track):
        if track <= 35:
            buf = self._sector(18, 0)
            e = 0x04 + 4 * (track - 1)
            return buf, e, e + 1
        raise AssertionError("composite path handles tracks > 35")

    def is_free(self, t, s):
        if t <= 35:
            return super().is_free(t, s)
        buf = self._sector(53, 0)
        off = 3 * (t - 36)
        return bool((buf[off + s // 8] >> (s % 8)) & 1)

    def free_count(self, t):
        if t <= 35:
            return super().free_count(t)
        return self._sector(18, 0)[0xDD + (t - 36)]


class _Bam1581(_BamStrategy):
    def header_ts(self):
        return (40, 0)

    def _entry(self, track):
        s = 1 if track <= 40 else 2
        buf = self._sector(40, s)
        e = 0x10 + 6 * ((track - 1) % 40)
        return buf, e, e + 1


_STRATEGIES = {"1541": _Bam1541, "1571": _Bam1571, "1581": _Bam1581}


class CBMFilesystem(Filesystem):
    """Commodore DOS filesystem over D64/D71/D81 logical geometry."""

    filesystem_type: ClassVar[str] = "CBMDOS"
    filesystem_aliases: ClassVar[list[str]] = ["CBM", "D64", "1541"]
    validity_threshold: ClassVar[int] = 40
    config_class: ClassVar[Optional[type]] = CBMDiskLayout

    def __init__(self, disk: Disk, config: Optional[Any] = None):
        super().__init__(disk, config)
        self.layout: Optional[CBMDiskLayout] = None
        self._bam: Optional[_BamStrategy] = None
        self._initialized = False

    def _initialize(self) -> None:
        if self._initialized:
            return
        if isinstance(self.config, CBMDiskLayout):
            self.layout = self.config
        else:
            self.layout = CBMDiskLayout.infer_from_geometry(self.disk.physical_format)
        if self.layout is None:
            raise ValueError("Disk geometry is not a known CBM layout")
        self._bam = _STRATEGIES[self.layout.variant](self)
        self._initialized = True

    def _read_ts(self, track: int, sector: int) -> bytes:
        return self.disk.read_sector(track - 1, 0, sector)

    def _write_ts(self, track: int, sector: int, data: bytes) -> None:
        self.disk.write_sector(track - 1, 0, sector, data)

    @staticmethod
    def configs_match(config1: Any, config2: Any) -> bool:
        return CBMDiskLayout.matches(config1, config2)

    def get_specific_config(self) -> Optional[Any]:
        self._initialize()
        return self.layout

    def get_volume_label(self) -> Optional[str]:
        self._initialize()
        t, s = self._bam.header_ts()
        hdr = self._read_ts(t, s)
        name_off = 0x04 if self.layout.variant == "1581" else 0x90
        return petscii_to_unicode(hdr[name_off : name_off + 16])

    def get_free_space(self) -> tuple[int, int]:
        self._initialize()
        free = self._bam.free_blocks() * PAYLOAD
        data_capacity = {"1541": 664, "1571": 1328, "1581": 3160}[
            self.layout.variant
        ] * PAYLOAD
        if self.layout.variant == "1541" and self.layout.tracks > 35:
            extra = sum(self.layout.spt(t) for t in range(36, self.layout.tracks + 1))
            data_capacity += extra * PAYLOAD
        return free, data_capacity

    def get_allocated_units(self) -> list[int]:
        self._initialize()
        return sorted(
            self.layout.linear_index(t, s)
            for t in range(1, self.layout.tracks + 1)
            for s in range(self.layout.spt(t))
            if not self._bam.is_free(t, s)
        )

    def get_validity_score(self) -> int:
        # Real scoring lands with the read path; until then never claim a disk
        # (and never raise: the registry probes every filesystem on every disk).
        try:
            self._initialize()
        except (ValueError, OSError):
            return 0
        return 0

    def create_directory(self, path: str) -> None:
        raise NotImplementedError("CBM DOS has no subdirectories")

    def delete(self, path: str) -> None:
        raise NotImplementedError("CBM delete arrives with the write path")

    def delete_recursive(self, path: str) -> bool:
        raise NotImplementedError("CBM delete arrives with the write path")

    def format_fs(self, profile, volume_label=None) -> None:
        raise NotImplementedError("CBM format arrives with the write path")

    def get_disk_map_layout(self) -> dict[str, Any]:
        raise NotImplementedError("CBM disk map arrives with the read path")

    def get_display_info(self) -> dict[str, str]:
        raise NotImplementedError("CBM display info arrives with the read path")

    def get_file_allocation_units(self, path: str) -> list[int]:
        raise NotImplementedError("CBM file chains arrive with the read path")

    def list_directory(self, path: str) -> list:
        raise NotImplementedError("CBM directory listing arrives with the read path")

    def read_file(self, path: str) -> bytes:
        raise NotImplementedError("CBM file reading arrives with the read path")

    def write_file(self, path: str, data: bytes) -> None:
        raise NotImplementedError("CBM write arrives with the write path")
