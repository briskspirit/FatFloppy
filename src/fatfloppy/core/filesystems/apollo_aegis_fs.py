"""Read-only Apollo AEGIS native filesystem (SR9 floppies).

Serves the object tree parsed by :mod:`fatfloppy.core.apollo_aegis`
("spec section N" here and below: the maintainer's private AEGIS write-up,
see that module's docstring for the public sources):

- The root is the volume entry directory (``vtoc_hdr.root_dir_vtocx``); the
  network-root wrapper (``//NODE_xxxx``) is display-info metadata ("Node"),
  never a path component.
- Names are listed verbatim as stored (UPPERCASE on SR9 volumes); lookup is
  case-sensitive first with a case-insensitive fallback (the catalog's
  policy).
- ``FileInfo.datetime`` is the VTOCE dtm as a NAIVE UTC datetime (house
  convention), falling back to the object's UID-creation time, then the
  Apollo epoch 1980-01-01.
- ``FileInfo.attributes`` names the canned object type (``TEXT``, ``OBJ``,
  ``SYSBOOT``, ``REC``, ``HDRU``, ``DIR-OBJ``, ``RAW``; ``DIR`` for
  directories) plus ``DMG`` when the directory entry's VTOCE is missing or
  its file map references out-of-range daddrs.
- Managed types (uasc/rec/hdru) hide their 32-byte storage header: sizes
  and reads are header-stripped (the catalog's policy, same UX as wbak).
- ``get_free_space`` returns ``(0, IMAGE_SIZE)`` like the wbak filesystem:
  the volume is read-only, so 0 bytes are importable and the GUI space
  panel shows the medium capacity.  The BAT's free-block count is a
  volume statistic, reported in ``get_display_info`` under
  "Free Blocks (BAT)" instead.
- Catalog degradations (``build_catalog`` warnings, e.g. an unreadable root
  directory on a still-claimed volume that would otherwise browse as merely
  empty) are surfaced: logged at warning level during initialization and
  shown in ``get_display_info`` as a "Volume Damage" row, present only when
  damage was detected.
- ``check()`` reconciles block ownership (labels, BAT, VTOC, index blocks,
  every in-use VTOCE's pages) against the BAT bitmap: 0 mismatches expected
  on clean volumes (disk5 reconciles perfectly); any mismatch is logged;
  more than 1% of the covered blocks mismatching -> False; structural
  failure (unparseable VTOC/root) -> False.
- All mutating operations raise ``OSError("AEGIS volumes are read-only")``.
"""

import struct
from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar, Optional

from ..apollo_aegis import (
    BLOCK,
    DIR_EXPECTED_HEAD,
    KIND_ACL,
    PV_MAGIC,
    AegisCatalog,
    AegisNode,
    build_catalog,
    object_daddrs,
    parse_lv_label,
    parse_pv_label,
)
from ..apollo_wbak import (
    CYLINDERS,
    HEADS,
    IMAGE_SIZE,
    SECTORS_PER_TRACK,
    apollo_time_to_datetime,
)
from ..format_profile import FormatProfile
from .fs_base import FileInfo, Filesystem

_READ_ONLY_MSG = "AEGIS volumes are read-only"
_APOLLO_EPOCH = datetime(1980, 1, 1)
_TOTAL_SECTORS = CYLINDERS * HEADS * SECTORS_PER_TRACK
_BITS_PER_BAT_BLOCK = BLOCK * 8

# Canned type UIDs (high word) -> display attribute (spec section 4).
_TYPE_NAMES = {
    0x311: "TEXT",  # uasc
    0x302: "OBJ",  # executable object
    0x315: "SYSBOOT",
    0x300: "REC",
    0x301: "HDRU",
    0x312: "DIR-OBJ",
    0x000: "RAW",
}

# Sector classification precedence for the disk map: object data outranks
# free, directories outrank plain data on shared blocks (they never share
# on real volumes), damage stays visible over data, and the volume's own
# structures (labels/BAT/VTOC/index blocks) outrank everything.
_CLASS_RANK = {"free": 0, "file": 1, "directory": 2, "damaged": 3, "system": 4}

