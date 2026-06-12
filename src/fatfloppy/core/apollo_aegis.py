"""Apollo AEGIS native-volume parser: labels, VTOC, directories, catalog.

Parses the on-disk structures of AEGIS-native (SR9) Apollo floppies --
77x2x8x1024 (1,261,568-byte) images such as bootable utility disks -- from
the PV/LV labels through the VTOC/VTOCEs and directories up to the fully
walked :class:`AegisCatalog` (:func:`build_catalog`).  The normative format
description lives in
``docs/superpowers/specs/2026-06-11-aegis-fs-design.md`` section 2; primary
sources are the *Domain Engineering Handbook Rev 4* (``eng_handbook_rev4.pdf``,
exact Pascal record layouts) and *AEGIS Internals and Data Structures*
(``aegis_internals.pdf``, ch. 8 for directories), cross-checked against the
empirical dissection of disk5 in
``docs/superpowers/research/apollo/aegis_empirical/``.

On-disk layout (all multi-byte fields big-endian; blocks are 1024 bytes;
daddrs inside LV structures are LV-relative, absolute byte offset =
``(daddr + lv_base) * 1024``)::

    PV label  := block 0: +00 version, +02 magic "APOLLO", +08 name[32],
                 +28 uid[8], +34 total blocks, +38 blocks/track,
                 +3A tracks/cyl, +3C lv_list[10], +64 alt_lv_list[10]
    LV label  := block lv_base (= LV daddr 0): +00 version, +04 name[32],
                 +24 uid[8], +2C bat_hdr (0x20 bytes), +4C vtoc_hdr
                 (to +B0), +B0 label_write_time, +B4 last_mounted_node,
                 +B8 node_boot_time, +BC mounted, +C0 dismounted, ...
    vtoc_hdr  := +4C version:u16 | bucket count:u16, +50 blocks used:u32,
                 +54 net-root vtocx, +58 root-dir vtocx, +5C paging-file
                 vtocx, +60 boot-file vtocx, +64 map[8] of 6-byte extents
                 {n_blocks:u16, first daddr:u32}, +94 pad (to +B0)
    VTOC      := hash buckets headed by the blocks the map extents
                 enumerate (first ``bucket count`` of them), chained via
                 a u32 next-daddr at +0 of each block (0 = end);
                 each block holds 5 x 204-byte VTOCEs at
                 +004/+0D0/+19C/+268/+334; vtocx = (daddr << 4) | slot
    VTOCE     := +00 version:u8 kind:u8 flags:u16 (0x8000 = in use),
                 +04 object uid, +0C type uid, +14 ACL uid, +1C length,
                 +20 blocks used, +24 dtm, +28 dtu, +2C parent-dir uid,
                 +34 extdtm|ref_cnt, +38 lock_key, +3C pad,
                 +40 direct daddrs[32], +C0/+C4/+C8 L1/L2/L3 indirect

Two documented evidence conflicts were resolved against disk5 (spec
section 2; throwaway verification scripts ran at implementation time):

- **vtoc_hdr offset**: the handbook (``lv_label_t`` p.2-18, ``vtoc_hdr_t``
  p.2-23, "Offsets given are from the start of the label") places the VTOC
  header at LV label +0x4C, immediately after the 0x20-byte ``bat_hdr_t``
  (+0x2C..+0x4B, p.2-3).  The empirical script ``02_vtoc.py`` used a
  +0x40 origin with intra-header offsets shifted +0xC -- the *same
  absolute bytes* -- and misread ``bat_hdr.bat_step`` (+0x40, value 2 on
  disk5) as a header version.  disk5 block 1 confirms the handbook
  framing: +4C = version 0 | bucket count 2, +50 = 17 blocks used,
  +64 = map extent {2 blocks @ 0x266}.  Verdict: **+0x4C**.
- **bucket hash**: handbook p.2-15: "x = the four words of the UID XORed
  together; INDEX = x mod TABLE_SIZE", TABLE_SIZE = ``vtoc_hdr.vtoc_size``
  (the bucket count).  The empirical scripts used ``uid_hi mod n``, which
  merely coincides on disk5 (both verified 80/80 there at n=2).  The
  handbook formula is implemented; because the two variants are only
  provably equal on this one sample, :meth:`Vtoc.lookup_uid` ALWAYS falls
  back to an exhaustive walk on a hash miss (floppy VTOCs are tiny --
  hashing is an optimization, never a correctness dependency).
"""

import logging
import struct
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from .apollo_wbak import STORAGE_HEADER_SIZE, apollo_time_to_datetime

logger = logging.getLogger(__name__)

BLOCK = 1024
PV_MAGIC = b"APOLLO"  # at PV-label offset 2 (wbak media carries it at offset 0)

