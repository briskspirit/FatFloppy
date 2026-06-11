"""Commodore CBM DOS filesystem (1541/1571/1581)."""

import dataclasses
import datetime
from abc import ABC, abstractmethod
from collections.abc import Iterable
from typing import Any, ClassVar, Optional

from ..cbm_layout import CBMDiskLayout
from ..disk import Disk
from ..format_profile import FormatProfile
from ..physical_format import PhysicalFormat
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

# Accepted DOS-version bytes per variant; 0x00 is tolerated for
# soft-write-protect/blank DOS bytes (all-zero headers are rejected earlier).
_DOS_BYTE_OK = {"1541": (0x41, 0x00), "1571": (0x41, 0x00), "1581": (0x44, 0x00)}


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

    def _set_bit(self, t: int, s: int, free: bool) -> None:
        if t not in self.mapped_tracks():
            raise ValueError(f"Track {t} has no writable BAM entry")
        if not 0 <= s < self.layout.spt(t):
            raise ValueError(f"Sector {s} out of range on track {t}")
        buf, c, bm = self._entry(t)
        mask = 1 << (s % 8)
        cur = bool(buf[bm + s // 8] & mask)
        if cur == free:
            state = "free" if free else "allocated"
            raise ValueError(f"BAM bit for {t}/{s} already {state}")
        new_count = buf[c] + (1 if free else -1)
        if not 0 <= new_count <= self.layout.spt(t):
            # Corrupt count byte: raise BEFORE flipping the bitmap so the
            # BAM stays exactly as found (visible, repairable).
            raise ValueError(
                f"BAM free count for track {t} would become {new_count} "
                f"(valid 0-{self.layout.spt(t)}); count byte is corrupt"
            )
        buf[bm + s // 8] ^= mask
        buf[c] = new_count

    def set_allocated(self, t: int, s: int) -> None:
        self._set_bit(t, s, free=False)

    def set_free(self, t: int, s: int) -> None:
        self._set_bit(t, s, free=True)

    def flush(self) -> None:
        # Persist via the disk directly: _write_ts would pop our own cache.
        for (t, s), buf in self._cache.items():
            self.fs.disk.write_sector(t - 1, 0, s, bytes(buf))

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

    def mapped_tracks(self) -> range:
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

    def _set_bit(self, t, s, free):
        if t <= 35:
            return super()._set_bit(t, s, free)
        # Side-2 entries are split: bitmap lives at 53/0, count at 18/0.
        if not 0 <= s < self.layout.spt(t):
            raise ValueError(f"Sector {s} out of range on track {t}")
        bm_buf = self._sector(53, 0)
        cnt_buf = self._sector(18, 0)
        off, mask = 3 * (t - 36) + s // 8, 1 << (s % 8)
        if bool(bm_buf[off] & mask) == free:
            state = "free" if free else "allocated"
            raise ValueError(f"BAM bit for {t}/{s} already {state}")
        new_count = cnt_buf[0xDD + (t - 36)] + (1 if free else -1)
        if not 0 <= new_count <= self.layout.spt(t):
            raise ValueError(
                f"BAM free count for track {t} would become {new_count} "
                f"(valid 0-{self.layout.spt(t)}); count byte is corrupt"
            )
        bm_buf[off] ^= mask
        cnt_buf[0xDD + (t - 36)] = new_count


class _Bam1581(_BamStrategy):
    """Two BAM sectors: 40/1 covers tracks 1-40, 40/2 covers 41-80;
    six bytes per track (count + 5-byte bitmap) starting at 0x10.

    A 1581 sub-directory partition carries the very same structure inside
    itself (same global 1-40/41-80 track split, out-of-partition tracks
    marked fully allocated). `sector_map` redirects the canonical BAM sector
    addresses to the partition's own, e.g. {(40,1): (50,1), (40,2): (50,2)},
    and `header` overrides the header sector, e.g. (50, 0). The defaults
    address the root disk BAM. Cache keys (and therefore flush targets) are
    always the REAL post-redirect addresses.
    """

    def __init__(
        self,
        fs: "CBMFilesystem",
        sector_map: Optional[dict[tuple[int, int], tuple[int, int]]] = None,
        header: tuple[int, int] = (40, 0),
    ):
        super().__init__(fs)
        self._sector_map = sector_map or {}
        self._header = header

    def header_ts(self):
        return self._header

    def _sector(self, t, s):
        t, s = self._sector_map.get((t, s), (t, s))
        return super()._sector(t, s)

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
        # Set by _sub_fs on partition-scoped instances: confines check()'s
        # BAM-vs-owned-blocks comparison to the partition's own tracks (its
        # BAM marks everything outside as allocated by convention).
        self._check_tracks: Optional[set[int]] = None

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
        # Contract: BAM sectors are mutated exclusively through the strategy
        # cache (set_allocated/set_free + flush, which writes the disk directly
        # to avoid invalidating itself); everything else goes through _write_ts.
        # Drop only this sector's cached buffer if present -- a data-sector
        # write must NOT discard unrelated pending BAM mutations.
        if self._bam is not None:
            self._bam._cache.pop((track, sector), None)

    @classmethod
    def get_format_definitions(cls) -> dict[str, FormatProfile]:
        from .formats.cbm_formats import CBM_FORMATS

        return CBM_FORMATS

    @staticmethod
    def configs_match(config1: Any, config2: Any) -> bool:
        return CBMDiskLayout.matches(config1, config2)

    @staticmethod
    def create_config_from_params(
        _format_info: dict[str, Any], physical_format: PhysicalFormat
    ) -> Optional[CBMDiskLayout]:
        """CBM layouts are fully determined by geometry; format_info is unused."""
        return CBMDiskLayout.infer_from_geometry(physical_format)

    def get_specific_config(self) -> Optional[Any]:
        self._initialize()
        return self.layout

    def get_volume_label(self) -> Optional[str]:
        self._initialize()
        t, s = self._bam.header_ts()
        hdr = self._read_ts(t, s)
        name_off = 0x04 if self.layout.variant == "1581" else 0x90
        return petscii_to_unicode(hdr[name_off : name_off + 16])

    @property
    def allocation_unit_size(self) -> int:
        """
        Returns the size of a single allocation unit (block) in bytes.

        A CBM block occupies a 256-byte sector but carries PAYLOAD (254) data
        bytes; get_free_space() accounts in payload bytes, so the GUI's
        bytes // allocation_unit_size division reproduces the native
        "blocks free" counts a real drive reports.

        Returns:
            The block payload size in bytes, or 0 if the geometry is not a
            known CBM layout.
        """
        try:
            self._initialize()
        except (ValueError, OSError):
            return 0
        return PAYLOAD

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
        except (OSError, ValueError):
            return 0
        if not any(hdr):
            return 0
        score = 0
        name_off = 0x04 if self.layout.variant == "1581" else 0x90
        try:
            dir_t, dir_s = hdr[0], hdr[1]
            if 1 <= dir_t <= self.layout.tracks and dir_s < self.layout.spt(dir_t):
                score += 15
            if hdr[2] in _DOS_BYTE_OK[self.layout.variant]:
                score += 10
            name = hdr[name_off : name_off + 16]
            # 0x00 tolerated alongside 0xA0 padding: crack-era disks (e.g.
            # the corpus' Archon.d64) zero the name field while the rest of
            # the header and directory stay fully valid.
            if all(b in (PETSCII_PAD, 0x00) or b in _P2U for b in name):
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
                    if tb == 0x00 or tb & 0x0F == 0:
                        # Scratched slots and DEL placeholders (e.g. closed-DEL
                        # 0x80) never count against: same visibility rule as
                        # _entry_to_fileinfo.
                        continue
                    ftype = tb & 0x0F
                    first_t = entry[3]
                    if ftype not in (1, 2, 3, 4, 5) or not (
                        1 <= first_t <= self.layout.tracks
                    ):
                        entries_ok = False
            except (OSError, ValueError) as exc:
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
        except (OSError, ValueError, IndexError) as exc:
            self.logger.debug(f"Validity scoring stopped early: {exc}")
        return min(score, 100)

    def create_directory(self, path: str) -> None:
        """Creates a 1581 CBM partition formatted as a sub-directory.

        path is '/NAME' or '/NAME,<sectors>' (decimal block count; default
        120; must be >= 120 and a multiple of 40, i.e. whole tracks). The
        partition occupies the first contiguous run of fully-free tracks that
        does not touch or straddle the directory track 40, gets the standard
        sub-directory structures (header, BAMs, empty directory) and a type-5
        root entry. Only the 1581 supports partitions.
        """
        self._initialize()
        if self.layout.variant != "1581":
            raise NotImplementedError("Partitions are only supported on D81")
        name = path.removeprefix("/")
        # Reject any path that contains a slash: either a nested-partition
        # attempt or an ambiguous slash-in-name.  Partition names with '/'
        # are not supported (they break path routing).
        if "/" in name:
            comp0, _slash, _rest = name.partition("/")
            entry = self._find_root_type5(comp0)
            if entry is not None:
                raise NotImplementedError("Nested partitions are not supported")
            raise ValueError(f"Partition names cannot contain '/': {name!r}")
        base, sep, suffix = name.rpartition(",")
        if sep and suffix.isdigit():
            sectors = int(suffix)
        else:
            base, sectors = name, 120
        if sectors < 120 or sectors % 40:
            raise ValueError(
                "Partition size must be >= 120 sectors and a multiple of 40, "
                f"got {sectors}"
            )
        raw_name = unicode_to_petscii(base)
        if not raw_name or len(raw_name) > 16:
            raise ValueError(f"Invalid CBM filename {base!r} (1-16 PETSCII chars)")
        if petscii_to_unicode(raw_name) != base:
            raise ValueError(
                f"Filename is not canonical PETSCII: {base!r} "
                f"(canonical form is {petscii_to_unicode(raw_name)!r})"
            )
        try:
            self._find_entry("/" + base)
        except FileNotFoundError:
            pass
        else:
            raise FileExistsError(f"An entry named {base!r} already exists")
        n_tracks = sectors // 40
        dir_t = self.layout.dir_track
        start = None
        # First fit ascending; the windows stop short of track 40 on the low
        # side and start past it on the high side, so a partition can neither
        # contain nor straddle the directory track.
        for lo, hi in ((1, dir_t - 1), (dir_t + 1, self.layout.tracks)):
            for t0 in range(lo, hi - n_tracks + 2):
                if all(
                    self._bam.free_count(t) == self.layout.spt(t)
                    for t in range(t0, t0 + n_tracks)
                ):
                    start = t0
                    break
            if start is not None:
                break
        if start is None:
            raise OSError("No contiguous free region for partition")
        allocated_ts: list[tuple[int, int]] = []
        try:
            for t in range(start, start + n_tracks):
                for s in range(self.layout.spt(t)):
                    self._bam.set_allocated(t, s)
                    allocated_ts.append((t, s))
        except ValueError:
            for t, s in allocated_ts:
                self._bam.set_free(t, s)
            raise
        raw_name = raw_name.ljust(16, bytes([PETSCII_PAD]))
        self._format_partition_subdir(start, n_tracks, raw_name)
        try:
            dt, ds, k = self._claim_dir_slot()
        except OSError:
            # Directory full: roll the whole range back so the BAM returns
            # to its pre-create state.
            for t in range(start, start + n_tracks):
                for s in range(self.layout.spt(t)):
                    self._bam.set_free(t, s)
            self._bam.flush()
            self.disk.flush()
            raise
        sec = bytearray(self._read_ts(dt, ds))
        off = 0x20 * k
        sec[off + 2] = 0x85  # closed CBM partition
        sec[off + 3], sec[off + 4] = start, 0
        sec[off + 5 : off + 0x15] = raw_name
        sec[off + 0x15 : off + 0x1E] = bytes(9)
        sec[off + 0x1E] = sectors & 0xFF
        sec[off + 0x1F] = sectors >> 8
        self._write_ts(dt, ds, bytes(sec))
        self._bam.flush()
        self.disk.flush()

    def delete(self, path: str) -> None:
        """Scratches a file: zeroes the slot's type byte FIRST, then frees its
        blocks in the BAM. A REL file's blocks include its side sectors (and
        the D81 super side sector). A CBM partition frees its whole contiguous
        range; a partition that qualifies as a sub-directory must be empty
        (delete_recursive deletes it regardless of content).

        The whole block list is validated BEFORE any mutation: a corrupt
        chain (cycle, off-disk link, block on a track without a BAM entry, or
        a block already free, e.g. cross-linked) raises ValueError and leaves
        both the BAM and the directory entry intact so the situation stays
        visible.

        Mutation order is entry-first so a failure mid-free degrades to
        orphaned-allocated blocks (warn-only in check(), what a real VALIDATE
        silently frees) instead of a live entry pointing at freed blocks
        (data-loss risk: the allocator could overwrite them).
        """
        self._delete_impl(path, force=False)

    def _delete_impl(self, path: str, force: bool) -> None:
        self._initialize()
        part, inner = self._resolve(path)
        if part is not None:
            return self._sub_fs(part)._delete_impl("/" + inner, force)
        t, s, k, entry = self._find_entry(path)
        ftype = entry[2] & 0x0F
        if ftype == 5:
            return self._delete_partition(t, s, k, entry, force)
        blocks = self._follow_chain(entry[3], entry[4])
        if ftype == 4 and entry[0x15] != 0:
            # REL: the side-sector structure is part of the file and is
            # validated and freed together with the data chain.
            blocks += list(self._iter_side_sectors(entry[0x15], entry[0x16]))
        seen: set[tuple[int, int]] = set()
        for ct, cs in blocks:
            if ct not in self._bam.mapped_tracks():
                raise ValueError(
                    f"Chain block {ct}/{cs} lies on a track with no BAM entry"
                )
            if self._bam.is_free(ct, cs):
                raise ValueError(f"Chain block {ct}/{cs} is already free in the BAM")
            if (ct, cs) in seen:
                raise ValueError(f"Chain block {ct}/{cs} appears twice in the chain")
            seen.add((ct, cs))
        sec = bytearray(self._read_ts(t, s))
        sec[0x20 * k + 2] = 0x00
        self._write_ts(t, s, bytes(sec))
        try:
            for ct, cs in blocks:
                self._bam.set_free(ct, cs)
        finally:
            # Flush even on a mid-free failure: the scratched entry and any
            # partial frees must reach the disk together.
            self._bam.flush()
            self.disk.flush()

    def delete_recursive(self, path: str) -> bool:
        try:
            self._delete_impl(path, force=True)
            return True
        except FileNotFoundError:
            return False

    def _delete_partition(
        self, t: int, s: int, k: int, entry: bytes, force: bool
    ) -> None:
        """Deletes a CBM partition entry: scratches the slot, then frees the
        whole contiguous range in this directory's BAM. The range is
        pre-validated (on-disk, off the reserved tracks, every block currently
        allocated) before any mutation. A qualifying sub-directory with live
        entries is refused unless `force` (delete_recursive): freeing the
        range implicitly destroys everything inside."""
        name = petscii_to_unicode(entry[5:0x15])
        blocks = self._partition_ts_list(entry)  # validates the run on disk
        for ct, cs in blocks:
            if ct in self.layout.reserved_tracks:
                raise ValueError(
                    f"Partition {name!r} overlaps reserved track {ct}; not deleting"
                )
            if ct not in self._bam.mapped_tracks():
                raise ValueError(
                    f"Partition block {ct}/{cs} lies on a track with no BAM entry"
                )
            if self._bam.is_free(ct, cs):
                raise ValueError(
                    f"Partition block {ct}/{cs} is already free in the BAM"
                )
        if not force and self._is_subdirectory(entry):
            sub = self._sub_fs(entry)
            if any(
                e[2] != 0x00 and e[2] & 0x0F != 0
                for _t, _s, _k, e in sub._iter_entries()
            ):
                raise OSError(f"Partition {name!r} not empty (use recursive delete)")
        sec = bytearray(self._read_ts(t, s))
        sec[0x20 * k + 2] = 0x00
        self._write_ts(t, s, bytes(sec))
        try:
            for ct, cs in blocks:
                self._bam.set_free(ct, cs)
        finally:
            # Flush even on a mid-free failure: the scratched entry and any
            # partial frees must reach the disk together.
            self._bam.flush()
            self.disk.flush()

    def check(self) -> bool:
        """VALIDATE-style consistency check.

        Compares the BAM against the set of blocks the directory structures
        actually own: system sectors, the directory chain, every live entry's
        data chain, REL side sectors (including D81 super side sectors),
        closed-DEL chains (real VALIDATE traces those too) and CBM partition
        ranges. A partition that qualifies as a 1581 sub-directory is
        additionally checked with its own partition-scoped filesystem (its
        BAM governs the partition interior) and ANDed into the verdict.

        Verdict rule (probe-driven, see test_44):
        - FAILURE: a live block marked free in the BAM (data-loss risk: the
          allocator could overwrite it), BAM free counts inconsistent with the
          bitmaps, an unwalkable directory/live-file structure, or a REL
          side-sector structure that contradicts the entry or data chain.
        - WARNING only: allocated-but-unowned (orphaned) blocks. A real DOS
          VALIDATE frees those silently; vintage disks legitimately carry them
          (e.g. 1571_demo has two orphaned all-zero blocks).
        """
        self._initialize()
        expected = {self._bam.header_ts()}
        if self.layout.variant == "1571":
            # 1571 DOS reserves the whole of track 53 at format time (53/0 is
            # the side-2 BAM, the rest stays allocated): all of it is system.
            expected |= {(53, s) for s in range(self.layout.spt(53))}
        if self.layout.variant == "1581":
            # BAM sectors 1 and 2 on the header track: 40 on the root disk,
            # the partition's start track on a sub-directory filesystem.
            ht = self._bam.header_ts()[0]
            expected |= {(ht, 1), (ht, 2)}
        ok = True
        try:
            for t, s, _d in self._iter_dir_sectors():
                expected.add((t, s))
            for _t, _s, _k, entry in self._iter_entries():
                tb = entry[2]
                if tb == 0x00:
                    continue  # scratched slot
                ftype = tb & 0x0F
                if ftype == 0:
                    # Closed DEL entries can own a chain (directory-art and
                    # stash tricks); real VALIDATE traces them. A broken DEL
                    # chain is not a data-loss risk: warn and move on, its
                    # blocks surface as orphans at worst.
                    if tb & 0x80 and entry[3] != 0:
                        try:
                            expected.update(self._follow_chain(entry[3], entry[4]))
                        except ValueError as exc:
                            self.logger.warning(f"check(): broken DEL chain: {exc}")
                    continue
                if ftype == 5:  # CBM partition: contiguous raw blocks
                    expected.update(self._partition_ts_list(entry))
                    if self._is_subdirectory(entry) and not self._sub_fs(entry).check():
                        self.logger.warning(
                            "check(): sub-directory partition "
                            f"{petscii_to_unicode(entry[5:0x15])!r} failed "
                            "its own check"
                        )
                        ok = False
                    continue
                expected.update(self._follow_chain(entry[3], entry[4]))
                if ftype == 4:
                    if entry[0x15] == 0:
                        # No side-sector pointer: readable as a chain but not
                        # usable as a REL by real DOS. Warn only.
                        self.logger.warning(
                            "check(): REL entry "
                            f"{petscii_to_unicode(entry[5:0x15])!r} has no "
                            "side-sector pointer"
                        )
                        continue
                    expected.update(self._iter_side_sectors(entry[0x15], entry[0x16]))
                    if not self._verify_rel_side_sectors(entry):
                        ok = False
        except (OSError, ValueError) as exc:
            self.logger.warning(f"check(): unwalkable structure: {exc}")
            return False
        mapped = set(self._bam.mapped_tracks())
        if self._check_tracks is not None:
            # Sub-directory filesystem: only the partition's own tracks are
            # meaningful (its BAM marks everything outside fully allocated).
            mapped &= self._check_tracks
        allocated = {
            (t, s)
            for t in mapped
            for s in range(self.layout.spt(t))
            if not self._bam.is_free(t, s)
        }
        # Unmapped tracks (e.g. 36-40 on extended 1541 images) have no BAM
        # entry to compare against.
        expected = {ts for ts in expected if ts[0] in mapped}
        missing = expected - allocated  # live data marked free: data-loss risk
        orphaned = allocated - expected  # allocated but unowned: benign cruft
        if missing:
            self.logger.warning(
                f"check(): {len(missing)} live blocks marked free: "
                f"{sorted(missing)[:8]}"
            )
            ok = False
        if orphaned:
            self.logger.warning(
                f"check(): {len(orphaned)} orphaned allocated blocks "
                f"(a real VALIDATE would free them): {sorted(orphaned)[:8]}"
            )
        if not self._bam.verify_counts():
            self.logger.warning("check(): BAM free counts inconsistent with bitmaps")
            ok = False
        return ok

    def format_fs(
        self, profile: FormatProfile, volume_label: Optional[str] = None
    ) -> None:
        """Formats the disk as an empty CBM filesystem (byte-exact CBM DOS
        BAM/header layout). volume_label is 'NAME' or 'NAME,ID' (c1541 style);
        the ID defaults to '00' and is padded with '0' when shorter than 2.
        """
        layout = profile.filesystem_config
        if not isinstance(layout, CBMDiskLayout):
            raise ValueError("Profile has no CBM layout config")
        # Full zone-map gate: the open disk's geometry must infer back to the
        # exact same CBM layout (variant + track count). A shallow cylinders +
        # track-0 spt check would accept uniform non-CBM geometries.
        inferred = CBMDiskLayout.infer_from_geometry(self.disk.physical_format)
        if not CBMDiskLayout.matches(inferred, layout):
            raise ValueError("Profile geometry does not match the open disk")
        label = volume_label or "UNTITLED"
        name, _sep, disk_id = label.partition(",")
        raw_name = unicode_to_petscii(name.upper())
        if not raw_name or len(raw_name) > 16:
            raise ValueError(f"Disk name must be 1-16 PETSCII chars, got {name!r}")
        raw_name = raw_name.ljust(16, bytes([PETSCII_PAD]))
        raw_id = (unicode_to_petscii(disk_id.upper()) if disk_id else b"00")[:2].ljust(
            2, b"0"
        )
        # Wipe first: _write_ts pops any stale key from the old BAM strategy's
        # cache, and the strategy itself is replaced AFTER the wipe so its
        # first _sector() read pulls the freshly-built bytes from the disk.
        for t in range(1, layout.tracks + 1):
            for s in range(layout.spt(t)):
                self._write_ts(t, s, bytes(256))
        self.layout = layout
        self.config = layout
        self._bam = _STRATEGIES[layout.variant](self)
        self._initialized = True
        {
            "1541": self._format_1541,
            "1571": self._format_1571,
            "1581": self._format_1581,
        }[layout.variant](raw_name, raw_id)
        self.disk.flush()

    @staticmethod
    def _all_free_bits(spt: int) -> int:
        return (1 << spt) - 1

    def _format_1541(self, raw_name: bytes, raw_id: bytes, extra=None) -> None:
        layout = self.layout
        bam = bytearray(256)
        bam[0], bam[1], bam[2] = 18, 1, 0x41
        # Extended 1541 images (tracks 36+) get no BAM entries: read-tolerated
        # only, hence the min(..., 35) cap.
        for t in range(1, min(layout.tracks, 35) + 1):
            spt = layout.spt(t)
            e = 0x04 + 4 * (t - 1)
            bits, free = self._all_free_bits(spt), spt
            if t == 18:
                bits &= ~0b11  # 18/0 BAM + 18/1 first directory sector
                free -= 2
            bam[e] = free
            bam[e + 1 : e + 4] = bytes(
                [bits & 0xFF, (bits >> 8) & 0xFF, (bits >> 16) & 0xFF]
            )
        bam[0x90:0xA0] = raw_name
        bam[0xA0:0xA2] = b"\xa0\xa0"
        bam[0xA2:0xA4] = raw_id
        bam[0xA4] = 0xA0
        bam[0xA5:0xA7] = b"2A"
        bam[0xA7:0xAB] = b"\xa0" * 4
        if extra:
            extra(bam)
        self._write_ts(18, 0, bytes(bam))
        self._write_ts(18, 1, self._fresh_dir_sector())

    def _format_1571(self, raw_name: bytes, raw_id: bytes) -> None:
        layout = self.layout

        def extra(bam: bytearray) -> None:
            bam[3] = 0x80  # double-sided flag
            for t in range(36, 71):
                bam[0xDD + (t - 36)] = 0 if t == 53 else layout.spt(t)

        self._format_1541(raw_name, raw_id, extra=extra)
        side = bytearray(256)  # 53/0: side-2 bitmaps, 3 bytes per track
        for t in range(36, 71):
            bits = 0 if t == 53 else self._all_free_bits(layout.spt(t))
            off = 3 * (t - 36)
            side[off : off + 3] = bytes(
                [bits & 0xFF, (bits >> 8) & 0xFF, (bits >> 16) & 0xFF]
            )
        self._write_ts(53, 0, bytes(side))

    @staticmethod
    def _build_1581_header(
        dir_ts: tuple[int, int], raw_name: bytes, raw_id: bytes
    ) -> bytes:
        """1581 header sector (disk root at 40/0 or a partition's at start/0):
        directory pointer, DOS byte 'D', name, ID, '3D' version."""
        hdr = bytearray(256)
        hdr[0], hdr[1], hdr[2] = dir_ts[0], dir_ts[1], 0x44
        hdr[0x04:0x14] = raw_name
        hdr[0x14:0x16] = b"\xa0\xa0"
        hdr[0x16:0x18] = raw_id
        hdr[0x18] = 0xA0
        hdr[0x19], hdr[0x1A] = ord("3"), ord("D")
        hdr[0x1B:0x1D] = b"\xa0\xa0"
        return bytes(hdr)

    def _build_1581_bam_sector(
        self, nxt: tuple[int, int], raw_id: bytes, lo: int, hi: int, free_bits
    ) -> bytes:
        """One 1581 BAM sector covering tracks lo..hi. free_bits(t) returns
        the free-sector bitmap for track t (0 = fully allocated); the count
        byte is its popcount, keeping count and bitmap consistent by
        construction."""
        bam = bytearray(256)
        bam[0], bam[1] = nxt
        bam[2], bam[3] = 0x44, 0xBB  # DOS version, one's complement
        bam[4:6] = raw_id
        bam[6] = 0xC0  # verify on + check header CRC
        for t in range(lo, hi + 1):
            e = 0x10 + 6 * ((t - 1) % 40)
            bits = free_bits(t)
            bam[e] = bin(bits).count("1")
            bam[e + 1 : e + 6] = bits.to_bytes(5, "little")
        return bytes(bam)

    @staticmethod
    def _fresh_dir_sector() -> bytes:
        d = bytearray(256)
        d[1] = 0xFF  # chain end: 8 fresh directory slots
        return bytes(d)

    def _format_1581(self, raw_name: bytes, raw_id: bytes) -> None:
        layout = self.layout

        def free_bits(t: int) -> int:
            bits = self._all_free_bits(layout.spt(t))
            if t == 40:
                bits &= ~0b1111  # header, both BAM sectors, directory
            return bits

        self._write_ts(40, 0, self._build_1581_header((40, 3), raw_name, raw_id))
        self._write_ts(
            40, 1, self._build_1581_bam_sector((40, 2), raw_id, 1, 40, free_bits)
        )
        self._write_ts(
            40, 2, self._build_1581_bam_sector((0, 0xFF), raw_id, 41, 80, free_bits)
        )
        self._write_ts(40, 3, self._fresh_dir_sector())

    def _format_partition_subdir(
        self, start_t: int, n_tracks: int, raw_name: bytes
    ) -> None:
        """Writes a 1581 sub-directory's structures inside a partition at
        tracks start_t..start_t+n_tracks-1: header at (start,0), BAMs at
        (start,1)/(start,2) covering the same global 1-40/41-80 split with
        every out-of-partition track marked fully allocated, directory at
        (start,3). The root disk's ID is reused for simplicity; real 1581
        sub-directories carry their own independent disk ID (e.g. PIC.DIR in
        1581_demo has ID "HR" while the root disk has "GB")."""
        root_hdr = self._read_ts(*self._bam.header_ts())
        raw_id = bytes(root_hdr[0x16:0x18])
        inside = range(start_t, start_t + n_tracks)

        def free_bits(t: int) -> int:
            if t not in inside:
                return 0
            bits = self._all_free_bits(self.layout.spt(t))
            if t == start_t:
                bits &= ~0b1111  # header, both BAM sectors, directory
            return bits

        self._write_ts(
            start_t, 0, self._build_1581_header((start_t, 3), raw_name, raw_id)
        )
        self._write_ts(
            start_t,
            1,
            self._build_1581_bam_sector((start_t, 2), raw_id, 1, 40, free_bits),
        )
        self._write_ts(
            start_t,
            2,
            self._build_1581_bam_sector((0, 0xFF), raw_id, 41, 80, free_bits),
        )
        self._write_ts(start_t, 3, self._fresh_dir_sector())

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
        error_codes = getattr(self.disk.driver, "error_codes", None)
        if error_codes is not None:
            bad = sum(1 for c in error_codes if c not in (0x00, 0x01))
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
        if path in ("", "/"):
            return self._list_root()
        name = path.removeprefix("/")
        if self.layout.variant == "1581" and "/" not in name:
            entry = self._find_root_type5(name)
            if entry is not None:
                if not self._is_subdirectory(entry):
                    raise NotADirectoryError(
                        f"{name} is a raw CBM partition, not a sub-directory"
                    )
                return self._sub_fs(entry)._list_root()
        raise FileNotFoundError(f"No such directory: {path}")

    def _list_root(self) -> list[FileInfo]:
        """Lists this filesystem's own directory (the disk root, or the
        partition's directory on a _sub_fs instance). Qualifying CBM
        sub-directory partitions list as directories; raw partitions stay
        plain files (their whole range is readable as raw bytes)."""
        out = []
        for _t, _s, _k, entry in self._iter_entries():
            info = self._entry_to_fileinfo(entry)
            if info is None:
                continue
            if info.extra_data["ftype"] == 5 and self._is_subdirectory(entry):
                info.is_dir = True
                info.size = 0
                out.append(info)
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

    def _follow_chain(
        self, track: int, sector: int, tolerant: bool = False
    ) -> list[tuple[int, int]]:
        """Walks a T/S chain, returning the visited blocks in order.

        Strict mode (default) raises ValueError on an out-of-range link or a
        cycle - required wherever the chain feeds the BAM (delete, check,
        REL verification). Tolerant mode truncates at the first bad link
        instead: real disks in the corpus carry such chains legitimately
        (the 1541 Test/Demo disk's "CBM" USR file points at a raw data block
        whose first two bytes are content, crack intros leave garbage links),
        and reads must salvage what is recoverable.
        """
        self._initialize()
        chain, seen = [], set()
        t, s = track, sector
        while t != 0:
            if not (1 <= t <= self.layout.tracks) or not (0 <= s < self.layout.spt(t)):
                if tolerant:
                    self.logger.warning(
                        f"Chain points outside disk at {t}/{s}; "
                        f"truncating after {len(chain)} block(s)"
                    )
                    break
                raise ValueError(f"Chain points outside disk at {t}/{s}")
            if (t, s) in seen:
                if tolerant:
                    self.logger.warning(
                        f"Chain cycle at {t}/{s}; "
                        f"truncating after {len(chain)} block(s)"
                    )
                    break
                raise ValueError(f"Chain cycle at {t}/{s}")
            seen.add((t, s))
            chain.append((t, s))
            data = self._read_ts(t, s)
            t, s = data[0], data[1]
        return chain

    def _read_chain(self, track: int, sector: int) -> bytes:
        # Tolerant: a broken link ends the chain at the last valid block,
        # whose full 254-byte payload is kept (its link bytes are data).
        out = bytearray()
        for t, s in self._follow_chain(track, sector, tolerant=True):
            data = self._read_ts(t, s)
            if data[0] == 0:
                last = data[1]
                if last == 0:
                    self.logger.warning(
                        f"Last sector {t}/{s} claims 0 valid bytes; treating as empty"
                    )
                    continue
                if last == 1:
                    continue  # canonical empty encoding: pointer 1, no payload
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

    # -- partitions (1581 sub-directories) -----------------------------------------

    def _partition_track_range(self, entry: bytes) -> Optional[tuple[int, int]]:
        """(start_track, n_tracks) of a whole-track-aligned CBM partition, or
        None when the entry is not track-aligned (start sector != 0, size not
        a whole number of tracks) or the range runs off the disk."""
        sectors = entry[0x1E] | (entry[0x1F] << 8)
        start_t, start_s = entry[3], entry[4]
        if start_s != 0 or sectors == 0 or not 1 <= start_t <= self.layout.tracks:
            return None
        t, total = start_t, 0
        while total < sectors:
            if t > self.layout.tracks:
                return None
            total += self.layout.spt(t)
            t += 1
        if total != sectors:
            return None
        return start_t, t - start_t

    def _is_subdirectory(self, entry: bytes) -> bool:
        """True when a CBM partition entry qualifies as a 1581 sub-directory:
        whole-track aligned, >= 120 sectors (3 tracks), track range excludes
        the root directory track 40, and the partition interior carries its
        own 1581 header at (start,0) and BAM at (start,1)."""
        if self.layout.variant != "1581":
            return False
        rng = self._partition_track_range(entry)
        if rng is None:
            return False
        t0, n = rng
        if n < 3 or t0 <= self.layout.dir_track <= t0 + n - 1:
            return False
        try:
            hdr = self._read_ts(t0, 0)
            bam = self._read_ts(t0, 1)
        except (OSError, ValueError):
            return False
        return hdr[2] == 0x44 and bam[2] == 0x44 and bam[3] == 0xBB

    def _sub_fs(self, entry: bytes) -> "CBMFilesystem":
        """Filesystem view of a qualifying 1581 sub-directory partition: the
        SAME disk, a partition-scoped layout (directory at start/3; every
        track outside the partition plus the start track itself reserved, so
        the allocator only touches partition data tracks) and a _Bam1581
        redirected at the partition's own header/BAM sectors."""
        t0, n = self._partition_track_range(entry)
        inside = set(range(t0, t0 + n))
        sub_layout = dataclasses.replace(
            self.layout,
            dir_track=t0,
            dir_sector=3,
            reserved_tracks=tuple(
                t
                for t in range(1, self.layout.tracks + 1)
                if t not in inside or t == t0
            ),
        )
        sub = CBMFilesystem(self.disk, config=sub_layout)
        sub.layout = sub_layout
        sub._bam = _Bam1581(
            sub,
            sector_map={(40, 1): (t0, 1), (40, 2): (t0, 2)},
            header=(t0, 0),
        )
        sub._check_tracks = inside
        sub._initialized = True
        return sub

    def _find_root_type5(self, name: str) -> Optional[bytes]:
        """First visible CBM-partition (type 5) entry named `name`, or None."""
        for _t, _s, _k, entry in self._iter_entries():
            if entry[2] == 0x00 or entry[2] & 0x0F == 0:
                continue
            if entry[2] & 0x0F == 5 and petscii_to_unicode(entry[5:0x15]) == name:
                return entry
        return None

    def _resolve(self, path: str) -> tuple[Optional[bytes], str]:
        """Routes '/PARTITION/NAME' paths one level deep on 1581 disks.

        Returns (partition_entry, inner_name) when the first path component
        names a qualifying sub-directory partition; otherwise (None,
        full_name) so the caller looks the whole string up in this directory
        (CBM names may legally contain '/'). Raises NotADirectoryError when
        the component names a raw (unformatted) partition and
        FileNotFoundError for deeper nesting: nested partitions exist on real
        1581 disks but are not traversable here."""
        self._initialize()
        name = path.removeprefix("/")
        if self.layout.variant == "1581" and "/" in name:
            comp0, _slash, rest = name.partition("/")
            entry = self._find_root_type5(comp0)
            if entry is not None:
                if "/" in rest:
                    raise FileNotFoundError(
                        f"Nested partition paths are not supported: {path}"
                    )
                if not self._is_subdirectory(entry):
                    raise NotADirectoryError(
                        f"{comp0} is a raw CBM partition, not a sub-directory"
                    )
                return entry, rest
        return None, name

    def _iter_side_sectors(self, t: int, s: int):
        """Yields every side-sector (and super side sector) T/S of a REL entry.

        D64/D71 RELs point straight at a plain side-sector chain; D81 RELs
        point at a super side sector (byte 2 == 0xFE) holding up to 126 group
        pointers, each the head of a side-sector chain. Range checks and cycle
        guards come from _follow_chain; the super sector itself is validated
        here before the first read.
        """
        self._initialize()
        if not (1 <= t <= self.layout.tracks) or not (0 <= s < self.layout.spt(t)):
            raise ValueError(f"Side sector pointer outside disk at {t}/{s}")
        first = self._read_ts(t, s)
        if first[2] == 0xFE:  # D81 super side sector
            yield (t, s)
            for g in range(126):
                gt, gs = first[3 + 2 * g], first[4 + 2 * g]
                if gt == 0:
                    break
                yield from self._follow_chain(gt, gs)
        else:
            yield from self._follow_chain(t, s)

    def _verify_rel_side_sectors(self, entry: bytes) -> bool:
        """Cross-checks a REL entry's side-sector structure: every side
        sector's record length must match the entry's (+0x17), and the
        data-block T/S pairs listed across the side sectors must equal the
        data chain in order. Returns False (check() failure) on mismatch."""
        reclen = entry[0x17]
        chain = self._follow_chain(entry[3], entry[4])
        listed: list[tuple[int, int]] = []
        for t, s in self._iter_side_sectors(entry[0x15], entry[0x16]):
            sec = self._read_ts(t, s)
            if sec[2] == self.SSS_MARKER:
                continue  # D81 super side sector: group pointers, no data T/S
            if sec[3] != reclen:
                self.logger.warning(
                    f"check(): REL side sector {t}/{s} record length {sec[3]} "
                    f"!= directory entry's {reclen}"
                )
                return False
            # Non-last side sectors carry a full table; the last one's byte 1
            # is the index of its last valid byte (bytes beyond may be stale
            # on real disks, so never read past it).
            end = 0x10 + 2 * self.SS_POINTERS if sec[0] != 0 else sec[1] + 1
            for off in range(0x10, min(end, 255), 2):
                if sec[off] == 0:
                    break
                listed.append((sec[off], sec[off + 1]))
        if listed != chain:
            self.logger.warning(
                "check(): REL side-sector data-block list does not match the "
                f"data chain ({len(listed)} listed vs {len(chain)} chained)"
            )
            return False
        return True

    # -- allocation --------------------------------------------------------------

    def _track_search_order(self) -> list[int]:
        """File-allocation track order: 1581 linear; others distance-ascending
        from the directory track, lower side first (17, 19, 16, 20, ...)."""
        if self.layout.variant == "1581":
            return [
                t
                for t in range(1, self.layout.tracks + 1)
                if t not in self.layout.reserved_tracks
            ]
        d = self.layout.dir_track
        order = []
        for delta in range(1, self.layout.tracks):
            for t in (d - delta, d + delta):
                if (
                    1 <= t <= self.layout.tracks
                    and t not in self.layout.reserved_tracks
                ):
                    order.append(t)
        return order

    def _allocate_in_track(self, t: int, start: int = 0) -> Optional[tuple[int, int]]:
        spt = self.layout.spt(t)
        s = start % spt
        for _ in range(spt):
            if self._bam.is_free(t, s):
                self._bam.set_allocated(t, s)
                return t, s
            s = (s + 1) % spt
        return None

    def _allocate_first_sector(self) -> tuple[int, int]:
        for t in self._track_search_order():
            if self._bam.free_count(t) > 0:
                got = self._allocate_in_track(t)
                if got:
                    return got
        raise OSError("Disk full")

    def _allocate_next_sector(self, last_t: int, last_s: int) -> tuple[int, int]:
        if self._bam.free_count(last_t) > 0:
            got = self._allocate_in_track(
                last_t, (last_s + self.layout.interleave) % self.layout.spt(last_t)
            )
            if got:
                return got
        for t in self._track_search_order():
            if self._bam.free_count(t) > 0:
                got = self._allocate_in_track(t)
                if got:
                    return got
        raise OSError("Disk full")

    # -- write path --------------------------------------------------------------

    _TYPE_SUFFIXES: ClassVar[dict[str, int]] = {"p": 2, "s": 1, "u": 3, "r": 4}
    SS_POINTERS = 120  # data-block T/S pairs per side sector
    MAX_SIDE_SECTORS = 6  # per group: 6 * 120 = 720 data blocks
    SSS_MARKER = 0xFE  # byte 2 of a D81 super side sector

    @staticmethod
    def _split_type_suffix(name: str) -> tuple[str, int, int]:
        """Splits a c1541-style type suffix: 'NAME,s' -> (NAME, 1, 0);
        'NAME,r:100' -> (NAME, 4, 100). Defaults to PRG when there is no
        recognizable suffix; a comma that does not parse as a type code stays
        part of the filename (e.g. 'A,B'). REL record length is validated
        here (1-254)."""
        base, sep, suffix = name.rpartition(",")
        if sep and suffix:
            code, _colon, arg = suffix.partition(":")
            ftype = CBMFilesystem._TYPE_SUFFIXES.get(code.lower())
            if ftype is not None and (not arg or code.lower() == "r"):
                reclen = 0
                if ftype == 4:
                    if not arg:
                        raise ValueError("REL files need ',r:<record-length>'")
                    try:
                        reclen = int(arg)
                    except ValueError:
                        raise ValueError(
                            f"REL record length must be a number, got {arg!r}"
                        ) from None
                    if not 1 <= reclen <= 254:
                        raise ValueError("REL record length must be 1-254")
                return base, ftype, reclen
        return name, 2, 0

    def _write_chain(self, chunks: list[bytes]) -> list[tuple[int, int]]:
        """Allocates and writes a linked sector chain; frees everything
        allocated so far if the disk fills up mid-allocation."""
        chain: list[tuple[int, int]] = []
        try:
            for _ in chunks:
                ts = (
                    self._allocate_first_sector()
                    if not chain
                    else self._allocate_next_sector(*chain[-1])
                )
                chain.append(ts)
        except OSError:
            self._rollback(chain)
            raise
        try:
            for i, (t, s) in enumerate(chain):
                sec = bytearray(256)
                if i + 1 < len(chain):
                    sec[0], sec[1] = chain[i + 1]
                    sec[2:256] = chunks[i].ljust(PAYLOAD, b"\x00")
                else:
                    sec[0], sec[1] = 0, len(chunks[i]) + 1
                    sec[2 : 2 + len(chunks[i])] = chunks[i]
                self._write_ts(t, s, bytes(sec))
        except OSError:
            self._rollback(chain)
            raise
        return chain

    def _rollback(self, chain: list[tuple[int, int]]) -> None:
        """Frees a partially-allocated chain so the BAM returns to its
        pre-write state (counts and bitmaps both)."""
        for t, s in chain:
            try:
                self._bam.set_free(t, s)
            except ValueError:
                self.logger.warning(f"Rollback: {t}/{s} was already free")

    def _build_side_sector_group(
        self, blocks: list[tuple[int, int]], reclen: int
    ) -> list[tuple[int, int]]:
        """Allocates and writes one side-sector group (max 6 side sectors
        covering max 720 data blocks); returns the side-sector T/S list.
        Frees its own partial allocations on failure, so the caller only
        rolls back what it actually received."""
        n_ss = -(-len(blocks) // self.SS_POINTERS)  # ceil; blocks never empty
        if n_ss > self.MAX_SIDE_SECTORS:
            raise OSError("REL file too large for one side-sector group")
        ss_ts: list[tuple[int, int]] = []
        try:
            for _ in range(n_ss):
                ss_ts.append(
                    self._allocate_first_sector()
                    if not ss_ts
                    else self._allocate_next_sector(*ss_ts[-1])
                )
            # Bytes 4..15 of EVERY side sector list ALL side sectors of the
            # group (unused pairs stay 0,0).
            table = bytearray(2 * self.MAX_SIDE_SECTORS)
            for i, (t, s) in enumerate(ss_ts):
                table[2 * i], table[2 * i + 1] = t, s
            for i, (t, s) in enumerate(ss_ts):
                covered = blocks[i * self.SS_POINTERS : (i + 1) * self.SS_POINTERS]
                sec = bytearray(256)
                if i + 1 < n_ss:
                    sec[0], sec[1] = ss_ts[i + 1]
                else:
                    sec[0], sec[1] = 0, 0x10 + 2 * len(covered) - 1
                sec[2], sec[3] = i, reclen
                sec[4:16] = table
                for j, (bt, bs) in enumerate(covered):
                    sec[0x10 + 2 * j], sec[0x11 + 2 * j] = bt, bs
                self._write_ts(t, s, bytes(sec))
        except OSError:
            self._rollback(ss_ts)
            raise
        return ss_ts

    def _write_rel_file(
        self, base: str, raw_name: bytes, data: bytes, reclen: int
    ) -> None:
        """Writes a REL file: a normal data chain plus side sectors (D64/D71)
        or a super side sector with per-group side-sector chains (D81).

        Records of length reclen pack contiguously across 254-byte payloads
        (records span sector boundaries); a partial final record is padded
        with 0x00 to the record boundary. Empty data writes exactly one
        record of zeros, mirroring CBM DOS initializing record 1 on a fresh
        REL file. The directory block count includes data blocks, all side
        sectors, and (on D81) the super side sector."""
        self._scratch_existing(base)
        if not data:
            data = bytes(reclen)  # new REL: one blank record
        if len(data) % reclen:
            data += bytes(reclen - len(data) % reclen)
        chunks = [data[i : i + PAYLOAD] for i in range(0, len(data), PAYLOAD)]
        allocated: list[tuple[int, int]] = []
        try:
            chain = self._write_chain(chunks)
            allocated += chain
            if self.layout.variant == "1581":
                group_size = self.MAX_SIDE_SECTORS * self.SS_POINTERS
                groups = [
                    chain[i : i + group_size] for i in range(0, len(chain), group_size)
                ]
                if len(groups) > 126:  # SSS holds 126 group pointers
                    raise OSError("REL file too large")
                group_ss: list[list[tuple[int, int]]] = []
                for g in groups:
                    ss = self._build_side_sector_group(g, reclen)
                    group_ss.append(ss)
                    allocated += ss
                sss_ts = self._allocate_next_sector(*group_ss[-1][-1])
                allocated.append(sss_ts)
                sss = bytearray(256)
                # Group 0's pointer appears BOTH at bytes 0/1 and 3/4.
                sss[0], sss[1] = group_ss[0][0]
                sss[2] = self.SSS_MARKER
                for g, ss in enumerate(group_ss):
                    sss[3 + 2 * g], sss[4 + 2 * g] = ss[0]
                self._write_ts(*sss_ts, bytes(sss))
                anchor = sss_ts
                n_extra = sum(len(ss) for ss in group_ss) + 1
            else:
                if len(chain) > self.MAX_SIDE_SECTORS * self.SS_POINTERS:
                    raise OSError("REL file too large for 1541/1571 (720 blocks max)")
                ss = self._build_side_sector_group(chain, reclen)
                allocated += ss
                anchor = ss[0]
                n_extra = len(ss)
            # _claim_dir_slot last: if it raises (directory full) it has
            # allocated nothing itself, so rolling back `allocated` is
            # complete; if it extends the directory it returns successfully
            # and the extension is a valid dir sector regardless.
            t, s, k = self._claim_dir_slot()
        except OSError:
            self._rollback(allocated)
            self._bam.flush()
            self.disk.flush()
            raise
        sec = bytearray(self._read_ts(t, s))
        off = 0x20 * k
        total = len(chain) + n_extra
        sec[off + 2] = 0x84  # closed REL
        sec[off + 3], sec[off + 4] = chain[0]
        sec[off + 5 : off + 0x15] = raw_name.ljust(16, bytes([PETSCII_PAD]))
        sec[off + 0x15], sec[off + 0x16] = anchor
        sec[off + 0x17] = reclen
        sec[off + 0x18 : off + 0x1E] = bytes(6)  # no @-replacement tracking
        sec[off + 0x1E] = total & 0xFF
        sec[off + 0x1F] = total >> 8
        self._write_ts(t, s, bytes(sec))
        self._bam.flush()
        self.disk.flush()

    def _scratch_existing(self, base: str) -> None:
        """Scratch-and-replace prelude: deletes an existing entry named
        `base`, refusing to clobber a CBM partition (real DOS refuses to
        overwrite a CBM type with a file)."""
        try:
            _t, _s, _k, entry = self._find_entry("/" + base)
        except FileNotFoundError:
            return
        if entry[2] & 0x0F == 5:
            raise ValueError("Cannot overwrite a partition with a file")
        self.delete("/" + base)

    def _claim_dir_slot(self) -> tuple[int, int, int]:
        """Returns (track, sector, slot) of a free directory slot, reusing a
        scratched slot anywhere in the chain or extending the chain within the
        directory track (dir_interleave) when every slot is taken.

        The extension's set_allocated lives in the BAM strategy buffer keyed
        by the BAM sector (e.g. (18,0) on a 1541), while the subsequent
        _write_ts of the new directory sector pops only its own (track,
        sector) key -- the pending allocation is never discarded.
        """
        self._initialize()
        count = 0
        last = None
        for t, s, data in self._iter_dir_sectors():
            for k in range(8):
                count += 1
                if count > self.layout.max_dir_entries:
                    raise OSError("Directory full")
                if data[0x20 * k + 2] == 0x00:
                    return t, s, k
            last = (t, s)
        if count + 1 > self.layout.max_dir_entries:
            raise OSError("Directory full")
        lt, ls = last
        spt = self.layout.spt(lt)
        s = (ls + self.layout.dir_interleave) % spt
        for _ in range(spt):
            if self._bam.is_free(lt, s):
                self._bam.set_allocated(lt, s)
                new = bytearray(256)
                new[1] = 0xFF  # chain end: 8 fresh slots
                self._write_ts(lt, s, bytes(new))
                old = bytearray(self._read_ts(lt, ls))
                old[0], old[1] = lt, s
                self._write_ts(lt, ls, bytes(old))
                return lt, s, 0
            s = (s + 1) % spt
        raise OSError("Directory full (track exhausted)")

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
        part, inner = self._resolve(path)
        if part is not None:
            return self._sub_fs(part).read_file("/" + inner)
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
            part, inner = self._resolve(path)
            if part is not None:
                return self._sub_fs(part).get_file_allocation_units("/" + inner)
            _t, _s, _k, entry = self._find_entry(path)
            if (entry[2] & 0x0F) == 5:
                blocks = self._partition_ts_list(entry)
            else:
                blocks = self._follow_chain(entry[3], entry[4])
            return [self.layout.linear_index(t, s) for t, s in blocks]
        except (FileNotFoundError, NotADirectoryError, ValueError):
            return []

    def write_file(self, path: str, data: bytes) -> None:
        """Writes a file, with c1541-style type suffixes (',p' PRG default,
        ',s' SEQ, ',u' USR, ',r:<reclen>' REL).

        Scratch-and-replace semantics, scratch FIRST like real CBM DOS ('@0:'):
        an existing file of the same name is deleted before the new chain is
        allocated, so large rewrites fit in the freed space. The trade-off: a
        mid-write disk-full leaves the old file deleted -- but the rollback
        still frees the new partial chain, so the BAM stays consistent.
        A name collision with a CBM partition raises ValueError (real DOS
        refuses to overwrite a CBM type with a file).
        """
        self._initialize()
        part, inner = self._resolve(path)
        if part is not None:
            return self._sub_fs(part).write_file("/" + inner, data)
        base, ftype, reclen = self._split_type_suffix(inner)
        raw_name = unicode_to_petscii(base)
        if not raw_name or len(raw_name) > 16:
            raise ValueError(f"Invalid CBM filename {base!r} (1-16 PETSCII chars)")
        if petscii_to_unicode(raw_name) != base:
            # Reject ~hh aliases of mappable chars and $A0 pad bytes in names:
            # scratch-and-replace matches on the decoded form, so an alias
            # would silently coexist with its canonical twin.
            raise ValueError(
                f"Filename is not canonical PETSCII: {base!r} "
                f"(canonical form is {petscii_to_unicode(raw_name)!r})"
            )
        if ftype == 4:
            return self._write_rel_file(base, raw_name, data, reclen)
        self._scratch_existing(base)
        chunks = [data[i : i + PAYLOAD] for i in range(0, len(data), PAYLOAD)] or [b""]
        chain = self._write_chain(chunks)
        try:
            t, s, k = self._claim_dir_slot()
        except OSError:
            self._rollback(chain)
            self._bam.flush()
            self.disk.flush()
            raise
        sec = bytearray(self._read_ts(t, s))
        off = 0x20 * k
        sec[off + 2] = 0x80 | ftype  # closed file
        sec[off + 3], sec[off + 4] = chain[0]
        sec[off + 5 : off + 0x15] = raw_name.ljust(16, bytes([PETSCII_PAD]))
        sec[off + 0x15 : off + 0x1E] = bytes(9)  # side-sector/reclen/unused
        sec[off + 0x1E] = len(chain) & 0xFF
        sec[off + 0x1F] = len(chain) >> 8
        self._write_ts(t, s, bytes(sec))
        self._bam.flush()
        self.disk.flush()

    # -- name policy -------------------------------------------------------------

    def suggest_import_name(
        self,
        host_name: str,
        existing_names: Iterable[str],
        is_dir: bool = False,  # noqa: ARG002
    ) -> str:
        """
        CBM names: up to 16 PETSCII characters, no extension concept. ',' becomes
        '.' so the result can never parse as a ,p/,s/,u/,r: type suffix; '~' is
        the codec's escape introducer and is never emitted; characters without a
        PETSCII mapping become '-'. Uniqueness uses a '-NN' suffix.
        """
        existing = {n.upper() for n in existing_names}
        out = []
        for ch in host_name.upper():
            if ch == ",":
                out.append(".")
            elif ch == "~" or ch not in _U2P:
                out.append("-")
            else:
                out.append(ch)
        name = "".join(out).strip()[:16].rstrip() or "-FILE"
        if name.upper() not in existing:
            return name
        for counter in range(1, 100):
            suffix = f"-{counter:02d}"
            candidate = name[: 16 - len(suffix)] + suffix
            if candidate.upper() not in existing:
                return candidate
        raise ValueError(f"Cannot generate unique CBM name for {host_name!r}")

    def name_hint(self) -> str:
        return "up to 16 chars; optional ,p ,s ,u ,r:<len> type suffix"
