"""Teledisk TD0 container driver (read-only).

Opens Teledisk ``.td0`` archives — both "normal" (``'TD'`` signature,
uncompressed body) and "advanced" (``'td'``, one continuous LZHUF stream
after the 12-byte file header) — so every FatFloppy filesystem works on
them transparently.  Format reference: ``docs/superpowers/refs``
td0notes.txt (Dunfield) with the sector-CRC scope corrected by code
evidence; design: ``docs/superpowers/specs/2026-06-12-td0-driver-design.md``.

The driver deliberately mirrors the IMD driver's model (``imd.py``):

- the whole file is parsed at open into per-track, sector-ID-keyed maps;
- logical sector index n maps to the n-th smallest sector ID on the track
  (the sorted-ID rule of ``IMDImageDriver._add_track_format``), carried by
  each ``TrackFormat.sector_translation_table``;
- geometry is derived by grouping contiguous cylinders with identical
  track properties into ``TrackFormat`` ranges (variable everything);
- zero-sector tracks are skipped with a warning, exactly like
  ``IMDImageDriver._load_and_parse_imd_file``;
- reading a sector that is absent from the image (ID gap) warns and
  returns zero-fill, exactly like ``IMDImageDriver._read_original_sector``;
- the archive comment is exposed as ``self.comment`` (+
  ``self.creation_date``), which the GUI surfaces the same way as the
  IMD comment.

The driver is **read-only**: TD0 is archival container media (same
precedent as the Apollo driver); ``write_sector`` raises ``OSError``,
``flush`` is a no-op, and image creation is unsupported.

Rejected inputs (clear errors at ``validate_for_opening`` AND at open):
multi-volume sequels (sequence byte != 0), Teledisk 1.x "old advanced"
LZW compression (``'td'`` with version < 0x14 — no open implementation
exists), header-CRC mismatches, and decompressed payloads beyond the
``TD0_MAX_DECOMPRESSED`` ceiling (LZHUF output is unbounded relative to
input, so the driver must size-gate).  Track/sector CRC mismatches on a
validated file only warn — real archives contain them.
"""

import copy
import datetime
import struct
from pathlib import Path
from typing import ClassVar, Optional

import crcmod.predefined

from ..physical_format import PhysicalFormat, TrackFormat
from ..td0_compression import lzhuf_decompress
from ..utils.logging_config import get_logger
from .base_driver import DiskIODriver

logger = get_logger("TD0ImageDriver")

TD0_SIG_NORMAL = b"TD"
TD0_SIG_ADVANCED = b"td"
TD0_HEADER_SIZE = 12
TD0_MIN_ADVANCED_VERSION = 0x14

# Decompressed-payload ceiling.  4 MB is generous for any floppy (the largest
# real corpus payload is ~1.3 MB); a crafted LZHUF stream expanding beyond it
# is rejected as a decompression bomb.  Module-level so tests can lower it.
TD0_MAX_DECOMPRESSED = 4 * 1024 * 1024

# Data-rate byte: low bits encode the rate, bit 7 a global FM (single
# density) flag used by older Teledisk versions.
TD0_RATE_KBPS = {0: 250, 1: 300, 2: 500}
TD0_FM_MASK = 0x80

TD0_STEPPING_COMMENT_MASK = 0x80
TD0_COMMENT_HEADER_SIZE = 10

TD0_TRACK_HEADER_SIZE = 4
TD0_TRACK_END = 0xFF
TD0_TRACK_HEAD_MASK = 0x01

TD0_SECTOR_HEADER_SIZE = 6
TD0_SECTOR_MAX_SIZE_CODE = 6
TD0_FLAG_DUPLICATE = 0x01
TD0_FLAG_CRC_ERROR = 0x02
TD0_FLAG_DELETED_DAM = 0x04
TD0_FLAG_NO_DATA_MASK = 0x30  # 0x10 DOS-alloc skip | 0x20 ID without data

TD0_METHOD_RAW = 0
TD0_METHOD_PATTERN = 1
TD0_METHOD_RLE = 2

# Defaults mirroring the IMD driver's derived-geometry conventions.
TD0_RPM_FM_DEFAULT = 360
TD0_RPM_MFM_DEFAULT = 300
TD0_FALLBACK_BPS = 128

