"""Read-only Apollo DOMAIN wbak backup filesystem.

Serves the backup trees parsed by :mod:`fatfloppy.core.apollo_wbak` as a
directory hierarchy (spec section 4):

- Each HDR1 tree mounts at its decoded ``file_id`` path (tree ``SYS5/ETC``
  -> entries under ``sys5/etc/...``); the root listing merges the first
  path components of all trees on the disk.
- ``tree.entries`` is an ORDERED sequence and duplicate paths exist on
  real media (disk8's INSTALL carries ``com/srf`` twice: first clean,
  second damaged).  Path lookup is FIRST match, so the clean copy wins;
  no dict may let a later entry shadow an earlier one.
- Entries whose NAME record was destroyed keep the catalog placeholder
  ``?``; the filesystem uniquifies colliding placeholders as ``?``,
  ``?~1``, ``?~2`` ... in entry order.  A file legitimately named ``?``
  is indistinguishable from a placeholder (the marker IS the literal name
  byte) and would be uniquified the same way -- harmless, since ``?`` is
  a shell wildcard on AEGIS and never appears as a real name.  Entries
  whose clipped NAME the parser recovered via UID overlap
  (``WbakEntry.name_recovered``, surfaced as ``extra_data
  ["name_recovered"]``) are uniquified the same way against earlier
  paths, so a recovered name colliding with a clean copy lists as
  ``name~1`` and both stay reachable.
- ``FileInfo.datetime`` carries the genuine Apollo mtime as a NAIVE UTC
  datetime (``tzinfo`` stripped) -- the shipped filesystems all use naive
  datetimes (FAT12 stores naive local time); entries without an mtime
  (links, synthesized mount directories) fall back to the backup-set
  creation time, then the Apollo epoch 1980-01-01.
- ``FileInfo.attributes`` is a space-joined marker string: ``LINK`` for
  symbolic links, ``PARTIAL`` for the file cut by end-of-volume, ``DMG``
  for parser-flagged damage; clean files/directories get ``""``.
- Cross-volume reassembly: :meth:`ApolloWbakFilesystem.attach_volume`
  stitches the next volume of a split backup set into the mounted
  catalog (``PARTIAL`` clears once the cut file's declared size is
  satisfied).  Attached volumes supply CONTENT only -- the disk map,
  allocation units, free space and ``check()`` keep describing the
  primary medium -- and attachments last for the filesystem instance's
  lifetime (closing the disk discards them).
- All mutating operations raise ``OSError("Apollo wbak volumes are
  read-only")``.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar, Optional

from ..apollo_wbak import (
    APOLLO_MAGIC,
    CYLINDERS,
    HEADS,
    IMAGE_SIZE,
    SECTOR,
    SECTORS_PER_TRACK,
    ContinuationSpec,
    WbakCatalog,
    WbakEntry,
    WbakTree,
    build_catalog,
    decode_wbak_name,
    expected_continuation,
    stitch_tree,
)
from ..format_profile import FormatProfile
from .fs_base import FileInfo, Filesystem

_READ_ONLY_MSG = "Apollo wbak volumes are read-only"
_APOLLO_EPOCH = datetime(1980, 1, 1)
_TOTAL_SECTORS = CYLINDERS * HEADS * SECTORS_PER_TRACK

# Sector classification precedence for the disk map: a sector holding both
# ANSI labels and block data shows as "directory" (labels are the rarer,
# more interesting content); damage markers outrank data so problems stay
# visible; the PV-label sectors are always "system".
_CLASS_RANK = {"free": 0, "file": 1, "directory": 2, "damaged": 3, "system": 4}


@dataclass
class ApolloWbakConfig:
    """Detected-volume descriptor (read-only; nothing is configurable).

    The shipped format profile carries the ``volume_id=None`` sentinel:
    volume IDs are per-disk, not per-format, so the sentinel matches any
    parsed volume in :meth:`ApolloWbakFilesystem.configs_match`.
    """

    volume_id: Optional[str] = None
    set_id: str = "BACKUP"


@dataclass
class AttachResult:
    """Outcome of a successful :meth:`ApolloWbakFilesystem.attach_volume`.

    ``stitched_tree_ids`` names the trees (HDR1 file_ids) the attached
    volume continued; ``still_incomplete`` lists the continuation specs
    still pending afterwards -- non-empty for 3+ volume chains, where the
    caller should prompt for the next volume.
    """

    volume_id: Optional[str]
    stitched_tree_ids: tuple
    still_incomplete: list  # list[ContinuationSpec]


class ApolloWbakFilesystem(Filesystem):
    """Read-only filesystem over an Apollo wbak backup catalog."""

    filesystem_type: ClassVar[str] = "APOLLO_WBAK"
    filesystem_aliases: ClassVar[list[str]] = ["APOLLO", "WBAK"]
    validity_threshold: ClassVar[int] = 40
    config_class: ClassVar[Optional[type]] = ApolloWbakConfig
    # Capability flag the GUI probes (via getattr) before offering the
    # insert-next-volume prompt; absent/False on every other filesystem.
    supports_volume_attach: ClassVar[bool] = True

    def __init__(self, disk, config: Optional[Any] = None):
        super().__init__(disk, config)
        self._catalog: Optional[WbakCatalog] = None
        # Ordered (full_path, tree, entry) triples; FIRST match wins on
        # duplicate paths, "?" placeholders pre-uniquified.
        self._index: list[tuple[str, WbakTree, WbakEntry]] = []
        self._recoverable_cache: Optional[int] = None
        self._initialized = False
        # One (volume_id, {(tree uid, section), ...}) record per attached
        # volume: the duplicate-attach guard and the display-info list.
        self._attached_volumes: list[tuple[Optional[str], frozenset]] = []

    # ------------------------------------------------------------------
    # Initialization / catalog adaptation
    # ------------------------------------------------------------------

    def _initialize(self) -> None:
        if self._initialized:
            return
        if not self.disk or not self.disk.physical_format:
            raise ValueError("No disk or geometry available")
        if not self._geometry_is_apollo():
            raise ValueError("Disk geometry is not the Apollo 77x2x8x1024 layout")
        chunks = []
        for cylinder in range(CYLINDERS):
            for head in range(HEADS):
                for sector in range(SECTORS_PER_TRACK):
                    chunks.append(self.disk.read_sector(cylinder, head, sector))
        self._catalog = build_catalog(b"".join(chunks))
        self._build_index()
        self._initialized = True

    def _geometry_is_apollo(self) -> bool:
        pf = self.disk.physical_format
        try:
            return (
                pf.cylinders == CYLINDERS
                and pf.heads == HEADS
                and pf.bytes_per_sector == SECTOR
                and pf.get_sectors_per_track(0, 0) == SECTORS_PER_TRACK
            )
        except (ValueError, AttributeError):
            return False

    def _build_index(self) -> None:
        """Adapt the catalog into the ordered full-path index.

        Colliding "?" placeholders (NAME record destroyed) and recovered
        names (clipped NAME proven via UID overlap, ``name_recovered``)
        are uniquified deterministically in entry order against every
        path already indexed -- so a recovered name colliding with its
        clean earlier copy lists as ``name~1`` and both stay reachable.
        (If a recovered entry ever preceded its clean twin in entry
        order, neither is renamed and the pair degrades to the genuine-
        duplicate policy below.)  Genuine duplicate paths are kept as-is
        (FIRST match wins on lookup)."""
        self._index = []
        taken: set[str] = set()
        for tree in self._catalog.trees:
            mount = decode_wbak_name(tree.file_id.encode("ascii", "replace"))
            for entry in tree.entries:
                if entry.path == "":
                    full = mount  # the tree root directory itself
                elif mount:
                    full = f"{mount}/{entry.path}"
                else:
                    full = entry.path
                if entry.raw_name == b"?" or entry.name_recovered:
                    candidate, counter = full, 0
                    while candidate.lower() in taken:
                        counter += 1
                        candidate = f"{full}~{counter}"
                    full = candidate
                taken.add(full.lower())
                self._index.append((full, tree, entry))

    # ------------------------------------------------------------------
    # Path resolution
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize(path: str) -> str:
        return "/".join(part for part in path.strip("/").split("/") if part)

    def _lookup(self, norm: str) -> Optional[tuple[str, WbakTree, WbakEntry]]:
        """FIRST index item whose full path equals norm (case-insensitive)."""
        key = norm.lower()
        for item in self._index:
            if item[0].lower() == key:
                return item
        return None

    def _has_children(self, norm: str) -> bool:
        prefix = norm.lower() + "/"
        return any(full.lower().startswith(prefix) for full, _t, _e in self._index)

    # ------------------------------------------------------------------
    # Listing / reading
    # ------------------------------------------------------------------

    def list_directory(self, path: str) -> list[FileInfo]:
        self._initialize()
        norm = self._normalize(path)
        hit = self._lookup(norm) if norm else None
        if hit is not None and not hit[2].is_dir:
            raise NotADirectoryError(f"Not a directory: {path}")
        prefix_len = len(norm) + 1 if norm else 0
        prefix = norm.lower() + "/" if norm else ""
        children: dict[str, FileInfo] = {}
        synthetic: set[str] = set()
        found = not norm or hit is not None
        for full, tree, entry in self._index:
            if prefix and not full.lower().startswith(prefix):
                continue
            rest = full[prefix_len:]
            if not rest:
                continue  # the directory entry itself, not a child
            found = True
            child = rest.split("/", 1)[0]
            key = child.lower()
            if "/" not in rest:
                # direct child: first real entry wins, upgrading a
                # previously synthesized intermediate in place
                if key not in children or key in synthetic:
                    children[key] = self._entry_to_fileinfo(child, tree, entry)
                    synthetic.discard(key)
            elif key not in children:
                # intermediate component with no entry (yet): synthesize
                children[key] = FileInfo(
                    name=child,
                    size=0,
                    is_dir=True,
                    datetime=self._fallback_datetime(),
                    attributes="",
                )
                synthetic.add(key)
        if not found:
            raise FileNotFoundError(f"No such directory: {path}")
        return list(children.values())

    def read_file(self, path: str) -> bytes:
        self._initialize()
        norm = self._normalize(path)
        hit = self._lookup(norm) if norm else None
        if hit is None:
            if not norm or self._has_children(norm):
                raise IsADirectoryError(f"Path is a directory: {path}")
            raise FileNotFoundError(f"File not found: {path}")
        _full, _tree, entry = hit
        if entry.is_dir:
            raise IsADirectoryError(f"Path is a directory: {path}")
        if entry.partial:
            self.logger.warning(
                f"{path}: cut by end of volume; returning the available prefix"
            )
        if entry.damaged:
            self.logger.warning(
                f"{path}: damaged on the medium ({len(entry.damage_notes)} "
                "annotations); holes are zero-filled"
            )
        return self._catalog.read(entry)

    def _entry_to_fileinfo(self, name: str, tree: WbakTree, entry: WbakEntry):
        markers = []
        if entry.link_target is not None:
            markers.append("LINK")
        if entry.partial:
            markers.append("PARTIAL")
        if entry.damaged:
            markers.append("DMG")
        if entry.mtime is not None:
            dt = entry.mtime.replace(tzinfo=None)  # naive UTC, documented
        else:
            dt = self._fallback_datetime()
        return FileInfo(
            name=name,
            size=0 if entry.is_dir else entry.size,
            is_dir=entry.is_dir,
            datetime=dt,
            attributes=" ".join(markers),
            starting_cluster=entry.extents[0][0] // SECTOR if entry.extents else 0,
            extra_data={
                "raw_name": entry.raw_name,
                "name_recovered": entry.name_recovered,
                "link_target": entry.link_target,
                "damage_notes": list(entry.damage_notes),
                "partial": entry.partial,
                "atime": entry.atime,
                # Tree identity for the GUI's insert-next-volume matching:
                # (tree_id, sequence) name the tree within the backup set
                # and section is its CURRENT section (advances on stitch),
                # so a pending ContinuationSpec matches iff its file_id,
                # sequence and next_section == section + 1 all line up.
                "tree_id": tree.file_id,
                "sequence": tree.sequence,
                "section": tree.section,
            },
        )

    def _fallback_datetime(self) -> datetime:
        created = self._catalog.created if self._catalog else None
        return created.replace(tzinfo=None) if created else _APOLLO_EPOCH

    # ------------------------------------------------------------------
    # Cross-volume reassembly
    # ------------------------------------------------------------------

    def pending_continuations(self) -> list[ContinuationSpec]:
        """Continuation specs for every incomplete (EOV-cut) tree, in
        tree order; empty when every tree ends with an EOF trailer."""
        self._initialize()
        return [
            expected_continuation(tree, self._catalog)
            for tree in self._catalog.trees
            if not tree.complete
        ]

    def attach_volume(self, data: bytes) -> AttachResult:
        """Stitch a continuation volume's image into the mounted catalog.

        Parses ``data`` as a wbak volume and merges every tree section
        that continues one of this catalog's incomplete trees (matched on
        file_id + sequence + section; the per-tree UHL1 uid -- the
        cross-volume invariant -- is validated by :func:`stitch_tree`).
        Validation is all-or-nothing: every failure raises
        :class:`ValueError` BEFORE any state changes (candidates are
        stitched into a local map and swapped in only after the whole
        volume validates), so a rejected attach leaves the filesystem
        exactly as it was.

        The attached volume supplies CONTENT only: the medium-level views
        (disk map, allocation units, free space, ``check()``) keep
        describing the primary volume, and foreign trees on the attached
        volume (other sequences of the set) are not merged.
        """
        self._initialize()
        catalog = build_catalog(data)
        if catalog.volume_id is None or not catalog.trees:
            raise ValueError(
                "Attached image is not a wbak backup volume "
                "(no ANSI volume/header labels found)"
            )
        keys = frozenset((tree.uid_text, tree.section) for tree in catalog.trees)
        for attached_id, attached_keys in self._attached_volumes:
            if catalog.volume_id == attached_id and keys & attached_keys:
                raise ValueError(f"Volume {catalog.volume_id!r} is already attached")
        pending = [tree for tree in self._catalog.trees if not tree.complete]
        if not pending:
            raise ValueError(
                "Every tree on this volume is complete; nothing for "
                f"volume {catalog.volume_id!r} to continue"
            )
        stitched: dict[int, WbakTree] = {}
        stitched_ids: list[str] = []
        for index, prev in enumerate(self._catalog.trees):
            if prev.complete:
                continue
            cont = next(
                (
                    tree
                    for tree in catalog.trees
                    if tree.file_id == prev.file_id
                    and tree.sequence == prev.sequence
                    and tree.section == prev.section + 1
                ),
                None,
            )
            if cont is None:
                continue
            stitched[index] = stitch_tree(
                prev, cont, prev_uid=prev.uid_text, cont_uid=cont.uid_text
            )
            stitched_ids.append(prev.file_id)
        if not stitched:
            expected = "; ".join(
                f"section {spec.next_section} of {spec.file_id!r} "
                f"seq {spec.sequence} (uid {spec.backup_uid})"
                for spec in (
                    expected_continuation(tree, self._catalog) for tree in pending
                )
            )
            found = "; ".join(
                f"{tree.file_id!r} seq {tree.sequence} section {tree.section} "
                f"(uid {tree.uid_text})"
                for tree in catalog.trees
            )
            raise ValueError(
                f"Wrong volume {catalog.volume_id!r}: expected {expected}; "
                f"the volume contains {found}"
            )
        # every validation passed: swap the stitched trees in
        self._catalog.trees = [
            stitched.get(index, tree) for index, tree in enumerate(self._catalog.trees)
        ]
        self._build_index()
        self._recoverable_cache = None
        self._attached_volumes.append((catalog.volume_id, keys))
        return AttachResult(
            volume_id=catalog.volume_id,
            stitched_tree_ids=tuple(stitched_ids),
            still_incomplete=self.pending_continuations(),
        )

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
    # Allocation / disk map
    # ------------------------------------------------------------------

    @property
    def allocation_unit_size(self) -> int:
        """Bytes per allocation unit (one 1024-byte sector).

        The GUI space panel divides byte counts by this attribute
        (``disk_manager.update_space_info``); a filesystem without it makes
        the panel reset to zero and the disk map show everything free.
        """
        return SECTOR

    def get_free_space(self) -> tuple[int, int]:
        """Read-only: 0 bytes free; total = medium capacity.

        The shipped filesystems report (free, total) of the data area
        (FAT12 returns ``num_clusters * cluster_size``); for a
        tape-on-floppy the data area is the whole 1,261,568-byte image.
        The recoverable-content figure -- a *content* statistic, not a
        capacity -- is reported by :meth:`get_display_info` instead, so
        the GUI space panel no longer renders content bytes as a
        misleading "131.1 KB" disk size."""
        self._initialize()
        return 0, IMAGE_SIZE

    def _recoverable_bytes(self) -> int:
        """Total bytes :meth:`read_file` would yield across all trees
        (cached: computing it reads every file's extents).

        Declared FILE-header sizes are deliberately NOT summed: destroyed
        headers declare junk (disk8 carries an entry claiming 0x20202000
        -- four ASCII spaces -- bytes, 514 MB on a 1.2 MB floppy)."""
        if self._recoverable_cache is None:
            self._recoverable_cache = sum(
                len(self._catalog.read(entry))
                for tree in self._catalog.trees
                for entry in tree.entries
                if not entry.is_dir
            )
        return self._recoverable_cache

    def get_allocated_units(self) -> list[int]:
        """Sectors touched by the stream's records (labels + data blocks).

        Stale/gap sectors carry no stream content and the PV-label sectors
        are not part of the stream; neither counts as allocated."""
        self._initialize()
        allocated: set[int] = set()
        for event in self._catalog.frame_events:
            if event.kind != "record":
                continue
            for _payload_off, image_off, length in event.spans:
                allocated.update(self._span_sectors(image_off, length))
        return sorted(allocated)

    def get_file_allocation_units(self, path: str) -> list[int]:
        self._initialize()
        hit = self._lookup(self._normalize(path))
        if hit is None or hit[2].is_dir:
            return []
        sectors: set[int] = set()
        for image_off, length in hit[2].extents:
            sectors.update(self._span_sectors(image_off, length))
        return sorted(sectors)

    @staticmethod
    def _span_sectors(image_off: int, length: int) -> range:
        if length <= 0:
            return range(0)
        return range(image_off // SECTOR, (image_off + length - 1) // SECTOR + 1)

    def get_disk_map_layout(self) -> dict[str, Any]:
        self._initialize()
        classes = ["free"] * _TOTAL_SECTORS
        classes[0] = classes[1] = "system"  # PV label + reserved sector 1

        def claim(sector: int, kind: str) -> None:
            if (
                0 <= sector < _TOTAL_SECTORS
                and _CLASS_RANK[kind] > _CLASS_RANK[classes[sector]]
            ):
                classes[sector] = kind

        for event in self._catalog.frame_events:
            if event.kind == "record":
                kind = "directory" if event.category == "label" else "file"
                for _payload_off, image_off, length in event.spans:
                    for sector in self._span_sectors(image_off, length):
                        claim(sector, kind)
            elif event.kind in ("stale", "gapsec", "gap", "badrec"):
                claim(event.offset // SECTOR, "damaged")
            # filler/tapemark sectors stay "free" unless records share them

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
                ("PV Label", colors["system"]),
                ("ANSI Labels", colors["directory"]),
                ("Backup Data", colors["file"]),
                ("Free/Tail", colors["free"]),
                ("Damaged/Stale", colors["damaged"]),
            ],
            "get_sector_type": get_sector_type,
            "allocation_unit_size_sectors": 1,
            "first_data_sector": 0,
            "type_color_map": colors,
        }

    # ------------------------------------------------------------------
    # Info / validity
    # ------------------------------------------------------------------

    def get_display_info(self) -> dict[str, str]:
        self._initialize()
        cat = self._catalog
        files = [
            entry
            for tree in cat.trees
            for entry in tree.entries
            if not entry.is_dir and entry.link_target is None
        ]
        tree_lines = []
        for tree in cat.trees:
            count = sum(
                1
                for entry in tree.entries
                if not entry.is_dir and entry.link_target is None
            )
            line = (
                f"{tree.file_id} seq {tree.sequence} section {tree.section}, "
                f"{'EOF' if tree.complete else 'EOV'}, files {count}"
            )
            if tree.continued_from_previous:
                line += " (continues from a previous volume)"
            tree_lines.append(line)
        created = cat.created.strftime("%Y-%m-%d %H:%M:%S UTC") if cat.created else ""
        info = {
            "Filesystem": "Apollo wbak (read-only)",
            "Volume ID": cat.volume_id or "",
            "Owner": cat.owner or "",
            "Created": created,
            "Backup UID": cat.backup_uid or "",
            "Trees": str(len(cat.trees)),
            "Backup Sets": "; ".join(tree_lines),
            "Files": str(len(files)),
            "Recoverable Content": f"{self._recoverable_bytes():,} bytes",
            "Damaged Files": str(sum(1 for entry in files if entry.damaged)),
            "Partial Files": str(sum(1 for entry in files if entry.partial)),
            "Read-Only": "yes",
        }
        if self._attached_volumes:
            info["Attached Volumes"] = ", ".join(
                volume_id or "?" for volume_id, _keys in self._attached_volumes
            )
        return info

    def get_specific_config(self) -> Optional[ApolloWbakConfig]:
        self._initialize()
        return ApolloWbakConfig(volume_id=self._catalog.volume_id)

    def get_volume_label(self) -> Optional[str]:
        self._initialize()
        return self._catalog.volume_id

    def get_validity_score(self) -> int:
        # Never raise: the registry probes every filesystem on every disk.
        score = 0
        try:
            if not self.disk or not self.disk.physical_format:
                return 0
            if not self._geometry_is_apollo():
                return 0
            if self.disk.read_sector(0, 0, 0)[: len(APOLLO_MAGIC)] == APOLLO_MAGIC:
                # +25, deliberately below this filesystem's own
                # validity_threshold (40), the claim floor: a bare APOLLO
                # container without a wbak tape stream (AEGIS-native disks
                # like disk5) must never be claimed as a wbak filesystem.
                score += 25
            # The parser is proven non-hanging on hostile input
            # (adversarial-probed), so a full parse during scoring is safe;
            # the catalog is cached so detection->open doesn't parse twice.
            self._initialize()
            if self._catalog.volume_id:
                score += 40
            if self._catalog.trees:  # at least one HDR1-parsed tree section
                score += 30
        except Exception as exc:
            self.logger.debug(f"Validity scoring stopped early: {exc}")
        return min(score, 100)

    def check(self) -> bool:
        """True iff the stream parsed to an EOT with consistent labels
        (VOL1 present, at least one HDR1 tree).  Logs a damage census."""
        try:
            self._initialize()
        except Exception as exc:
            self.logger.warning(f"check(): catalog parse failed: {exc}")
            return False
        cat = self._catalog
        files = [
            entry
            for tree in cat.trees
            for entry in tree.entries
            if not entry.is_dir and entry.link_target is None
        ]
        anomalies = sum(
            1
            for event in cat.frame_events
            if event.kind in ("stale", "gapsec", "gap", "badrec")
        )
        self.logger.info(
            f"check(): {len(cat.trees)} trees, {len(files)} files, "
            f"{sum(1 for e in files if e.damaged)} damaged, "
            f"{sum(1 for e in files if e.partial)} partial, "
            f"{anomalies} stale/gap events"
        )
        if not cat.eot_found:
            self.logger.warning("check(): stream has no EOT")
            return False
        if cat.volume_id is None or not cat.trees:
            self.logger.warning("check(): missing VOL1/HDR1 labels")
            return False
        return True

    # ------------------------------------------------------------------
    # Plugin contract
    # ------------------------------------------------------------------

    @classmethod
    def get_format_definitions(cls) -> dict[str, FormatProfile]:
        # Only this plugin's slice: the AEGIS filesystem registers the
        # AEGIS profile, so each shows up in the registry exactly once.
        from .formats.apollo_formats import APOLLO_WBAK_FORMATS

        return APOLLO_WBAK_FORMATS

    @staticmethod
    def configs_match(config1: Any, config2: Any) -> bool:
        """Type match plus volume-id and set-id equality.

        ``set_id`` must be ``"BACKUP"`` on both sides (documents that only
        wbak backup tapes are supported; a sentinel ``None`` volume_id
        matches any parsed volume because volume IDs are per-disk, not
        per-format).
        """
        if not (
            isinstance(config1, ApolloWbakConfig)
            and isinstance(config2, ApolloWbakConfig)
        ):
            return False
        if config1.set_id != config2.set_id:
            return False
        if config1.volume_id is None or config2.volume_id is None:
            return True
        return config1.volume_id == config2.volume_id

    @staticmethod
    def create_config_from_params(_format_info, _physical_format) -> None:
        """Formatting/creation is unsupported: no config from parameters."""
        return None

    def name_hint(self) -> str:
        return "read-only volume"