VTOCE_SIZE = 0xCC  # 204 bytes
VTOCE_SLOT_OFFSETS = (0x004, 0x0D0, 0x19C, 0x268, 0x334)
VTOCE_FLAG_IN_USE = 0x8000  # handbook p.2-22: U bit of the VTOCE header word
VTOC_MAP_ENTRIES = 8  # handbook p.2-23: vtoc_hdr.map[8]
_VTOC_CHAIN_WARN_CAP = 3  # max individual "chain broken" warnings per read_vtoc call

# VTOCE kind (sys_type byte; spec section 2 / empirical disk5 census)
KIND_FILE = 0
KIND_DIR = 1
KIND_ROOT_DIR = 2  # "root-class" directory (volume entry dir, network root)
KIND_ACL = 3


class AegisError(ValueError):
    """Raised when bytes do not parse as the expected AEGIS structure."""


def _be16(data: bytes, offset: int) -> int:
    return struct.unpack_from(">H", data, offset)[0]


def _be32(data: bytes, offset: int) -> int:
    return struct.unpack_from(">I", data, offset)[0]


def _uid_text(uid: bytes) -> str:
    """Canonical Apollo UID rendering ``HHHHHHHH.LLLLLLLL`` (spec 2)."""
    return f"{_be32(uid, 0):08X}.{_be32(uid, 4):08X}"


def _name32(raw: bytes) -> str:
    """Decode a 32-byte space-padded ASCII volume name field."""
    return raw.decode("ascii", "replace").rstrip(" \x00")


def _time_or_none(hi32: int) -> Optional[datetime]:
    """Apollo 32-bit timestamp to naive-UTC datetime; 0 means "never"."""
    if hi32 == 0:
        return None
    return apollo_time_to_datetime(hi32).replace(tzinfo=None)


@dataclass
class PvLabel:
    """Physical volume label (block 0, ``pv_label_t``)."""

    version: int
    name: str
    uid: bytes
    uid_text: str
    total_blocks: int
    blocks_per_track: int
    tracks_per_cyl: int
    lv_daddr: int  # lv_list[1]: absolute daddr of the (only) LV label
    alt_lv_daddr: int  # alt_lv_list[1]: absolute daddr of the alternate copy


@dataclass
class LvLabel:
    """Logical volume label (block ``lv_base``, ``lv_label_t``).

    BAT-header fields come from label +0x2C..+0x4B (``bat_hdr_t``), VTOC
    header fields from +0x4C..+0xAF (``vtoc_hdr_t``) -- see the module
    docstring for the resolved +0x4C-vs-+0x40 framing conflict.
    """

    name: str
    uid: bytes
    uid_text: str
    # Block Availability Table header (label +0x2C, bat_hdr_t)
    bat_blocks_covered: int  # .n_blk: number of blocks the bitmap represents
    bat_free_count: int  # .n_free
    bat_daddr: int  # .addr: LV daddr of the first BAT block
    bat_first_covered: int  # .base_add: LV daddr bit 0 of word 0 represents
    bat_step: int  # .bat_step: allocation stride (2 on disk5)
    # VTOC header (label +0x4C, vtoc_hdr_t)
    vtoc_version: int
    vtoc_bucket_count: int  # .vtoc_size: number of hash buckets
    vtoc_total_blocks: int  # .vtoc_blocks: VTOC blocks in use
    net_root_vtocx: int  # .net_x: network root ``//``
    root_dir_vtocx: int  # .root_x: volume entry directory ``/``
    os_paging_vtocx: int  # .os_x: AEGIS paging file (0 on floppies)
    sysboot_vtocx: int  # .boot_x
    vtoc_map: list[tuple[int, int]]  # nonzero extents: (n_blocks, first daddr)
    # Times (naive UTC, house convention; None = never/zero)
    label_written: Optional[datetime]
    node_boot_time: Optional[datetime]
    mounted: Optional[datetime]
    dismounted: Optional[datetime]
    last_mounted_node: int


@dataclass
class Vtoce:
    """One 204-byte VTOC entry (``vtoce_hdr_t`` + file map, handbook p.2-22)."""

    vtocx: int  # (VTOC block daddr << 4) | slot
    version: int
    kind: int  # sys_type: KIND_FILE/DIR/ROOT_DIR/ACL
    flags: int  # u16 at +2; bit 0x8000 = in use
    uid: bytes
    uid_text: str
    type_uid: bytes
    type_uid_hi: int  # canned types live in the high word (0x311 uasc, ...)
    acl_uid: bytes
    length: int  # current length in bytes (includes any storage header)
    blocks_used: int
    dtu: Optional[datetime]  # naive UTC; None = never
    dtm: Optional[datetime]
    parent_uid: bytes  # directory where the object is catalogued
    parent_uid_text: str
    ref_info: int  # raw +0x34 word (extdtm hi16 | ref_cnt lo16)
    lock_key: int  # raw +0x38 word
    direct_daddrs: list[int]  # 32 direct page daddrs (0 = sparse zero page)
    l1_daddr: int  # fm2[1]: L1 indirect block (256 daddrs)
    l2_daddr: int  # fm2[2]: L2 indirect block (256 L1 pointers)
    l3_daddr: int  # fm2[3]: L3 indirect block (256 L2 pointers)

    @property
    def kind_is_dir(self) -> bool:
        """True for ordinary and root-class directories."""
        return self.kind in (KIND_DIR, KIND_ROOT_DIR)


