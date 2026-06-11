"""Commodore CBM DOS filesystem (1541/1571/1581)."""

import datetime
from abc import ABC, abstractmethod
from typing import Any, ClassVar, Optional

from ..cbm_layout import CBMDiskLayout
from ..disk import Disk
from .fs_base import FileInfo, Filesystem

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
            hh = name[i + 1 : i + 3]
            # Strict: exactly two lowercase hex digits (the codec only emits those).
            if any(c not in "0123456789abcdef" for c in hh):
                raise ValueError(f"Bad ~hh escape in {name!r}")
            out.append(int(hh, 16))
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

    def invalidate(self) -> None:
        """Drop cached BAM sector buffers (re-read from disk on next access)."""
        self._cache.clear()

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

    def mapped_tracks(self) -> range:
        """Tracks that have a real BAM entry (count verification covers these)."""
        return range(1, self.layout.tracks + 1)

    def verify_counts(self) -> bool:
        for t in self.mapped_tracks():
            bits = sum(1 for s in range(self.layout.spt(t)) if self.is_free(t, s))
            if bits != self.free_count(t):
                return False
        return True


class _Bam1541(_BamStrategy):
    """Single BAM sector at 18/0: four bytes per track (count + 3-byte bitmap).

    Tracks above 35 on extended 1541 images have no standard BAM entry
    (Dolphin/Speed DOS variants are read-tolerated, never written): report
    them allocated with count 0 and exclude them from count verification.
    """

    def header_ts(self):
        return (18, 0)

    def _entry(self, track):
        buf = self._sector(18, 0)
        e = 0x04 + 4 * (track - 1)
        return buf, e, e + 1

    def mapped_tracks(self):
        return range(1, min(self.layout.tracks, 35) + 1)

    def is_free(self, t, s):
        if t > 35:
            return False
        return super().is_free(t, s)

    def free_count(self, t):
        if t > 35:
            return 0
        return super().free_count(t)