# CRC-16 poly 0xA097, MSB-first, init 0 (td0notes 3.10); crcmod ships it as
# the predefined 'crc-16-teledisk'.  Track/sector CRCs store only the low
# byte of the same function.
_crc16 = crcmod.predefined.mkCrcFun("crc-16-teledisk")


class TD0TrackInfo:
    """Decoded sector content and metadata for one (cylinder, head) track.

    Unlike IMDTrackInfo, which records file offsets into the raw image,
    sector content is stored fully decoded: advanced images are one
    compressed stream, so lazy offsets buy nothing.
    """

    def __init__(self, cylinder: int, head: int, is_fm: bool):
        self.cylinder = cylinder
        self.head = head
        self.is_fm = is_fm
        # Per-sector maps keyed by sector ID; recorded order kept separately.
        # Duplicate IDs: first valid occurrence wins (pinned byte-exactly by
        # the flattened-content oracle over MSDOS20T.TD0).
        self.sector_order: list[int] = []
        self.sector_data: dict[int, bytes] = {}
        self.sector_sizes: dict[int, int] = {}
        self.sector_flags: dict[int, int] = {}

    @property
    def sector_count(self) -> int:
        """Number of distinct sector IDs on the track."""
        return len(self.sector_data)


class TD0ImageDriver(DiskIODriver):
    """Read-only disk I/O driver for the Teledisk TD0 container format."""

    driver_type: ClassVar[str] = "TD0"
    driver_file_extensions: ClassVar[list[str]] = [".td0"]
    driver_category: ClassVar[str] = "metadata_based"
    driver_description: ClassVar[str] = "Teledisk TD0 archive (read-only)"
    driver_priority: ClassVar[int] = 50

    def __init__(self, file_path: str):
        """Opens and fully parses a TD0 file.

        Args:
            file_path: Path to the .td0 image.

        Raises:
            FileNotFoundError: If the file does not exist.
            ValueError: For any rejected or structurally corrupt input (see
                module docstring).
        """
        super().__init__()
        self.logger = get_logger(self.__class__.__name__)
        self.file_path: str = file_path
        self.physical_format: Optional[PhysicalFormat] = None

        # Archive metadata (surfaced like the IMD comment).
        self.comment: str = ""
        self.creation_date: Optional[datetime.datetime] = None
        self.teledisk_version: str = ""
        self.data_rate_kbps: int = 250
        self.drive_type: int = 0
        self.dos_allocation: bool = False
        self.sides: int = 1
        self.is_fm_global: bool = False
        self.advanced_compression: bool = False

        # Per-disk anomaly counters (archival honesty: recorded, not fatal).
        self.crc_error_sector_count: int = 0
        self.deleted_dam_sector_count: int = 0
        self.duplicate_sector_count: int = 0
        self.skipped_data_sector_count: int = 0

        self.tracks: dict[tuple[int, int], TD0TrackInfo] = {}
        self._sector_crc_warned: bool = False

        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"TD0 file not found: {file_path}")

        self._load_and_parse(path.read_bytes())
        self.logger.info(
            f"Opened TD0 image: {file_path} "
            f"({'advanced' if self.advanced_compression else 'normal'} "
            f"compression, {len(self.tracks)} tracks)"
        )

    # ------------------------------------------------------------------
    # Capabilities (read-only archival container, like the Apollo driver)
    # ------------------------------------------------------------------

    @property
    def allows_geometry_override(self) -> bool:
        """Geometry can be overridden externally (mirrors IMD), with caveats."""
        return True

    @property
    def has_embedded_geometry(self) -> bool:
        """TD0 files contain complete geometry information."""
        return True

    @property
    def supports_in_place_formatting(self) -> bool:
        """TD0 archives are read-only; formatting is not supported."""
        return False

    @property
    def supports_new_image_creation(self) -> bool:
        """TD0 archives are read-only; new image creation is not supported."""
        return False

    def get_format_requirements(self) -> dict:
        """Returns format requirements (embedded geometry, like IMD)."""
        return {
            "needs_format_for_open": False,
            "needs_format_for_io": False,
            "can_derive_format": True,
            "preferred_detection_method": "embedded",
        }

    def initialize_new_image(
        self, _physical_format: Optional[PhysicalFormat] = None, _profile=None
    ) -> None:
        """Always raises — TD0 images are read-only archival media.

        Raises:
            NotImplementedError: Always.
        """
        raise NotImplementedError("TD0ImageDriver does not support creating new images")

    def flush(self) -> None:
        """No-op: the driver is read-only, so there is never dirty state."""
        # No write path -> nothing to persist.

    # ------------------------------------------------------------------
    # Sector I/O
    # ------------------------------------------------------------------

    def read_sector(self, cylinder: int, head: int, sector: int) -> bytes:
        """Reads a single sector.

        Args:
            cylinder: The cylinder number.
            head: The head number.
            sector: The logical sector index (0-based, sequential within
                track) — mapped to the n-th smallest sector ID via the
                track's translation table, exactly like the IMD driver.

        Returns:
            The decoded sector data (zero-fill for flags & 0x30 sectors and
            for IDs absent from the image, mirroring IMD's missing-sector
            behavior).

        Raises:
            ValueError: If no physical format is available or the track has
                no TrackFormat (e.g. a skipped zero-sector track).
            IndexError: If the logical index is out of range for the track.
        """
        if not self.physical_format:
            raise ValueError("No physical format available")

        track_format = self.physical_format.get_track_format(cylinder, head)
        sector_id = track_format.logical_to_physical_sector(sector)

        track_info = self.tracks.get((cylinder, head))
        expected_size = self._expected_sector_size(
            track_info, cylinder, head, sector_id
        )

        if track_info is None or sector_id not in track_info.sector_data:
            # Mirror IMDImageDriver._read_original_sector: a missing track
            # or sector-ID gap warns and reads as zeros, it does not raise.
            self.logger.warning(
                f"Missing track or sector C:{cylinder} H:{head} ID:{sector_id}"
            )
            return bytes(expected_size)

        data = track_info.sector_data[sector_id]
        if len(data) < expected_size:
            return data.ljust(expected_size, b"\0")
        return data[:expected_size]

    def write_sector(
        self, _cylinder: int, _head: int, _sector: int, _data: bytes
    ) -> None:
        """Always raises — TD0 archives are read-only.

        Raises:
            OSError: Always.
        """
        raise OSError("Teledisk TD0 images are read-only archival media")

    # ------------------------------------------------------------------
    # Geometry
    # ------------------------------------------------------------------

    def set_physical_format(self, physical_format: PhysicalFormat) -> None:
        """Overrides the embedded geometry (external format), with a warning.

        Args:
            physical_format: The PhysicalFormat object to apply.

        Raises:
            TypeError: If the provided object is not a PhysicalFormat.
        """
        if not isinstance(physical_format, PhysicalFormat):
            raise TypeError("Expected PhysicalFormat object")
        if self.tracks:
            self.logger.warning(
                "External physical format set, may conflict with TD0 data"
            )
        self.physical_format = copy.deepcopy(physical_format)

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate_for_opening(
        self, source: str, **_kwargs
    ) -> tuple[bool, Optional[str]]:
        """Cheap magic gate: signature, sequence, version and header CRC.

        Only the 12-byte file header is read, making this safe to call on
        every candidate during content-based auto-detection.  Random files
        are rejected by the signature + CRC-16 combination.

        Args:
            source: Path to the candidate file.

        Returns:
            ``(True, None)`` if the header is a valid, supported TD0;
            ``(False, reason)`` otherwise.
        """
        path = Path(source)
        if not path.exists():
            return False, f"TD0 file not found: {source}"
        try:
            with path.open("rb") as f:
                header = f.read(TD0_HEADER_SIZE)
        except Exception as e:  # pragma: no cover - filesystem errors
            return False, f"Cannot read TD0 header: {e}"
        return self._check_header(header)

    def validate_state_for_opening(self) -> tuple[bool, Optional[str]]:
        """Validates driver state after opening (mirrors IMD)."""
        if not self.file_path:
            return False, "TD0 driver has no file path"
        if not self.physical_format:
            return False, "TD0 file loaded but no physical format derived"
        return True, None

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _check_header(header: bytes) -> tuple[bool, Optional[str]]:
        """Validates the 12-byte file header (shared by validate and open).

        Returns:
            ``(True, None)`` or ``(False, reason)``.
        """
        if len(header) < TD0_HEADER_SIZE:
            return False, "File shorter than the 12-byte TD0 header"
        sig = header[:2]
        if sig not in (TD0_SIG_NORMAL, TD0_SIG_ADVANCED):
            return False, "Not a TD0 file: missing 'TD'/'td' signature"
        sequence = header[2]
        if sequence != 0:
            return False, (
                f"TD0 multi-volume sequel (sequence byte {sequence}) is not "
                "supported; open the first volume of the set"
            )
        version = header[4]
        if sig == TD0_SIG_ADVANCED and version < TD0_MIN_ADVANCED_VERSION:
            return False, (
                f"Teledisk 1.x 'old advanced' LZW compression "
                f"(version 0x{version:02X}) is not supported - no open "
                "implementation of that scheme exists"
            )
        stored_crc = struct.unpack_from("<H", header, 10)[0]
        if _crc16(header[:10]) != stored_crc:
            return False, "TD0 header CRC mismatch"
        return True, None

    def _load_and_parse(self, raw: bytes) -> None:
        """Parses the whole file: header, optional comment, tracks, sectors.

        Raises:
            ValueError: On any rejected or structurally corrupt input.
        """
        ok, error = self._check_header(raw[:TD0_HEADER_SIZE])
        if not ok:
            raise ValueError(f"{error}: {self.file_path}")

        (sig, _seq, _check_seq, version, rate, drive_type, stepping, dos, sides) = (
            struct.unpack_from("<2sBBBBBBBB", raw, 0)
        )
        self.advanced_compression = sig == TD0_SIG_ADVANCED
        self.teledisk_version = f"{version >> 4}.{version & 0x0F}"
        self.is_fm_global = bool(rate & TD0_FM_MASK)
        rate_code = rate & ~TD0_FM_MASK
        if rate_code in TD0_RATE_KBPS:
            self.data_rate_kbps = TD0_RATE_KBPS[rate_code]
        else:
            self.logger.warning(f"Unknown TD0 data-rate code {rate_code}, assuming 250")
            self.data_rate_kbps = 250
        self.drive_type = drive_type
        self.dos_allocation = dos != 0
        self.sides = 1 if sides == 1 else 2  # td0notes 3.9: anything-else = two

        if self.advanced_compression:
            try:
                # Module-global ceiling lookup so tests can monkeypatch it.
                body = lzhuf_decompress(
                    raw[TD0_HEADER_SIZE:], max_output=TD0_MAX_DECOMPRESSED
                )
            except ValueError as e:
                raise ValueError(
                    f"TD0 advanced decompression rejected for {self.file_path}: {e}"
                ) from e
        else:
            body = raw[TD0_HEADER_SIZE:]

        offset = 0
        if stepping & TD0_STEPPING_COMMENT_MASK:
            offset = self._parse_comment(body)

        self._parse_tracks(body, offset)

        if not self.tracks:
            raise ValueError(f"TD0 file contains no usable tracks: {self.file_path}")

        self._derive_physical_format()

    def _parse_comment(self, body: bytes) -> int:
        """Parses the comment block; returns the offset of the first track.

        Comment CRC scope (verified against the corpus): bytes 2..9 of the
        comment header plus the comment data.  Mismatches warn and continue.

        Raises:
            ValueError: If the comment block is truncated.
        """
        if len(body) < TD0_COMMENT_HEADER_SIZE:
            raise ValueError(f"Truncated TD0 comment header: {self.file_path}")
        stored_crc, length = struct.unpack_from("<HH", body, 0)
        end = TD0_COMMENT_HEADER_SIZE + length
        if end > len(body):
            raise ValueError(f"Truncated TD0 comment data: {self.file_path}")

        if _crc16(body[2:end]) != stored_crc:
            self.logger.warning("TD0 comment CRC mismatch, keeping comment anyway")

        year, month, day, hour, minute, second = body[4:10]
        try:
            # Year is an offset from 1900; month is 0-based (td0notes 4.3/4.4).
            self.creation_date = datetime.datetime(
                1900 + year, month + 1, day, hour, minute, second
            )
        except ValueError:
            self.logger.warning(
                f"Invalid TD0 comment timestamp "
                f"{(year, month, day, hour, minute, second)}"
            )
            self.creation_date = None

        text = body[TD0_COMMENT_HEADER_SIZE:end]
        # NUL-terminated lines, NUL-padded at the end; newline-join and strip.
        self.comment = (
            text.replace(b"\0", b"\n").decode("ascii", errors="replace").rstrip()
        )
        return end

    def _parse_tracks(self, body: bytes, offset: int) -> None:
        """Parses track and sector records until the 0xFF terminator.

        Raises:
            ValueError: On EOF before the terminator or malformed records.
        """
        while True:
            if offset >= len(body):
                raise ValueError(
                    f"Unexpected EOF before TD0 track-list terminator: {self.file_path}"
                )
            nsec = body[offset]
            if nsec == TD0_TRACK_END:
                break  # data may follow the terminator - stop (td0notes 5.1)

            if offset + TD0_TRACK_HEADER_SIZE > len(body):
                raise ValueError(f"Truncated TD0 track header: {self.file_path}")
            cyl = body[offset + 1]
            head_byte = body[offset + 2]
            stored_tcrc = body[offset + 3]
            # Track CRC is the low byte over header bytes 0-2; a stored 0 is
            # tolerated (some writers leave it blank).
            calc_tcrc = _crc16(body[offset : offset + 3]) & 0xFF
            if stored_tcrc not in (0, calc_tcrc):
                self.logger.warning(
                    f"TD0 track header CRC mismatch at C:{cyl} "
                    f"(stored {stored_tcrc:#04x}, calculated {calc_tcrc:#04x})"
                )
            offset += TD0_TRACK_HEADER_SIZE

            head = head_byte & TD0_TRACK_HEAD_MASK
            is_fm = bool(head_byte & TD0_FM_MASK) or self.is_fm_global

            if nsec == 0:
                # Mirror IMDImageDriver._load_and_parse_imd_file: zero-sector
                # tracks are real (3 corpus hits) but carry nothing; skip them
                # without registering a track, so they do not grow geometry.
                self.logger.warning(
                    f"Track C:{cyl} H:{head} has zero sectors, skipping"
                )
                continue

            track_info = self.tracks.get((cyl, head))
            if track_info is not None:
                # Repeated (cyl, head) track records do not occur in the
                # corpus; keep the first one, consistent with the
                # first-occurrence-wins rule for duplicate sector IDs.
                self.logger.warning(
                    f"Duplicate TD0 track record for C:{cyl} H:{head}, "
                    "merging sectors (first occurrence wins)"
                )
            else:
                track_info = TD0TrackInfo(cyl, head, is_fm)
                self.tracks[(cyl, head)] = track_info

            for _ in range(nsec):
                offset = self._parse_sector(body, offset, track_info)

    def _parse_sector(self, body: bytes, offset: int, track_info: TD0TrackInfo) -> int:
        """Parses one sector record into `track_info`; returns the new offset.

        Raises:
            ValueError: On truncation, invalid size codes or unknown
                encoding methods.
        """
        if offset + TD0_SECTOR_HEADER_SIZE > len(body):
            raise ValueError(f"Truncated TD0 sector header: {self.file_path}")
        _id_cyl, _id_head, sector_id, size_code, flags, stored_scrc = body[
            offset : offset + TD0_SECTOR_HEADER_SIZE
        ]
        offset += TD0_SECTOR_HEADER_SIZE

        if size_code > TD0_SECTOR_MAX_SIZE_CODE:
            raise ValueError(
                f"Invalid TD0 sector size code {size_code} at "
                f"C:{track_info.cylinder} H:{track_info.head} ID:{sector_id}"
            )
        size = 128 << size_code

        if flags & TD0_FLAG_NO_DATA_MASK:
            # No data block follows (td0notes 6.5): DOS-allocation skip or
            # ID-without-data.  Zero-fill, matching greaseweazle.
            data = bytes(size)
            self.skipped_data_sector_count += 1
        else:
            if offset + 3 > len(body):
                raise ValueError(f"Truncated TD0 sector data header: {self.file_path}")
            data_len, method = struct.unpack_from("<HB", body, offset)
            offset += 3
            # data_len counts the method byte plus the encoded block
            # (td0notes 7.1: "data block size + 1").
            block_len = data_len - 1
            if block_len < 0 or offset + block_len > len(body):
                raise ValueError(f"Truncated TD0 sector data: {self.file_path}")
            block = body[offset : offset + block_len]
            offset += block_len
            data = self._decode_sector_data(method, block, size, track_info, sector_id)

            # Sector CRC: low byte over the fully-DECODED data only
            # (td0notes' prose is wrong here; all implementations and the
            # whole corpus agree).  Mismatch warns once per disk.
            if (_crc16(data) & 0xFF) != stored_scrc and not self._sector_crc_warned:
                self.logger.warning(
                    f"TD0 sector data CRC mismatch at C:{track_info.cylinder} "
                    f"H:{track_info.head} ID:{sector_id} (further mismatches "
                    "not logged)"
                )
                self._sector_crc_warned = True

        if flags & TD0_FLAG_CRC_ERROR:
            self.crc_error_sector_count += 1
        if flags & TD0_FLAG_DELETED_DAM:
            self.deleted_dam_sector_count += 1

        if sector_id in track_info.sector_data:
            # Duplicate sector ID within a track: first valid occurrence
            # wins; the rest are counted as anomalies (matches the flattened
            # oracle over MSDOS20T.TD0, which contains 10 of these).
            self.duplicate_sector_count += 1
            return offset

        track_info.sector_order.append(sector_id)
        track_info.sector_data[sector_id] = data
        track_info.sector_sizes[sector_id] = size
        track_info.sector_flags[sector_id] = flags
        return offset

    def _decode_sector_data(
        self,
        method: int,
        block: bytes,
        size: int,
        track_info: TD0TrackInfo,
        sector_id: int,
    ) -> bytes:
        """Decodes one sector data block (methods 0/1/2, td0notes section 7).

        Returns exactly `size` bytes (short decodes are zero-padded with a
        warning, mirroring the IMD driver's padding of short records).

        Raises:
            ValueError: On an unknown encoding method.
        """
        if method == TD0_METHOD_RAW:
            data = block
        elif method == TD0_METHOD_PATTERN:
            # Repeated 2-byte pattern records: u16 count + 2 pattern bytes,
            # repeated until the sector is filled.
            out = bytearray()
            pos = 0
            while pos + 4 <= len(block) and len(out) < size:
                count, pattern = struct.unpack_from("<H2s", block, pos)
                pos += 4
                out += pattern * count
            data = bytes(out)
        elif method == TD0_METHOD_RLE:
            # RLE records: length byte L. L == 0: literal (next byte is a
            # count n, then n raw bytes).  L != 0: a block of L*2 bytes
            # follows a repeat count, and is emitted that many times.
            out = bytearray()
            pos = 0
            while pos < len(block) and len(out) < size:
                length = block[pos]
                if length == 0:
                    if pos + 2 > len(block):
                        break
                    count = block[pos + 1]
                    out += block[pos + 2 : pos + 2 + count]
                    pos += 2 + count
                else:
                    run = length * 2
                    if pos + 2 + run > len(block):
                        break
                    repeat = block[pos + 1]
                    out += block[pos + 2 : pos + 2 + run] * repeat
                    pos += 2 + run
            data = bytes(out)
        else:
            raise ValueError(
                f"Unknown TD0 sector encoding method {method} at "
                f"C:{track_info.cylinder} H:{track_info.head} ID:{sector_id}"
            )

        if len(data) < size:
            self.logger.warning(
                f"Short TD0 sector decode ({len(data)}/{size} bytes) at "
                f"C:{track_info.cylinder} H:{track_info.head} ID:{sector_id}, "
                "zero-padding"
            )
            data = data.ljust(size, b"\0")
        return data[:size]

    # ------------------------------------------------------------------
    # Geometry derivation (mirrors IMDImageDriver._derive_physical_format)
    # ------------------------------------------------------------------

    def _expected_sector_size(
        self,
        track_info: Optional[TD0TrackInfo],
        cylinder: int,
        head: int,
        sector_id: int,
    ) -> int:
        """Determines a sector's size, preferring per-sector metadata."""
        if track_info is not None and sector_id in track_info.sector_sizes:
            return track_info.sector_sizes[sector_id]
        if self.physical_format:
            try:
                return self.physical_format.get_bytes_per_sector(cylinder, head)
            except ValueError:
                return self.physical_format.bytes_per_sector
        return TD0_FALLBACK_BPS

    def _track_properties(
        self, track_info: TD0TrackInfo
    ) -> tuple[str, int, int, int, tuple[int, ...]]:
        """(encoding, rate, spt, bps, sorted-id tuple) used for grouping."""
        encoding = "FM" if track_info.is_fm else "MFM"
        spt = track_info.sector_count
        # Mirror IMD's variable-size convention: a track's nominal bps is
        # the first recorded sector's size.
        bps = track_info.sector_sizes[track_info.sector_order[0]]
        return (
            encoding,
            self.data_rate_kbps,
            spt,
            bps,
            tuple(sorted(track_info.sector_data)),
        )

    def _add_track_format(
        self,
        track_formats: list[TrackFormat],
        start: int,
        end: int,
        props: tuple[str, int, int, int, tuple[int, ...]],
        max_head_idx: int,
    ) -> None:
        """Creates one TrackFormat covering cylinders start..end."""
        encoding, rate, spt, bps, sorted_ids = props
        # The logical-to-ID mapping every filesystem expects is ascending
        # sector ID: logical index n -> the n-th smallest ID.  This is the
        # exact rule of IMDImageDriver._add_track_format (which sorts the
        # IMD numbering map); TD0 sector IDs are arbitrary (17-26, 0-based,
        # bogus >= 100), so the sorted table carries them verbatim.
        track_formats.append(
            TrackFormat(
                track_start=start,
                track_end=end,
                head_start=0,
                head_end=max_head_idx,
                sectors_per_track=spt,
                encoding=encoding,
                rate=rate,
                interleave=1,
                bytes_per_sector=bps,
                sector_translation_table=list(sorted_ids),
                iam_present=False,
                gap1_bytes=None,
                gap2_bytes=None,
                gap3_bytes=0,
                cskew=None,
                hskew=None,
            )
        )

    def _derive_physical_format(self) -> None:
        """Derives the PhysicalFormat from parsed tracks.

        Groups contiguous cylinders whose head-0 track has identical
        properties into TrackFormat ranges, mirroring the IMD driver's
        derivation (including its assumption that head 1 matches head 0,
        and that cylinders missing a head-0 track form coverage gaps).
        """
        max_cyl_idx = max(cyl for cyl, _head in self.tracks)
        max_head_idx = max(head for _cyl, head in self.tracks)

        track_formats: list[TrackFormat] = []
        current_start_cyl = 0
        prev_props = None

        for cyl in range(max_cyl_idx + 1):
            track_info = self.tracks.get((cyl, 0))

            if not track_info:
                if prev_props is not None:
                    self._add_track_format(
                        track_formats,
                        current_start_cyl,
                        cyl - 1,
                        prev_props,
                        max_head_idx,
                    )
                current_start_cyl = cyl + 1
                prev_props = None
                continue

            current_props = self._track_properties(track_info)
            if prev_props is not None and current_props != prev_props:
                self._add_track_format(
                    track_formats, current_start_cyl, cyl - 1, prev_props, max_head_idx
                )
                current_start_cyl = cyl

            prev_props = current_props

        if prev_props is not None:
            self._add_track_format(
                track_formats, current_start_cyl, max_cyl_idx, prev_props, max_head_idx
            )

        # RPM heuristic mirroring IMD's: FM (8"/SD lineages) and 300 kbps
        # imply 360 rpm; otherwise 300 rpm.
        ref_ti = self.tracks.get((0, 0)) or self.tracks[min(self.tracks.keys())]
        rpm = (
            TD0_RPM_FM_DEFAULT
            if ref_ti.is_fm or self.data_rate_kbps == 300
            else TD0_RPM_MFM_DEFAULT
        )

        all_bps = {tf.bytes_per_sector for tf in track_formats}
        disk_bps = next(iter(all_bps)) if len(all_bps) == 1 else TD0_FALLBACK_BPS

        self.physical_format = PhysicalFormat(
            cylinders=max_cyl_idx + 1,
            heads=max_head_idx + 1,
            rpm=rpm,
            heads_inverted=False,
            bytes_per_sector=disk_bps,
            track_formats=track_formats,
        )
        self.logger.info(
            f"Derived TD0 format: Cyls={self.physical_format.cylinders}, "
            f"Heads={self.physical_format.heads}, RPM={rpm}, "
            f"Variable BPS={len(all_bps) > 1}"
        )