@dataclass
class Vtoc:
    """The fully-walked VTOC of one logical volume."""

    bucket_count: int
    block_count: int  # distinct VTOC blocks actually walked
    entries: dict[int, Vtoce]  # keyed by vtocx
    block_bucket: dict[int, int] = field(default_factory=dict)  # daddr -> bucket

    def bucket_of(self, uid: bytes) -> int:
        """Hash a UID to its bucket index (handbook p.2-15 formula).

        ``x = the four 16-bit words of the UID XORed together;
        INDEX = x mod TABLE_SIZE`` where TABLE_SIZE is
        ``vtoc_hdr.vtoc_size`` (the bucket count).  Verified to place all
        80 disk5 VTOCEs in their observed buckets.
        """
        w0, w1, w2, w3 = struct.unpack(">4H", uid)
        return (w0 ^ w1 ^ w2 ^ w3) % self.bucket_count

    def bucket_containing(self, vtocx: int) -> int:
        """Bucket index whose chain holds the block of the given vtocx."""
        return self.block_bucket[vtocx >> 4]

    def lookup_uid(self, uid: bytes) -> Optional[Vtoce]:
        """Find the VTOCE of an object UID.

        Tries the hash-directed bucket first, then ALWAYS falls back to an
        exhaustive walk of every entry: the hash formula is only provably
        correct on the one real sample, and floppy VTOCs are small enough
        that correctness must never depend on it (module docstring,
        conflict b).
        """
        want = self.bucket_of(uid)
        for vtocx, entry in self.entries.items():
            if entry.uid == uid and self.block_bucket.get(vtocx >> 4) == want:
                return entry
        for entry in self.entries.values():  # exhaustive fallback
            if entry.uid == uid:
                logger.warning(
                    "AEGIS: UID %s found outside its hash bucket %d",
                    _uid_text(uid),
                    want,
                )
                return entry
        return None


def parse_pv_label(data: bytes) -> PvLabel:
    """Parse the physical volume label in block 0 of an AEGIS image.

    Raises :class:`AegisError` unless the ``APOLLO`` magic sits at offset 2
    (wbak "floppy-as-tape" media carries the magic at offset 0 instead and
    is rejected here).
    """
    if len(data) < BLOCK:
        raise AegisError("image too small for an AEGIS PV label")
    if data[2:8] != PV_MAGIC:
        raise AegisError("no APOLLO magic at offset 2: not an AEGIS PV label")
    uid = bytes(data[0x28:0x30])
    return PvLabel(
        version=_be16(data, 0x00),
        name=_name32(data[0x08:0x28]),
        uid=uid,
        uid_text=_uid_text(uid),
        total_blocks=_be32(data, 0x34),
        blocks_per_track=_be16(data, 0x38),
        tracks_per_cyl=_be16(data, 0x3A),
        lv_daddr=_be32(data, 0x3C),
        alt_lv_daddr=_be32(data, 0x64),
    )


def parse_lv_label(data: bytes, lv_base: int) -> LvLabel:
    """Parse the logical volume label at absolute block ``lv_base``.

    ``lv_base`` is the absolute daddr of the LV label (``pv_label.lv_daddr``,
    1 on floppies); it is also LV daddr 0, the base all LV-relative daddrs
    are offset from.
    """
    start = lv_base * BLOCK
    if start < 0 or start + BLOCK > len(data):
        raise AegisError(f"LV label block {lv_base} out of range")
    label = data[start : start + BLOCK]
    uid = bytes(label[0x24:0x2C])
    vtoc_map = []
    for i in range(VTOC_MAP_ENTRIES):
        offset = 0x64 + 6 * i  # vtoc_mape: u16 n_blocks + u32 first daddr
        n_blocks = _be16(label, offset)
        daddr = _be32(label, offset + 2)
        if n_blocks or daddr:
            vtoc_map.append((n_blocks, daddr))
    return LvLabel(
        name=_name32(label[0x04:0x24]),
        uid=uid,
        uid_text=_uid_text(uid),
        bat_blocks_covered=_be32(label, 0x2C),
        bat_free_count=_be32(label, 0x30),
        bat_daddr=_be32(label, 0x34),
        bat_first_covered=_be32(label, 0x38),
        bat_step=_be16(label, 0x40),
        vtoc_version=_be16(label, 0x4C),
        vtoc_bucket_count=_be16(label, 0x4E),
        vtoc_total_blocks=_be32(label, 0x50),
        net_root_vtocx=_be32(label, 0x54),
        root_dir_vtocx=_be32(label, 0x58),
        os_paging_vtocx=_be32(label, 0x5C),
        sysboot_vtocx=_be32(label, 0x60),
        vtoc_map=vtoc_map,
        label_written=_time_or_none(_be32(label, 0xB0)),
        node_boot_time=_time_or_none(_be32(label, 0xB8)),
        mounted=_time_or_none(_be32(label, 0xBC)),
        dismounted=_time_or_none(_be32(label, 0xC0)),
        last_mounted_node=_be32(label, 0xB4),
    )