class _Bam1571(_BamStrategy):
    """Composite BAM: 18/0 holds tracks 1-35 (plus side-2 free counts at 0xDD);
    53/0 holds the side-2 bitmaps, three bytes per track."""

    def header_ts(self):
        return (18, 0)

    def _entry(self, track):
        if track <= 35:
            buf = self._sector(18, 0)
            e = 0x04 + 4 * (track - 1)
            return buf, e, e + 1
        raise NotImplementedError("composite path handles tracks > 35")

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
    """Two BAM sectors: 40/1 covers tracks 1-40, 40/2 covers 41-80;
    six bytes per track (count + 5-byte bitmap) starting at 0x10."""

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
    filesystem_aliases: ClassVar[list[str]] = ["CBM"]
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
        # Phase 3 BAM mutation goes through the strategy cache + flush instead;
        # the BAM flush must call self.disk.write_sector directly (NOT _write_ts)
        # or it would invalidate the very cache entries it is flushing.
        if self._bam is not None:
            self._bam.invalidate()

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
        # Never raise: the registry probes every filesystem on every disk.
        try:
            self._initialize()
        except (ValueError, OSError):
            return 0
        try:
            t, s = self._bam.header_ts()
            hdr = self._read_ts(t, s)
        except Exception:
            return 0
        if not any(hdr):
            return 0
        score = 0
        try:
            dir_t, dir_s = hdr[0], hdr[1]
            if 1 <= dir_t <= self.layout.tracks and dir_s < self.layout.spt(dir_t):
                score += 15
            dos_ok = {"1541": (0x41, 0x00), "1571": (0x41, 0x00), "1581": (0x44, 0x00)}
            if hdr[2] in dos_ok[self.layout.variant]:
                score += 10
            name_off = 0x04 if self.layout.variant == "1581" else 0x90
            name = hdr[name_off : name_off + 16]
            if all(b == PETSCII_PAD or b in _P2U for b in name):
                score += 10
            if self._bam.verify_counts():
                score += 20
            entries_ok = True
            dir_walk_ok = True
            try:
                # No early break: the full chain must be walked so an invalid
                # directory link still raises (and caps the score below).
                for _t, _s, _k, entry in self._iter_entries():
                    tb = entry[2]
                    if tb == 0x00:
                        continue  # scratched slots never count against
                    ftype = tb & 0x0F
                    first_t = entry[3]
                    if ftype not in (1, 2, 3, 4, 5) or not (
                        1 <= first_t <= self.layout.tracks
                    ):
                        entries_ok = False
            except ValueError as exc:
                dir_walk_ok = False
                self.logger.debug(f"Directory walk failed during scoring: {exc}")
            if dir_walk_ok and entries_ok:
                score += 25
            if not dir_walk_ok:
                # No walkable directory chain: CBM DOS cannot operate on this
                # disk (a real 1541 errors out too), so never claim it even if
                # the header/BAM look plausible -- e.g. CP/M-reformatted disks
                # that keep the original 18/0 header intact.
                score = min(score, self.validity_threshold - 5)
        except Exception as exc:
            self.logger.debug(f"Validity scoring stopped early: {exc}")
        return min(score, 100)

    def create_directory(self, path: str) -> None:
        raise NotImplementedError("CBM DOS has no subdirectories")

    def delete(self, path: str) -> None:
        raise NotImplementedError("CBM delete arrives with the write path")

    def delete_recursive(self, path: str) -> bool:
        raise NotImplementedError("CBM delete arrives with the write path")

    def format_fs(self, profile, volume_label=None) -> None:
        raise NotImplementedError("CBM format arrives with the write path")

    def get_disk_map_layout(self) -> dict[str, Any]:
        self._initialize()
        layout = self.layout
        system = {layout.linear_index(*self._bam.header_ts())}
        if layout.variant == "1571":
            system.add(layout.linear_index(53, 0))
        if layout.variant == "1581":
            system |= {layout.linear_index(40, 1), layout.linear_index(40, 2)}
        directory = set()
        try:
            for t, s, _d in self._iter_dir_sectors():
                directory.add(layout.linear_index(t, s))
        except ValueError:
            pass
        allocated = set(self.get_allocated_units())

        def get_sector_type(lba: int) -> str:
            if lba in system:
                return "system"
            if lba in directory:
                return "directory"
            if lba in allocated:
                return "file"
            return "free"

        colors = {
            "system": "#CC4444",
            "directory": "#4444CC",
            "file": "#44AA44",
            "free": "#DDDDDD",
        }
        return {
            "legend": [
                ("BAM/Header", colors["system"]),
                ("Directory", colors["directory"]),
                ("File Data", colors["file"]),
                ("Free", colors["free"]),
            ],
            "get_sector_type": get_sector_type,
            "allocation_unit_size_sectors": 1,
            "first_data_sector": 0,
            "type_color_map": colors,
        }

    def get_display_info(self) -> dict[str, str]:
        self._initialize()
        t, s = self._bam.header_ts()
        hdr = self._read_ts(t, s)
        id_off = 0x16 if self.layout.variant == "1581" else 0xA2
        dos_off = 0x19 if self.layout.variant == "1581" else 0xA5
        info = {
            "Filesystem": "CBM DOS",
            "Variant": self.layout.variant,
            "Tracks": str(self.layout.tracks),
            "Disk Name": self.get_volume_label() or "",
            "Disk ID": petscii_to_unicode(hdr[id_off : id_off + 2]),
            "DOS Type": petscii_to_unicode(hdr[dos_off : dos_off + 2]),
            "Blocks Free": str(self._bam.free_blocks()),
            "Directory Entries": (
                f"{len(self.list_directory('/'))}/{self.layout.max_dir_entries}"
            ),
        }
        drv = self.disk.driver
        if getattr(drv, "has_error_block", False):
            bad = sum(1 for c in drv.error_codes if c not in (0x00, 0x01))
            info["Recorded Sector Errors"] = str(bad)
        if hdr[2] not in (0x41, 0x44, 0x00):
            info["Soft Write Protection"] = "yes (nonstandard DOS byte)"
        return info

    # -- directory ---------------------------------------------------------------

    def _iter_dir_sectors(self):
        """Yields (track, sector, data) for each directory sector, cycle-safe."""
        self._initialize()
        t, s = self.layout.dir_track, self.layout.dir_sector
        seen = set()
        while t != 0:
            if (t, s) in seen:
                raise ValueError(f"Directory chain cycle at {t}/{s}")
            seen.add((t, s))
            if not (1 <= t <= self.layout.tracks) or not (0 <= s < self.layout.spt(t)):
                raise ValueError(f"Directory chain points outside disk at {t}/{s}")
            data = self._read_ts(t, s)
            yield t, s, data
            t, s = data[0], data[1]

    def _iter_entries(self):
        """Yields (track, sector, slot, 32-byte entry) for every directory slot."""
        for t, s, data in self._iter_dir_sectors():
            for k in range(8):
                yield t, s, k, data[0x20 * k : 0x20 * k + 0x20]

    def _entry_to_fileinfo(self, entry: bytes) -> Optional[FileInfo]:
        type_byte = entry[2]
        ftype = type_byte & 0x0F
        if type_byte == 0x00 or ftype == 0:
            return None  # scratched / DEL placeholder
        if ftype not in FILE_TYPES:
            self.logger.warning(f"Skipping entry with invalid type byte {type_byte:#x}")
            return None
        name = petscii_to_unicode(entry[5:0x15])
        attrs = FILE_TYPES[ftype]
        if ftype == 4:
            attrs += f":{entry[0x17]}"
        if type_byte & 0x40:
            attrs += "<"
        if not type_byte & 0x80:
            attrs = "*" + attrs
        sectors = entry[0x1E] | (entry[0x1F] << 8)
        first_t, first_s = entry[3], entry[4]
        try:
            start = self.layout.linear_index(first_t, first_s)
        except ValueError:
            start = 0
        # CBM partitions (D81) are contiguous raw blocks with no T/S links:
        # all 256 bytes of every block are data.
        size = sectors * (256 if ftype == 5 else PAYLOAD)
        return FileInfo(
            name=name,
            size=size,  # chained types refined to exact bytes in list_directory
            is_dir=False,
            datetime=CBM_EPOCH,
            attributes=attrs,
            starting_cluster=start,
            extra_data={
                "raw_name": bytes(entry[5:0x15]),
                "type_byte": type_byte,
                "ftype": ftype,
                "first_ts": (first_t, first_s),
                "rel_record_len": entry[0x17],
                "side_sector_ts": (entry[0x15], entry[0x16]),
                "sectors": sectors,
            },
        )

    def list_directory(self, path: str) -> list[FileInfo]:
        self._initialize()
        if path not in ("", "/"):
            raise FileNotFoundError(f"No such directory: {path}")
        out = []
        for _t, _s, _k, entry in self._iter_entries():
            info = self._entry_to_fileinfo(entry)
            if info is None:
                continue
            base_type = FILE_TYPES[info.extra_data["ftype"]]
            if base_type in ("SEQ", "PRG", "USR", "REL"):
                try:
                    info.size = self._chain_length_bytes(*info.extra_data["first_ts"])
                except ValueError as exc:
                    self.logger.warning(
                        f"Corrupt chain for {info.name!r}: {exc}; using sector estimate"
                    )
            out.append(info)
        return out

    # -- chains ------------------------------------------------------------------

    def _follow_chain(self, track: int, sector: int) -> list[tuple[int, int]]:
        self._initialize()
        chain, seen = [], set()
        t, s = track, sector
        while t != 0:
            if not (1 <= t <= self.layout.tracks) or not (0 <= s < self.layout.spt(t)):
                raise ValueError(f"Chain points outside disk at {t}/{s}")
            if (t, s) in seen:
                raise ValueError(f"Chain cycle at {t}/{s}")
            seen.add((t, s))
            chain.append((t, s))
            data = self._read_ts(t, s)
            t, s = data[0], data[1]
        return chain

    def _read_chain(self, track: int, sector: int) -> bytes:
        out = bytearray()
        for t, s in self._follow_chain(track, sector):
            data = self._read_ts(t, s)
            if data[0] == 0:
                last = data[1]
                if last < 2:
                    self.logger.warning(
                        f"Last sector {t}/{s} claims {last} valid bytes; "
                        "treating as empty"
                    )
                    continue
                out += data[2 : last + 1]
            else:
                out += data[2:256]
        return bytes(out)

    def _chain_length_bytes(self, track: int, sector: int) -> int:
        """Exact byte length of a T/S chain. O(chain) disk I/O: walks every
        link sector, so listing a directory re-reads each file's chain once."""
        chain = self._follow_chain(track, sector)
        if not chain:
            return 0
        last = self._read_ts(*chain[-1])
        # max(last[1], 1) - 1 keeps byte1 < 2 meaning "empty", matching
        # _read_chain's last-sector rule; KEEP THEM CONSISTENT.
        return (len(chain) - 1) * PAYLOAD + max(last[1], 1) - 1

    def _partition_ts_list(self, entry: bytes) -> list[tuple[int, int]]:
        """Block list of a CBM partition (D81): contiguous, no T/S chain links."""
        sectors = entry[0x1E] | (entry[0x1F] << 8)
        t, s = entry[3], entry[4]
        self.layout.linear_index(t, s)  # validates the starting block
        out = []
        for _ in range(sectors):
            if t > self.layout.tracks:
                raise ValueError(
                    f"Partition at {entry[3]}/{entry[4]} ({sectors} blocks) "
                    "runs off the disk"
                )
            out.append((t, s))
            s += 1
            if s >= self.layout.spt(t):
                t, s = t + 1, 0
        return out

    # -- lookup ------------------------------------------------------------------

    def _find_entry(self, path: str):
        """Returns (dir_t, dir_s, slot, entry) or raises FileNotFoundError.

        Entry-based lookup (no path splitting): CBM names may contain '/'.
        """
        self._initialize()
        name = path.removeprefix("/")  # names may legally start with '/'
        for t, s, k, entry in self._iter_entries():
            # Same visibility rule as _entry_to_fileinfo: scratched slots and
            # DEL placeholders (type nibble 0) are not addressable.
            if entry[2] == 0x00 or entry[2] & 0x0F == 0:
                continue
            if petscii_to_unicode(entry[5:0x15]) == name:
                return t, s, k, entry
        raise FileNotFoundError(f"No such file: {path}")

    def read_file(self, path: str) -> bytes:
        _t, _s, _k, entry = self._find_entry(path)
        if not entry[2] & 0x80:
            self.logger.warning(f"Reading splat (unclosed) file {path!r}")
        if (entry[2] & 0x0F) == 5:  # CBM partition: raw contiguous blocks
            return b"".join(
                self._read_ts(t, s) for t, s in self._partition_ts_list(entry)
            )
        return self._read_chain(entry[3], entry[4])

    def get_file_allocation_units(self, path: str) -> list[int]:
        try:
            _t, _s, _k, entry = self._find_entry(path)
            if (entry[2] & 0x0F) == 5:
                blocks = self._partition_ts_list(entry)
            else:
                blocks = self._follow_chain(entry[3], entry[4])
            return [self.layout.linear_index(t, s) for t, s in blocks]
        except (FileNotFoundError, ValueError):
            return []

    def write_file(self, path: str, data: bytes) -> None:
        raise NotImplementedError("CBM write arrives with the write path")