# check() tolerance: mismatching blocks beyond this fraction of the
# BAT-covered range fail the volume (pinned rule, spec section 4).
_MISMATCH_TOLERANCE = 0.01


@dataclass
class AegisConfig:
    """Detected-volume descriptor (read-only; nothing is configurable).

    The shipped format profile carries the ``lv_uid=None`` sentinel:
    LV UIDs are per-disk, not per-format, so the sentinel matches any
    parsed volume in :meth:`ApolloAegisFilesystem.configs_match` (the
    wbak ``volume_id`` sentinel pattern).
    """

    volume_name: Optional[str] = None
    lv_uid: Optional[str] = None


class ApolloAegisFilesystem(Filesystem):
    """Read-only filesystem over an Apollo AEGIS native volume catalog."""

    filesystem_type: ClassVar[str] = "APOLLO_AEGIS"
    filesystem_aliases: ClassVar[list[str]] = ["AEGIS"]
    validity_threshold: ClassVar[int] = 40
    config_class: ClassVar[Optional[type]] = AegisConfig

    def __init__(self, disk, config: Optional[Any] = None):
        super().__init__(disk, config)
        self._image: Optional[bytes] = None
        self._catalog: Optional[AegisCatalog] = None
        self._account_cache: Optional[tuple[list[str], int, int]] = None
        self._initialized = False

    # ------------------------------------------------------------------
    # Initialization
    # ------------------------------------------------------------------

    def _read_image(self) -> bytes:
        """The whole 1232-block image, read once via the disk and cached."""
        if self._image is None:
            chunks = []
            for cylinder in range(CYLINDERS):
                for head in range(HEADS):
                    for sector in range(SECTORS_PER_TRACK):
                        chunks.append(self.disk.read_sector(cylinder, head, sector))
            self._image = b"".join(chunks)
        return self._image

    def _initialize(self) -> None:
        if self._initialized:
            return
        if not self.disk or not self.disk.physical_format:
            raise ValueError("No disk or geometry available")
        if not self._geometry_is_apollo():
            raise ValueError("Disk geometry is not the Apollo 77x2x8x1024 layout")
        self._catalog = build_catalog(self._read_image())
        if self._catalog.warnings:
            # The catalog flags-and-continues past damage (a broken root
            # directory still claims and browses, possibly empty); surface
            # the degradation here so the volume never looks merely empty.
            self.logger.warning(
                f"Volume {self._catalog.lv.name!r} has structural damage: "
                f"{self._damage_summary()}"
            )
        self._initialized = True

    def _damage_summary(self) -> str:
        """First catalog degradation message, plus a count of the rest."""
        warnings = self._catalog.warnings
        summary = warnings[0]
        if len(warnings) > 1:
            summary += f" (+{len(warnings) - 1} more)"
        return summary

    def _geometry_is_apollo(self) -> bool:
        pf = self.disk.physical_format
        try:
            return (
                pf.cylinders == CYLINDERS
                and pf.heads == HEADS
                and pf.bytes_per_sector == BLOCK
                and pf.get_sectors_per_track(0, 0) == SECTORS_PER_TRACK
            )
        except (ValueError, AttributeError):
            return False

    # ------------------------------------------------------------------
    # Listing / reading
    # ------------------------------------------------------------------

    def _lookup(self, path: str) -> AegisNode:
        """Catalog lookup; FileNotFoundError propagates for missing paths."""
        self._initialize()
        return self._catalog.lookup(path)

    def list_directory(self, path: str) -> list[FileInfo]:
        node = self._lookup(path)
        if not node.is_dir:
            raise NotADirectoryError(f"Not a directory: {path}")
        return [self._node_to_fileinfo(child) for child in node.children]

    def read_file(self, path: str) -> bytes:
        node = self._lookup(path)
        if node.is_dir:
            raise IsADirectoryError(f"Path is a directory: {path}")
        if node.missing:
            self.logger.warning(
                f"{path}: directory entry has no VTOCE (damaged volume); "
                "returning empty content"
            )
        elif node.map_damaged:
            self.logger.warning(
                f"{path}: file map references out-of-range blocks; "
                "damaged pages are zero-filled"
            )
        return self._catalog.read(node)

    def _node_to_fileinfo(self, node: AegisNode) -> FileInfo:
        vtoce = node.vtoce
        markers = []
        if node.is_dir:
            markers.append("DIR")
        elif vtoce is not None:
            markers.append(
                _TYPE_NAMES.get(vtoce.type_uid_hi, f"0x{vtoce.type_uid_hi:X}")
            )
        if node.missing or node.map_damaged:
            markers.append("DMG")
        created = None
        extra: dict[str, Any] = {"kind": None}
        if vtoce is not None:
            created = self._uid_created(vtoce.uid)
            extra = {
                "uid": vtoce.uid_text,
                "type_uid": f"{int.from_bytes(vtoce.type_uid[:4], 'big'):08X}."
                f"{int.from_bytes(vtoce.type_uid[4:], 'big'):08X}",
                "acl_uid": f"{int.from_bytes(vtoce.acl_uid[:4], 'big'):08X}."
                f"{int.from_bytes(vtoce.acl_uid[4:], 'big'):08X}",
                "dtu": vtoce.dtu,
                "created": created,
                "parent_uid": vtoce.parent_uid_text,
                "kind": vtoce.kind,
            }
        if vtoce is not None and vtoce.dtm is not None:
            dt = vtoce.dtm
        elif created is not None:
            dt = created
        else:
            dt = _APOLLO_EPOCH
        first_page = next((d for d in node.page_daddrs if d), 0)
        return FileInfo(
            name=node.name,
            size=0 if node.is_dir else node.size,
            is_dir=node.is_dir,
            datetime=dt,
            attributes=" ".join(markers),
            starting_cluster=first_page + self._catalog.lv_base if first_page else 0,
            extra_data=extra,
        )

    @staticmethod
    def _uid_created(uid: bytes) -> Optional[datetime]:
        """Object creation time from the UID's high 32 bits (spec sec. 2)."""
        hi32 = int.from_bytes(uid[:4], "big")
        if hi32 == 0:
            return None
        return apollo_time_to_datetime(hi32).replace(tzinfo=None)

    # ------------------------------------------------------------------
    # Mutators: read-only
    # ------------------------------------------------------------------

    def write_file(self, _path: str, _data: bytes) -> None:
        raise OSError(_READ_ONLY_MSG)

    def delete(self, _path: str) -> None:
        raise OSError(_READ_ONLY_MSG)

    def delete_recursive(self, _path: str) -> bool:
        raise OSError(_READ_ONLY_MSG)

    def create_directory(self, _path: str) -> None:
        raise OSError(_READ_ONLY_MSG)

    def format_fs(
        self, _profile: FormatProfile, _volume_label: Optional[str] = None
    ) -> None:
        raise OSError(_READ_ONLY_MSG)

    # ------------------------------------------------------------------
    # Block accounting (shared by the disk map and check())
    # ------------------------------------------------------------------

    def _bat_bits(self) -> list[bool]:
        """The BAT bitmap as a bool per covered LV daddr (True = free).

        Bit ``i`` (LSB-first within big-endian 32-bit words, empirically
        verified on disk5) represents LV daddr ``bat_first_covered + i``.
        """
        lv = self._catalog.lv
        data = self._read_image()
        n_blocks = -(-lv.bat_blocks_covered // _BITS_PER_BAT_BLOCK)
        start = (lv.bat_daddr + self._catalog.lv_base) * BLOCK
        raw = data[start : start + n_blocks * BLOCK]
        if len(raw) < n_blocks * BLOCK or lv.bat_blocks_covered <= 0:
            raise ValueError("BAT bitmap out of volume range")
        bits = []
        for i in range(lv.bat_blocks_covered):
            word = int.from_bytes(raw[(i // 32) * 4 : (i // 32) * 4 + 4], "big")
            bits.append(bool((word >> (i % 32)) & 1))
        return bits

    def _account(self) -> tuple[list[str], int, int]:
        """Classify every absolute sector and reconcile against the BAT.

        Returns ``(classes, mismatches, free_bits)`` where ``classes`` maps
        each absolute sector to one of system/directory/file/free/damaged,
        ``mismatches`` counts BAT-covered blocks whose free bit contradicts
        the ownership walk, and ``free_bits`` is the bitmap's free count.

        Ownership is recomputed from the full walk: PV/LV/alternate labels,
        BAT blocks, VTOC blocks, and every in-use VTOCE's pages and
        L1/L2/L3 index blocks (ACL objects -- security metadata, hidden
        from listings -- classify as system; directories as directory;
        everything else, SYSBOOT included, as file; unreferenced orphans
        classify by their kind).  Covered blocks the BAT marks allocated
        but nobody owns surface as "damaged".
        """
        if self._account_cache is not None:
            return self._account_cache
        cat = self._catalog
        lv_base = cat.lv_base
        data = self._read_image()
        classes = ["free"] * _TOTAL_SECTORS

        def claim(abs_sector: int, kind: str) -> None:
            if (
                0 <= abs_sector < _TOTAL_SECTORS
                and _CLASS_RANK[kind] > _CLASS_RANK[classes[abs_sector]]
            ):
                classes[abs_sector] = kind

        owned: set[int] = set()  # LV daddrs the walk accounts for

        def own(lv_daddr: int, kind: str) -> None:
            owned.add(lv_daddr)
            claim(lv_daddr + lv_base, kind)

        claim(0, "system")  # PV label
        claim(lv_base, "system")  # LV label (LV daddr 0, outside the BAT)
        if cat.pv.alt_lv_daddr:
            own(cat.pv.alt_lv_daddr - lv_base, "system")  # alternate LV label
        lv = cat.lv
        n_bat_blocks = max(0, -(-lv.bat_blocks_covered // _BITS_PER_BAT_BLOCK))
        for i in range(n_bat_blocks):
            own(lv.bat_daddr + i, "system")
        for daddr in cat.vtoc.block_bucket:
            own(daddr, "system")
        for vtoce in cat.vtoc.entries.values():
            pages, index = object_daddrs(data, lv_base, vtoce)
            if vtoce.kind == KIND_ACL:
                kind = "system"
            elif vtoce.kind_is_dir:
                kind = "directory"
            else:
                kind = "file"
            for daddr in pages:
                if daddr:  # 0 = sparse zero page, no block allocated
                    own(daddr, kind)
            for daddr in index:
                own(daddr, "system")

        mismatches = 0
        free_bits = 0
        try:
            bits = self._bat_bits()
        except (ValueError, IndexError) as exc:
            self.logger.warning(f"BAT bitmap unreadable: {exc}")
            bits = []
        for i, free in enumerate(bits):
            daddr = lv.bat_first_covered + i
            if free:
                free_bits += 1
            if free == (daddr in owned):
                mismatches += 1
            if not free and daddr not in owned:
                claim(daddr + lv_base, "damaged")  # allocated but unaccounted

        self._account_cache = (classes, mismatches, free_bits)
        return self._account_cache

    # ------------------------------------------------------------------
    # Allocation / disk map
    # ------------------------------------------------------------------

    @property
    def allocation_unit_size(self) -> int:
        """Bytes per allocation unit (one 1024-byte block).

        The GUI space panel divides byte counts by this attribute
        (``disk_manager.update_space_info``); a filesystem without it makes
        the panel reset to zero and the disk map show everything free.
        """
        return BLOCK

    def get_free_space(self) -> tuple[int, int]:
        """Read-only: 0 bytes free; total = medium capacity.

        Decision (mirrors the wbak precedent, test_51): the GUI renders
        this pair as the disk capacity, and a read-only volume offers 0
        importable bytes.  The BAT's free-block count -- a volume
        statistic, not importable capacity -- is reported by
        :meth:`get_display_info` under "Free Blocks (BAT)".
        """
        self._initialize()
        return 0, IMAGE_SIZE

    def get_allocated_units(self) -> list[int]:
        """Absolute sectors of every block the volume accounts as in-use:
        labels, BAT, VTOC, index blocks, directory and file pages, plus
        BAT-allocated-but-unaccounted (damaged) blocks."""
        self._initialize()
        classes, _mismatches, _free = self._account()
        return [lba for lba, kind in enumerate(classes) if kind != "free"]

    def get_file_allocation_units(self, path: str) -> list[int]:
        """Absolute sectors of a file's data pages (sparse pages and the
        L1/L2/L3 index blocks excluded; index blocks show as system in the
        disk map).  Empty for directories and missing paths."""
        self._initialize()
        try:
            node = self._catalog.lookup(path)
        except FileNotFoundError:
            return []
        if node.is_dir or node.vtoce is None:
            return []
        lv_base = self._catalog.lv_base
        return sorted({d + lv_base for d in node.page_daddrs if d})

    def get_disk_map_layout(self) -> dict[str, Any]:
        self._initialize()
        classes, _mismatches, _free = self._account()

        def get_sector_type(lba: int) -> str:
            if 0 <= lba < _TOTAL_SECTORS:
                return classes[lba]
            return "free"

        colors = {
            "system": "#888888",
            "directory": "#4444CC",
            "file": "#44AA44",
            "free": "#DDDDDD",
            "damaged": "#CC4444",
        }
        return {
            "legend": [
                ("Labels/VTOC/BAT", colors["system"]),
                ("Directories", colors["directory"]),
                ("File Data", colors["file"]),
                ("Free (BAT)", colors["free"]),
                ("Damaged/Unaccounted", colors["damaged"]),
            ],
            "get_sector_type": get_sector_type,
            "allocation_unit_size_sectors": 1,
            "first_data_sector": 0,
            "type_color_map": colors,
        }

    # ------------------------------------------------------------------
    # Info / validity
    # ------------------------------------------------------------------

    @staticmethod
    def _format_time(value: Optional[datetime]) -> str:
        return value.strftime("%Y-%m-%d %H:%M:%S UTC") if value else ""

    def get_display_info(self) -> dict[str, str]:
        self._initialize()
        cat = self._catalog
        info = {
            "Filesystem": "Apollo AEGIS (read-only)",
            "Volume": cat.lv.name,
            "Node": cat.node_entry_name or "",
            "PV UID": cat.pv.uid_text,
            "LV UID": cat.lv.uid_text,
            "Label Written": self._format_time(cat.lv.label_written),
            "Mounted": self._format_time(cat.lv.mounted),
            "Dismounted": self._format_time(cat.lv.dismounted),
            "Files": str(cat.counts.get("files", 0)),
            "Directories": str(cat.counts.get("dirs", 0)),
            "ACL Objects": str(cat.counts.get("acl", 0)),
            "Unreferenced Objects": str(cat.unreferenced),
            "VTOC": (f"{cat.vtoc.bucket_count} buckets, {cat.vtoc.block_count} blocks"),
            "Free Blocks (BAT)": str(cat.lv.bat_free_count),
        }
        if cat.warnings:
            # Only on damaged volumes: tells "broken disk" apart from
            # "empty disk" when the structure degraded past detection.
            info["Volume Damage"] = self._damage_summary()
        info["Read-Only"] = "yes"
        return info

    def get_specific_config(self) -> Optional[AegisConfig]:
        self._initialize()
        return AegisConfig(
            volume_name=self._catalog.lv.name, lv_uid=self._catalog.lv.uid_text
        )

    def get_volume_label(self) -> Optional[str]:
        self._initialize()
        return self._catalog.lv.name

    def _lv_label_sane(self, lv) -> bool:
        """Plausibility gate for the +25 LV-label validity points.

        A real INVOL-written label has a printable name and structure
        totals that fit a 1232-block floppy; random garbage behind a
        stray magic fails at least one of these.
        """
        if not all(" " <= ch <= "~" for ch in lv.name):
            return False
        return (
            0 < lv.bat_blocks_covered < _TOTAL_SECTORS
            and lv.bat_free_count <= lv.bat_blocks_covered
            and 0 < lv.bat_daddr < _TOTAL_SECTORS
            and lv.vtoc_bucket_count > 0
            and 0 < lv.vtoc_total_blocks < _TOTAL_SECTORS
        )

    def get_validity_score(self) -> int:
        """Spec section 4 matrix: +25 PV magic at offset 2; +25 sane LV
        label; +50 VTOC parses, the root resolves and its first block
        carries the standard directory header.  Never raises.

        wbak media (magic at offset 0) score 0 here; bare APOLLO containers
        with garbage stop at 25, below this filesystem's own
        validity_threshold (40).  SR10+ volumes (LV label version
        != 0; different VTOCE layout, apollofs ``logical_volume.go``) are
        capped at 25 with a warning -- never claimed (spec section 6).
        """
        score = 0
        try:
            if not self.disk or not self.disk.physical_format:
                return 0
            if not self._geometry_is_apollo():
                return 0
            if self.disk.read_sector(0, 0, 0)[2:8] != PV_MAGIC:
                return 0
            score += 25
            data = self._read_image()
            pv = parse_pv_label(data)
            lv = parse_lv_label(data, pv.lv_daddr)
            if lv.version != 0:
                self.logger.warning(
                    f"AEGIS LV label version {lv.version}: SR10-or-later "
                    "volume layout (different VTOCEs); not claimed"
                )
                return min(score, 25)
            if self._lv_label_sane(lv):
                score += 25
            # The parser degrades gracefully on hostile input (capped
            # warnings, no unbounded loops), so a full parse during scoring
            # is safe; the catalog is cached so detection->open is one parse.
            self._initialize()
            root_pages = self._catalog.root.page_daddrs
            if root_pages and root_pages[0]:
                start = (root_pages[0] + self._catalog.lv_base) * BLOCK
                if struct.unpack_from(">5H", data, start) == DIR_EXPECTED_HEAD:
                    score += 50
        except Exception as exc:
            self.logger.debug(f"Validity scoring stopped early: {exc}")
        return min(score, 100)

    def check(self) -> bool:
        """BAT reconciliation (spec section 4, rule pinned in test_54).

        Recomputes block ownership from the full walk and compares it with
        the BAT bitmap.  0 mismatches expected on clean volumes (disk5
        reconciles perfectly); any mismatch is logged; more than 1% of the
        covered blocks mismatching returns False; structural failure
        (unparseable labels/VTOC/root) returns False.
        """
        try:
            self._initialize()
            classes, mismatches, free_bits = self._account()
        except Exception as exc:
            self.logger.warning(f"check(): catalog parse failed: {exc}")
            return False
        cat = self._catalog
        covered = cat.lv.bat_blocks_covered
        self.logger.info(
            f"check(): {cat.counts.get('files', 0)} files, "
            f"{cat.counts.get('dirs', 0)} dirs, {cat.counts.get('acl', 0)} ACL "
            f"objects, {cat.unreferenced} unreferenced; BAT reconciliation: "
            f"{mismatches} mismatches in {covered} covered blocks"
        )
        if free_bits != cat.lv.bat_free_count:
            self.logger.warning(
                f"check(): BAT free count {cat.lv.bat_free_count} contradicts "
                f"the bitmap ({free_bits} free bits)"
            )
        if mismatches:
            self.logger.warning(
                f"check(): {mismatches} mismatches between the ownership "
                f"walk and the BAT bitmap ({covered} blocks covered)"
            )
        return covered > 0 and mismatches <= covered * _MISMATCH_TOLERANCE

    # ------------------------------------------------------------------
    # Plugin contract
    # ------------------------------------------------------------------

    @classmethod
    def get_format_definitions(cls) -> dict[str, FormatProfile]:
        # Only this plugin's slice: the wbak filesystem registers the wbak
        # profile, so each shows up in the registry exactly once.
        from .formats.apollo_formats import APOLLO_AEGIS_FORMATS

        return APOLLO_AEGIS_FORMATS

    @staticmethod
    def configs_match(config1: Any, config2: Any) -> bool:
        """Type match plus LV-UID equality, with a wildcard sentinel.

        A ``None`` lv_uid (the shipped profile) matches any parsed volume:
        LV UIDs are per-disk, not per-format.
        """
        if not (isinstance(config1, AegisConfig) and isinstance(config2, AegisConfig)):
            return False
        if config1.lv_uid is None or config2.lv_uid is None:
            return True
        return config1.lv_uid == config2.lv_uid

    @staticmethod
    def create_config_from_params(_format_info, _physical_format) -> None:
        """Formatting/creation is unsupported: no config from parameters."""
        return None

    def name_hint(self) -> str:
        return "read-only volume"