def _parse_vtoce(block: bytes, slot_offset: int, vtocx: int) -> Optional[Vtoce]:
    """Parse one 204-byte VTOCE slot; None when the in-use flag is clear."""
    entry = block[slot_offset : slot_offset + VTOCE_SIZE]
    flags = _be16(entry, 0x02)
    if not flags & VTOCE_FLAG_IN_USE:
        return None
    uid = bytes(entry[0x04:0x0C])
    parent_uid = bytes(entry[0x2C:0x34])
    return Vtoce(
        vtocx=vtocx,
        version=entry[0x00],
        kind=entry[0x01],
        flags=flags,
        uid=uid,
        uid_text=_uid_text(uid),
        type_uid=bytes(entry[0x0C:0x14]),
        type_uid_hi=_be32(entry, 0x0C),
        acl_uid=bytes(entry[0x14:0x1C]),
        length=_be32(entry, 0x1C),
        blocks_used=_be32(entry, 0x20),
        dtm=_time_or_none(_be32(entry, 0x24)),
        dtu=_time_or_none(_be32(entry, 0x28)),
        parent_uid=parent_uid,
        parent_uid_text=_uid_text(parent_uid),
        ref_info=_be32(entry, 0x34),
        lock_key=_be32(entry, 0x38),
        direct_daddrs=[_be32(entry, 0x40 + 4 * i) for i in range(32)],
        l1_daddr=_be32(entry, 0xC0),
        l2_daddr=_be32(entry, 0xC4),
        l3_daddr=_be32(entry, 0xC8),
    )


def read_vtoc(data: bytes, lv_base: int) -> Vtoc:
    """Walk the whole VTOC of the logical volume at ``lv_base``.

    The bucket heads are the first ``vtoc_bucket_count`` blocks enumerated
    by the map extents (INVOL preallocates them contiguously); each bucket
    chains further blocks through the u32 next-daddr at +0.  Corrupt chain
    pointers (out of range or already visited) end the chain with a
    warning rather than failing the whole volume.
    """
    lv = parse_lv_label(data, lv_base)
    if lv.vtoc_bucket_count <= 0:
        raise AegisError("VTOC header declares no hash buckets")
    mapped: list[int] = []
    for n_blocks, first_daddr in lv.vtoc_map:
        mapped.extend(range(first_daddr, first_daddr + n_blocks))
    if len(mapped) < lv.vtoc_bucket_count:
        raise AegisError(
            f"VTOC map enumerates {len(mapped)} blocks for "
            f"{lv.vtoc_bucket_count} buckets"
        )
    max_daddr = len(data) // BLOCK - lv_base

    entries: dict[int, Vtoce] = {}
    block_bucket: dict[int, int] = {}
    visited: set[int] = set()
    _chain_warn_count = 0
    for bucket, head in enumerate(mapped[: lv.vtoc_bucket_count]):
        daddr = head
        while daddr:
            if daddr in visited or not 0 < daddr < max_daddr:
                if _chain_warn_count < _VTOC_CHAIN_WARN_CAP:
                    logger.warning(
                        "AEGIS: VTOC bucket %d chain broken at daddr 0x%X",
                        bucket,
                        daddr,
                    )
                else:
                    logger.debug(
                        "AEGIS: VTOC bucket %d chain broken at daddr 0x%X (suppressed)",
                        bucket,
                        daddr,
                    )
                _chain_warn_count += 1
                break
            visited.add(daddr)
            block_bucket[daddr] = bucket
            start = (daddr + lv_base) * BLOCK
            block = data[start : start + BLOCK]
            for slot, slot_offset in enumerate(VTOCE_SLOT_OFFSETS):
                vtocx = (daddr << 4) | slot
                vtoce = _parse_vtoce(block, slot_offset, vtocx)
                if vtoce is not None:
                    entries[vtocx] = vtoce
            daddr = _be32(block, 0)
    if _chain_warn_count > _VTOC_CHAIN_WARN_CAP:
        logger.warning(
            "AEGIS: VTOC chain broken in %d buckets total (%d further suppressed)",
            _chain_warn_count,
            _chain_warn_count - _VTOC_CHAIN_WARN_CAP,
        )

    return Vtoc(
        bucket_count=lv.vtoc_bucket_count,
        block_count=len(visited),
        entries=entries,
        block_bucket=block_bucket,
    )


# ---------------------------------------------------------------------------
# Directories (AEGIS Internals ch. 8)
# ---------------------------------------------------------------------------

