"""Apollo AEGIS native-volume parser: PV/LV labels, VTOC and VTOCEs.

Parses the on-disk structures of AEGIS-native (SR9) Apollo floppies --
77x2x8x1024 (1,261,568-byte) images such as bootable utility disks.  The
normative format description lives in
``docs/superpowers/specs/2026-06-11-aegis-fs-design.md`` section 2; primary
sources are the *Domain Engineering Handbook Rev 4* (``eng_handbook_rev4.pdf``,
exact Pascal record layouts) and *AEGIS Internals and Data Structures*
(``aegis_internals.pdf``), cross-checked against the empirical dissection of
disk5 in ``docs/superpowers/research/apollo/aegis_empirical/``.

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
                 +20 blocks used, +24 dtu, +28 dtm, +2C parent-dir uid,
                 +34 extdtm|ref_cnt, +38 lock_key, +3C pad,
                 +40 direct daddrs[32], +C0/+C4/+C8 L1/L2/L3 indirect

Two documented evidence conflicts were resolved against disk5 (spec
section 2; throwaway verification scripts ran at implementation time):

- **vtoc_hdr offset**: the handbook (``lv_label_t`` p.2-18, ``vtoc_hdr_t``
  p.2-23, "Offsets given are from the start of the label") places the VTOC
  header at LV label +0x4C, immediately after the 0x20-byte ``bat_hdr_t``
  (+0x2C..+0x4B, p.2-3).  The empirical script ``01_labels.py`` used a
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

from .apollo_wbak import apollo_time_to_datetime

logger = logging.getLogger(__name__)

BLOCK = 1024
PV_MAGIC = b"APOLLO"  # at PV-label offset 2 (wbak media carries it at offset 0)

VTOCE_SIZE = 0xCC  # 204 bytes
VTOCE_SLOT_OFFSETS = (0x004, 0x0D0, 0x19C, 0x268, 0x334)
VTOCE_FLAG_IN_USE = 0x8000  # handbook p.2-22: U bit of the VTOCE header word
VTOC_MAP_ENTRIES = 8  # handbook p.2-23: vtoc_hdr.map[8]

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
        dtu=_time_or_none(_be32(entry, 0x24)),
        dtm=_time_or_none(_be32(entry, 0x28)),
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
    for bucket, head in enumerate(mapped[: lv.vtoc_bucket_count]):
        daddr = head
        while daddr:
            if daddr in visited or not 0 < daddr < max_daddr:
                logger.warning(
                    "AEGIS: VTOC bucket %d chain broken at daddr 0x%X",
                    bucket,
                    daddr,
                )
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

    return Vtoc(
        bucket_count=lv.vtoc_bucket_count,
        block_count=len(visited),
        entries=entries,
        block_bucket=block_bucket,
    )