DIR_HEADER_SIZE = 0x1A
DIR_ENTRY_SIZE = 48
DIR_LINEAR_OFFSET = 0x1A
DIR_POOL_OFFSET = 0x400  # entry-block pool starts after the header page
DIR_POOL_BLOCK_SIZE = 150  # u16 next + u16 prev + u8 type + u8 used + 3 x 48
DIR_POOL_ENTRIES_PER_BLOCK = 3
# Header constants: version 1, hash prime 43, linear list size 18,
# entry-block pool size 429, entries per pool block 3 (Internals table 8-1).
DIR_EXPECTED_HEAD = (1, 43, 18, 429, 3)
DIR_ENTRY_TYPE_FREE = 0
DIR_ENTRY_TYPE_UID = 1  # "a UID" -- a normal catalogued object
DIR_ENTRY_TYPE_LINK = 3  # a link descriptor (unobserved on disk5)

# Managed-file storage header (spec section 2): types uasc/rec/hdru carry a
# 32-byte header whose length the VTOCE length includes; reads hide it.
# 0x302 (obj) files also start with the magic on disk5 but are deliberately
# NOT in the strip set -- executables keep their object stream intact.
MANAGED_TYPE_HIS = frozenset({0x311, 0x300, 0x301})
STORAGE_HEADER_MAGIC = b"\x00\x20\x00\x01"

_CATALOG_WARN_CAP = 8  # max individual warning-level log records per catalog


def _warn(warnings: list, message: str) -> None:
    """Collect a degradation message; log the first few at warning level."""
    warnings.append(message)
    if len(warnings) <= _CATALOG_WARN_CAP:
        logger.warning("AEGIS: %s", message)
    else:
        logger.debug("AEGIS: %s", message)


@dataclass
class DirEntry:
    """One 48-byte directory entry (name[32] space-padded + 6 reserved bytes
    [the network-number hint in the network root] + u8 name length + u8 entry
    type + 8 bytes of entry data -- the object UID for type 1)."""

    name: str
    entry_type: int
    uid: bytes  # object UID for type 1; raw entry data otherwise


def parse_directory(data: bytes, warnings: Optional[list] = None) -> list:
    """Decode a directory object's content into its entries.

    Layout per AEGIS Internals ch. 8, verified byte-exact against disk5
    (header 26 B + 18 x 48 B linear list + 48 B information block at +0x37A
    + 43 u16 hash threads at +0x3AA == 1024 exactly):

    - header: +0 version (1), +2 hash prime (43), +4 linear list size (18),
      +6 pool size (429), +8 entries per pool block (3), +0xA high block,
      +0xC free chain, +0xE parent UID (zero on disk5), +0x16 entry count,
      +0x18 maximum count (1300 = 0x0514);
    - the linear list holds the first 18 entries;
    - **multi-block continuation (doc-interpreted; unobserved on real
      media, pinned by the synthetic builder)**: further entries live in
      the entry-block pool -- 150-byte blocks (u16 next / u16 prev hash
      chain links, u8 block type [0 free, 1 entry array, 3 link text],
      u8 used count, then 3 x 48-byte entries), numbered from 1 and packed
      as a flat array from file offset 0x400.  Directories are mapped
      memory on AEGIS, so pool blocks crossing 1024-byte page boundaries
      is harmless.  A full listing scans pool blocks 1..high-block by
      block type; the hash threads are only a search optimization.

    Unexpected header constants are tolerated with a warning; truncated
    data yields the entries that did parse plus a warning (graceful
    degradation, spec section 4).
    """
    if warnings is None:
        warnings = []
    entries: list = []
    if len(data) < DIR_HEADER_SIZE:
        _warn(warnings, f"directory data too short ({len(data)} bytes); no entries")
        return entries
    head = struct.unpack_from(">5H", data, 0)
    if head != DIR_EXPECTED_HEAD:
        _warn(
            warnings,
            f"non-standard directory header constants {head} "
            f"(expected {DIR_EXPECTED_HEAD}); decoding anyway",
        )
    list_size = head[2] if 0 < head[2] <= DIR_EXPECTED_HEAD[2] else DIR_EXPECTED_HEAD[2]
    high_block = _be16(data, 0x0A)
    declared_count = _be16(data, 0x16)

    def decode_slot(offset: int) -> Optional[DirEntry]:
        entry_type = data[offset + 0x27]
        if entry_type == DIR_ENTRY_TYPE_FREE:
            return None
        name_length = data[offset + 0x26]
        raw_name = data[offset : offset + 32]
        if 0 < name_length <= 32:
            name = raw_name[:name_length].decode("ascii", "replace")
        else:
            name = raw_name.decode("ascii", "replace").rstrip(" \x00")
            _warn(warnings, f"directory entry {name!r}: bad name length {name_length}")
        if entry_type != DIR_ENTRY_TYPE_UID:
            kind = " (link)" if entry_type == DIR_ENTRY_TYPE_LINK else ""
            _warn(
                warnings,
                f"directory entry {name!r}: unhandled entry type "
                f"{entry_type}{kind}; listed without an object",
            )
        return DirEntry(
            name=name,
            entry_type=entry_type,
            uid=bytes(data[offset + 0x28 : offset + 0x30]),
        )

    for slot in range(list_size):
        offset = DIR_LINEAR_OFFSET + DIR_ENTRY_SIZE * slot
        if offset + DIR_ENTRY_SIZE > len(data):
            _warn(warnings, "directory linear list truncated; partial listing")
            break
        entry = decode_slot(offset)
        if entry is not None:
            entries.append(entry)
    for block in range(1, high_block + 1):
        offset = DIR_POOL_OFFSET + (block - 1) * DIR_POOL_BLOCK_SIZE
        if offset + DIR_POOL_BLOCK_SIZE > len(data):
            _warn(
                warnings,
                f"directory entry-block pool truncated at block {block}; "
                "partial listing",
            )
            break
        block_type = data[offset + 4]
        if block_type == 0:  # free block
            continue
        if block_type == 3:  # link text storage; consumed via link entries
            continue
        if block_type != 1:
            _warn(
                warnings, f"directory pool block {block}: unhandled type {block_type}"
            )
            continue
        for i in range(DIR_POOL_ENTRIES_PER_BLOCK):
            entry = decode_slot(offset + 6 + DIR_ENTRY_SIZE * i)
            if entry is not None:
                entries.append(entry)
    if len(entries) != declared_count:
        _warn(
            warnings,
            f"directory declares {declared_count} entries but {len(entries)} decoded",
        )
    return entries


# ---------------------------------------------------------------------------
# File maps and the catalog
# ---------------------------------------------------------------------------

_L1_SPAN = 256  # daddrs per indirect block
_DIRECT_PAGES = 32
_ZERO_BLOCK = bytes(BLOCK)


def _collect_page_daddrs(
    data: bytes, lv_base: int, vtoce: Vtoce, warnings: list
) -> "tuple[list[int], bool]":
    """Resolve a VTOCE's file map to one daddr per page (0 = sparse zeros).

    Pages 0-31 come from the direct map; pages 32-287 from the L1 indirect
    block; then L2 (256 L1 pointers) and L3 (256 L2 pointers).  A zero
    daddr at any level means a sparse zero page (or a whole sparse
    subtree).  Out-of-range daddrs are defensive-replaced by zero pages
    and flag the map as damaged.
    """
    max_daddr = len(data) // BLOCK - lv_base
    pages_needed = -(-vtoce.length // BLOCK)
    damaged = False
    if pages_needed > max_daddr:  # cannot exceed the volume; corrupt length
        _warn(
            warnings,
            f"object {vtoce.uid_text}: length {vtoce.length} exceeds the "
            "volume; map truncated",
        )
        pages_needed = max_daddr
        damaged = True

    index_cache: dict = {}

    def check(daddr: int) -> int:
        nonlocal damaged
        if daddr and not 0 < daddr < max_daddr:
            if not damaged:
                _warn(
                    warnings,
                    f"object {vtoce.uid_text}: file map references "
                    f"out-of-range daddr 0x{daddr:X}",
                )
            damaged = True
            return 0
        return daddr

    def index_words(daddr: int) -> "tuple[int, ...]":
        if daddr not in index_cache:
            start = (daddr + lv_base) * BLOCK
            index_cache[daddr] = struct.unpack(">256I", data[start : start + BLOCK])
        return index_cache[daddr]

    l2_base = _DIRECT_PAGES + _L1_SPAN
    l3_base = l2_base + _L1_SPAN * _L1_SPAN
    daddrs: list = []
    for page in range(pages_needed):
        if page < _DIRECT_PAGES:
            daddr = vtoce.direct_daddrs[page]
        elif page < l2_base:
            l1 = check(vtoce.l1_daddr)
            daddr = index_words(l1)[page - _DIRECT_PAGES] if l1 else 0
        elif page < l3_base:
            index = page - l2_base
            l2 = check(vtoce.l2_daddr)
            l1 = check(index_words(l2)[index // _L1_SPAN]) if l2 else 0
            daddr = index_words(l1)[index % _L1_SPAN] if l1 else 0
        else:
            index = page - l3_base
            l3 = check(vtoce.l3_daddr)
            l2 = check(index_words(l3)[index // (_L1_SPAN * _L1_SPAN)]) if l3 else 0
            l1 = check(index_words(l2)[(index // _L1_SPAN) % _L1_SPAN]) if l2 else 0
            daddr = index_words(l1)[index % _L1_SPAN] if l1 else 0
        daddrs.append(check(daddr))
    return daddrs, damaged


def _read_pages(data: bytes, lv_base: int, daddrs: list, length: int) -> bytes:
    """Assemble pages (sparse zero pages for daddr 0), truncated to length."""
    parts = []
    for daddr in daddrs:
        if daddr == 0:
            parts.append(_ZERO_BLOCK)
        else:
            start = (daddr + lv_base) * BLOCK
            parts.append(data[start : start + BLOCK])
    return b"".join(parts)[:length]


@dataclass
class AegisNode:
    """One catalogued object (or dangling directory entry) in the tree.

    A directory entry whose VTOCE cannot be found (damaged volume) is still
    listed: ``vtoce`` is None, ``missing`` is True and ``size`` is 0 -- the
    filesystem layer maps ``missing``/``map_damaged`` to the DMG attribute.
    """

    name: str
    path: str
    entry_type: int  # raw directory entry type (1 = normal UID entry)
    vtoce: Optional[Vtoce]
    page_daddrs: list = field(default_factory=list, repr=False)
    map_damaged: bool = False
    header_stripped: bool = False  # 32-byte storage header hidden from reads
    children: list = field(default_factory=list, repr=False)

    @property
    def missing(self) -> bool:
        """True when the directory entry's VTOCE was not found."""
        return self.vtoce is None

    @property
    def is_dir(self) -> bool:
        return self.vtoce is not None and self.vtoce.kind_is_dir

    @property
    def size(self) -> int:
        """Object size in bytes, minus the storage header when hidden."""
        if self.vtoce is None:
            return 0
        if self.header_stripped:
            return max(0, self.vtoce.length - STORAGE_HEADER_SIZE)
        return self.vtoce.length


@dataclass
class AegisCatalog:
    """The fully-walked object catalog of one AEGIS volume.

    ``root`` is the volume entry directory (vtoc_hdr.root_dir_vtocx); the
    network-root wrapper directory ``//`` is kept aside as
    ``network_root``/``node_entry_name`` metadata, never a path component
    (spec section 4).  ACL objects are hidden from children but counted;
    VTOCEs reachable neither from the tree nor as ACLs are counted as
    ``unreferenced``.
    """

    pv: PvLabel
    lv: LvLabel
    vtoc: Vtoc = field(repr=False)
    lv_base: int = 1
    root: AegisNode = None
    network_root: Optional[AegisNode] = None
    node_entry_name: Optional[str] = None
    objects: dict = field(default_factory=dict, repr=False)  # uid bytes -> Vtoce
    counts: dict = field(default_factory=dict)  # {"files", "dirs", "acl"}
    reachable_objects: int = 0
    unreferenced: int = 0
    warnings: list = field(default_factory=list)
    _data: bytes = field(default=b"", repr=False)
    _by_path: dict = field(default_factory=dict, repr=False)
    _by_path_ci: dict = field(default_factory=dict, repr=False)

    def lookup(self, path: str) -> AegisNode:
        """Resolve a catalog path, case-sensitively first, then
        case-insensitively (AEGIS names are case-sensitive ASCII but SR9
        volumes are conventionally uppercase)."""
        norm = "/" + "/".join(part for part in path.split("/") if part)
        node = self._by_path.get(norm)
        if node is None:
            node = self._by_path_ci.get(norm.casefold())
        if node is None:
            raise FileNotFoundError(path)
        return node

    def read(self, entry: AegisNode) -> bytes:
        """Assemble a file's content; storage header stripped when hidden."""
        if entry.is_dir:
            raise IsADirectoryError(entry.path)
        if entry.vtoce is None:
            return b""
        raw = _read_pages(
            self._data, self.lv_base, entry.page_daddrs, entry.vtoce.length
        )
        if entry.header_stripped:
            return raw[STORAGE_HEADER_SIZE:]
        return raw


def build_catalog(data: bytes) -> AegisCatalog:
    """Walk a whole AEGIS volume image into an :class:`AegisCatalog`.

    Raises :class:`AegisError` only on structural failure (no PV/LV label,
    no VTOC, missing volume entry directory); everything else degrades
    gracefully into ``catalog.warnings``.
    """
    pv = parse_pv_label(data)
    lv_base = pv.lv_daddr
    lv = parse_lv_label(data, lv_base)
    vtoc = read_vtoc(data, lv_base)

    warnings: list = []
    by_uid: dict = {}
    for vtocx in sorted(vtoc.entries):
        vtoce = vtoc.entries[vtocx]
        if vtoce.uid in by_uid:
            _warn(warnings, f"duplicate UID {vtoce.uid_text} in the VTOC; first wins")
            continue
        by_uid[vtoce.uid] = vtoce

    root_vtoce = vtoc.entries.get(lv.root_dir_vtocx)
    if root_vtoce is None or not root_vtoce.kind_is_dir:
        raise AegisError(
            f"volume entry directory (vtocx 0x{lv.root_dir_vtocx:X}) missing "
            "or not a directory"
        )

    visited: set = set()
    by_path: dict = {}
    by_path_ci: dict = {}

    def register(node: AegisNode) -> None:
        if node.path in by_path:
            _warn(warnings, f"duplicate path {node.path}; first wins")
        else:
            by_path[node.path] = node
        by_path_ci.setdefault(node.path.casefold(), node)

    def make_node(vtoce: Vtoce, name: str, path: str, entry_type: int) -> AegisNode:
        visited.add(vtoce.uid)
        pages, damaged = _collect_page_daddrs(data, lv_base, vtoce, warnings)
        node = AegisNode(
            name=name,
            path=path,
            entry_type=entry_type,
            vtoce=vtoce,
            page_daddrs=pages,
            map_damaged=damaged,
        )
        if vtoce.kind_is_dir:
            content = _read_pages(data, lv_base, pages, vtoce.length)
            for entry in parse_directory(content, warnings):
                child = build_child(entry, path)
                if child is not None:
                    node.children.append(child)
                    register(child)
        elif (
            vtoce.type_uid_hi in MANAGED_TYPE_HIS
            and vtoce.length >= STORAGE_HEADER_SIZE
            and pages
            and pages[0]
        ):
            start = (pages[0] + lv_base) * BLOCK
            if data[start : start + 4] == STORAGE_HEADER_MAGIC:
                node.header_stripped = True
        return node

    def build_child(entry: DirEntry, parent_path: str) -> Optional[AegisNode]:
        child_path = parent_path.rstrip("/") + "/" + entry.name
        if entry.entry_type != DIR_ENTRY_TYPE_UID:  # link or unknown: no object
            return AegisNode(entry.name, child_path, entry.entry_type, None)
        child_vtoce = by_uid.get(entry.uid)
        if child_vtoce is None:
            _warn(
                warnings,
                f"{child_path}: no VTOCE for UID {_uid_text(entry.uid)}; "
                "listed as damaged",
            )
            return AegisNode(entry.name, child_path, entry.entry_type, None)
        if child_vtoce.kind == KIND_ACL:  # hidden from listings, counted
            return None
        if child_vtoce.kind_is_dir and child_vtoce.uid in visited:
            _warn(
                warnings,
                f"{child_path}: directory {child_vtoce.uid_text} already "
                "catalogued elsewhere; not re-walked",
            )
            pages, damaged = _collect_page_daddrs(data, lv_base, child_vtoce, warnings)
            return AegisNode(
                entry.name,
                child_path,
                entry.entry_type,
                child_vtoce,
                page_daddrs=pages,
                map_damaged=damaged,
            )
        return make_node(child_vtoce, entry.name, child_path, entry.entry_type)

    root_node = make_node(root_vtoce, "/", "/", DIR_ENTRY_TYPE_UID)
    register(root_node)

    # Reachability-only walk for unexpected extra network-root entries.
    def visit_only(vtoce: Vtoce) -> None:
        if vtoce.uid in visited or vtoce.kind == KIND_ACL:
            return
        visited.add(vtoce.uid)
        if not vtoce.kind_is_dir:
            return
        pages, _ = _collect_page_daddrs(data, lv_base, vtoce, warnings)
        content = _read_pages(data, lv_base, pages, vtoce.length)
        for entry in parse_directory(content, warnings):
            if entry.entry_type == DIR_ENTRY_TYPE_UID:
                child = by_uid.get(entry.uid)
                if child is not None:
                    visit_only(child)

    network_root = None
    node_entry_name = None
    net_vtoce = vtoc.entries.get(lv.net_root_vtocx)
    if net_vtoce is not None and net_vtoce.uid != root_vtoce.uid:
        if net_vtoce.kind_is_dir:
            visited.add(net_vtoce.uid)
            pages, damaged = _collect_page_daddrs(data, lv_base, net_vtoce, warnings)
            network_root = AegisNode(
                "//", "//", DIR_ENTRY_TYPE_UID, net_vtoce, pages, damaged
            )
            content = _read_pages(data, lv_base, pages, net_vtoce.length)
            for entry in parse_directory(content, warnings):
                if (
                    entry.entry_type == DIR_ENTRY_TYPE_UID
                    and entry.uid == root_vtoce.uid
                ):
                    node_entry_name = entry.name
                    network_root.children.append(root_node)
                else:
                    _warn(
                        warnings,
                        f"network root entry {entry.name!r} is not the volume "
                        "entry directory; counted but not catalogued",
                    )
                    extra = by_uid.get(entry.uid)
                    if extra is not None:
                        visit_only(extra)
        else:
            _warn(warnings, "network root vtocx does not resolve to a directory")

    acl_count = sum(1 for e in vtoc.entries.values() if e.kind == KIND_ACL)
    dir_count = sum(1 for uid in visited if by_uid[uid].kind_is_dir)
    counts = {
        "files": len(visited) - dir_count,
        "dirs": dir_count,
        "acl": acl_count,
    }
    unreferenced = max(0, len(vtoc.entries) - len(visited) - acl_count)
    if unreferenced:
        _warn(warnings, f"{unreferenced} VTOCEs unreachable from the root walk")

    return AegisCatalog(
        pv=pv,
        lv=lv,
        vtoc=vtoc,
        lv_base=lv_base,
        root=root_node,
        network_root=network_root,
        node_entry_name=node_entry_name,
        objects=by_uid,
        counts=counts,
        reachable_objects=len(visited),
        unreferenced=unreferenced,
        warnings=warnings,
        _data=data,
        _by_path=by_path,
        _by_path_ci=by_path_ci,
    )
