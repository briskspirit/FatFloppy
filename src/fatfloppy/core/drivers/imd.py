# src/fatfloppy/core/drivers/imd.py
import struct
import datetime
import copy
import os
import re
from typing import List, Optional, Tuple, Dict, Any

from .base_driver import DiskIODriver
from ..physical_format import PhysicalFormat, TrackFormat
from ..format_profile import FormatProfile
from ..utils.logging_config import get_logger
from ..._version import __version__ as fatfloppy_version

logger = get_logger("IMDImageDriver")

# IMD Sector Data Record Types
IMD_SECTOR_UNAVAILABLE = 0
IMD_SECTOR_NORMAL = 1
IMD_SECTOR_COMPRESSED = 2
IMD_SECTOR_NORMAL_DEL = 3
IMD_SECTOR_COMPRESSED_DEL = 4
IMD_SECTOR_NORMAL_ERR = 5
IMD_SECTOR_COMPRESSED_ERR = 6
IMD_SECTOR_NORMAL_DEL_ERR = 7
IMD_SECTOR_COMPRESSED_DEL_ERR = 8

# Map IMD Mode byte to Rate (kbps) and Encoding (FM/MFM)
IMD_MODE_MAP = {
    0: (500, "FM", "SD"),
    1: (300, "FM", "SD"),
    2: (250, "FM", "SD"),
    3: (500, "MFM", "HD/ED"),
    4: (300, "MFM", "HD"),
    5: (250, "MFM", "DD"),
}

# Map IMD Sector Size Code to bytes
IMD_SECTOR_SIZE_MAP = {
    0: 128,
    1: 256,
    2: 512,
    3: 1024,
    4: 2048,
    5: 4096,
    6: 8192,
}


class IMDTrackInfo:
    def __init__(self, mode: int, cylinder: int, head_flags: int,
                 num_sectors: int, sector_size_code: int):
        self.mode = mode
        self.cylinder = cylinder
        self.head = head_flags & 1
        self.has_cyl_map = bool(head_flags & 0x80)
        self.has_head_map = bool(head_flags & 0x40)
        self.num_sectors = num_sectors
        self.sector_size_code = sector_size_code
        self.sector_size_map: Optional[Dict[int, int]] = None  # Sector Num -> Size
        self.sector_size = self._get_base_sector_size()
        self.sector_num_map: List[int] = []
        self.sector_cyl_map: Optional[Dict[int, int]] = None  # Sector Num -> Logical Cyl
        self.sector_head_map: Optional[Dict[int, int]] = None  # Sector Num -> Logical Head
        self.sector_data_info: Dict[int, Tuple[int, int, int]] = {}  # Sector Num -> (Offset, Type, DataSize)

    def _get_base_sector_size(self) -> int:
        if self.sector_size_code == 0xFF:
            return -1  # Variable size indicated by map
        size = IMD_SECTOR_SIZE_MAP.get(self.sector_size_code)
        if size is None:
            raise ValueError(f"Invalid sector size code: {self.sector_size_code}")
        return size

    def get_sector_size(self, sector_num: int) -> int:
        if self.sector_size_map:
            size = self.sector_size_map.get(sector_num)
            if size is None:
                raise ValueError(f"Sector {sector_num} not found in variable size map for C:{self.cylinder} H:{self.head}")
            return size
        elif self.sector_size != -1:
            return self.sector_size
        else:
            raise ValueError(f"Inconsistent sector size information for C:{self.cylinder} H:{self.head}")


class IMDImageDriver(DiskIODriver):
    def __init__(self, file_path: str):
        super().__init__()
        self.file_path = file_path
        self.physical_format = None
        self.comment = ""
        self.imd_version = ""
        self.creation_date = None
        self.tracks = {}
        self.image_data = bytearray()
        self.dirty = False
        self.file_loaded = False
        self.modified_sector_data = {}
        self.last_format_fill_byte = None
        self._sector_size_cache = {}  # (cylinder, head, sector) -> size
        self.uses_physical_heads = False

        if os.path.exists(self.file_path):
            try:
                self._load_and_parse_imd_file()
                self.file_loaded = True
                for (cyl, head), track_info in self.tracks.items():
                    for sector in track_info.sector_num_map:
                        self._sector_size_cache[(cyl, head, sector)] = track_info.get_sector_size(sector)
            except FileNotFoundError:
                logger.error(f"IMD file not found: {self.file_path}")
                raise
            except ValueError as e:
                logger.error(f"Parse error in IMD file {self.file_path}: {e}")
                raise
            except Exception as e:
                logger.exception(f"Unexpected error loading IMD file {self.file_path}: {e}")
                raise
        else:
            logger.info(f"IMD file '{self.file_path}' not found. Initializing empty driver.")
            self.creation_date = datetime.datetime.now()
            self.imd_version = "IMD 1.18"
            self.comment = f"{self.creation_date.strftime('%d/%m/%Y %H:%M:%S')}\r\nFatFloppy v{fatfloppy_version}"

    def read_sector(self, cylinder: int, head: int, sector: int) -> bytes:
        if not self.physical_format:
            raise ValueError("No physical format available")

        sector_key = (cylinder, head, sector)
        if sector_key in self.modified_sector_data:
            logger.debug(f"Reading modified sector C:{cylinder} H:{head} S:{sector}")
            return self.modified_sector_data[sector_key]

        track_info = self.tracks.get((cylinder, head))
        expected_size = self._get_sector_size(track_info, cylinder, head, sector)

        if not track_info or sector not in track_info.sector_data_info:
            logger.warning(f"Missing track or sector C:{cylinder} H:{head} S:{sector}")
            return bytes(expected_size)

        offset, data_type, size = track_info.sector_data_info[sector]
        if data_type == IMD_SECTOR_UNAVAILABLE:
            return bytes(expected_size)
        elif data_type in (IMD_SECTOR_COMPRESSED, IMD_SECTOR_COMPRESSED_DEL, IMD_SECTOR_COMPRESSED_ERR, IMD_SECTOR_COMPRESSED_DEL_ERR):
            return bytes([self.image_data[offset]] * expected_size)
        elif data_type in (IMD_SECTOR_NORMAL, IMD_SECTOR_NORMAL_DEL, IMD_SECTOR_NORMAL_ERR, IMD_SECTOR_NORMAL_DEL_ERR):
            data = self.image_data[offset:offset + size]
            return data.ljust(expected_size, b'\0') if len(data) < expected_size else data[:expected_size]
        logger.error(f"Unknown sector type {data_type} for C:{cylinder} H:{head} S:{sector}")
        return bytes(expected_size)

    def write_sector(self, cylinder: int, head: int, sector: int, data: bytes) -> None:
        if not self.physical_format:
            raise ValueError("No physical format set")

        track_info = self.tracks.get((cylinder, head))
        target_size = track_info.get_sector_size(sector) if track_info else self.physical_format.bytes_per_sector

        if len(data) != target_size:
            raise ValueError(f"Data size mismatch: {len(data)} vs {target_size}")

        if self.file_loaded and (not track_info or sector not in track_info.sector_data_info):
            raise IOError(f"Invalid sector C:{cylinder} H:{head} S:{sector}")

        self.modified_sector_data[(cylinder, head, sector)] = bytes(data)
        self.dirty = True
        logger.debug(f"Cached write for sector C:{cylinder} H:{head} S:{sector}")

    def flush(self) -> None:
        if not self.dirty or not self.physical_format:
            logger.debug("Nothing to flush or no format set")
            return

        logger.info(f"Flushing IMD to {self.file_path}")
        new_data = bytearray(f"{self.imd_version}: {self.comment}".encode('ascii', errors='ignore') + b'\x1A')
        new_tracks = {}

        for cyl in range(self.physical_format.cylinders):
            for head in range(self.physical_format.heads):
                track_key = (cyl, head)
                track_info = self.tracks.get(track_key)
                track_format = self.physical_format.get_track_format(cyl, head)
                mode = next((m for m, (r, e, _) in IMD_MODE_MAP.items() if r == track_format.rate and e == track_format.encoding), -1)
                if mode == -1:
                    logger.error(f"Unsupported format for C:{cyl} H:{head}")
                    continue

                size_code = track_info.sector_size_code if track_info else next((c for c, s in IMD_SECTOR_SIZE_MAP.items() if s == self.physical_format.bytes_per_sector), -1)
                if size_code == -1:
                    logger.error(f"Unsupported sector size for C:{cyl} H:{head}")
                    continue

                spt = track_format.sectors_per_track
                sector_map = track_info.sector_num_map if track_info and len(track_info.sector_num_map) == spt else list(range(1, spt + 1))
                head_flags = head & 1 | (0x80 if track_info and track_info.has_cyl_map else 0) | (0x40 if track_info and track_info.has_head_map else 0)

                rebuilt_info = IMDTrackInfo(mode, cyl, head_flags, spt, size_code)
                rebuilt_info.sector_num_map = sector_map
                if track_info and size_code == 0xFF:
                    rebuilt_info.sector_size_map = track_info.sector_size_map

                new_data.extend(struct.pack("<BBBBB", mode, cyl, head_flags, spt, size_code))
                new_data.extend(struct.pack(f"<{spt}B", *sector_map))

                for sector in sector_map:
                    sector_key = (cyl, head, sector)
                    sector_data = self.modified_sector_data.get(sector_key, self._read_original_sector(cyl, head, sector) if self.file_loaded else bytes([self.last_format_fill_byte or 0xE5] * self.physical_format.bytes_per_sector))
                    target_size = rebuilt_info.get_sector_size(sector)

                    if len(sector_data) != target_size or all(b == sector_data[0] for b in sector_data):
                        data_type, content = IMD_SECTOR_COMPRESSED, bytes([sector_data[0] if sector_data else 0xE5])
                    else:
                        data_type, content = IMD_SECTOR_NORMAL, sector_data

                    offset = len(new_data) + 1
                    new_data.extend([data_type, *content])
                    rebuilt_info.sector_data_info[sector] = (offset, data_type, len(content))

                new_tracks[track_key] = rebuilt_info

        try:
            with open(self.file_path, "wb") as f:
                f.write(new_data)
            self.image_data, self.tracks, self.modified_sector_data, self.dirty, self.file_loaded = new_data, new_tracks, {}, False, True
            logger.info("Flush successful")
        except Exception as e:
            logger.error(f"Flush failed: {e}")
            raise IOError(f"Failed to flush IMD: {e}") from e

    def set_physical_format(self, physical_format: PhysicalFormat) -> None:
        if not isinstance(physical_format, PhysicalFormat):
            raise TypeError("Expected PhysicalFormat object")
        logger.warning("External physical format set, may conflict with IMD data")
        self.physical_format = copy.deepcopy(physical_format)

    def read_boot_sector_data(self) -> Optional[bytes]:
        if not self.physical_format or (track_info := self.tracks.get((0, 0))) is None:
            logger.warning("Cannot read boot sector: no format or track")
            return None

        sector = 1 if 1 in track_info.sector_num_map else (track_info.sector_num_map[0] if track_info.sector_num_map else None)
        if sector is None:
            logger.warning("No sectors in track C:0 H:0")
            return None

        try:
            return self.read_sector(0, 0, sector)
        except Exception as e:
            logger.warning(f"Failed to read boot sector: {e}")
            return None

    def format_imd(self, profile: FormatProfile, fill_byte: int = 0xE5):
        logger.info(f"Formatting IMD with profile: {profile.name}")
        if not profile.physical_format:
            raise ValueError("Physical format required for formatting")

        self.physical_format = copy.deepcopy(profile.physical_format)
        self.tracks, self.image_data, self.modified_sector_data, self.dirty, self.file_loaded = {}, bytearray(), {}, True, False
        self.last_format_fill_byte = fill_byte

        self.image_data.extend(f"{self.imd_version}: {self.comment}".encode('ascii', errors='ignore') + b'\x1A')
        for cyl in range(self.physical_format.cylinders):
            for head in range(self.physical_format.heads):
                track_format = self.physical_format.get_track_format(cyl, head)
                mode = next((m for m, (r, e, _) in IMD_MODE_MAP.items() if r == track_format.rate and e == track_format.encoding), -1)
                size_code = next((c for c, s in IMD_SECTOR_SIZE_MAP.items() if s == self.physical_format.bytes_per_sector), -1)

                if mode == -1 or size_code == -1:
                    logger.error(f"Unsupported format for C:{cyl} H:{head}")
                    continue

                spt = track_format.sectors_per_track
                self.image_data.extend(struct.pack("<BBBBB", mode, cyl, head & 1, spt, size_code))
                self.image_data.extend(struct.pack(f"<{spt}B", *range(1, spt + 1)))
                for _ in range(spt):
                    self.image_data.extend([IMD_SECTOR_COMPRESSED, fill_byte])

        logger.info(f"Formatted IMD in memory, size: {len(self.image_data)} bytes")

    def _load_and_parse_imd_file(self):
        logger.info(f"Loading IMD file: {self.file_path}")
        try:
            with open(self.file_path, "rb") as f:
                self.image_data = bytearray(f.read())
        except Exception as e:
            raise ValueError(f"Cannot read file: {e}") from e

        if not self.image_data:
            raise ValueError("IMD file is empty")

        header_end_pos = self.image_data.find(b'\x1A')
        if header_end_pos == -1:
            raise ValueError("IMD header terminator (0x1A) not found")

        header_bytes = self.image_data[:header_end_pos]
        first_colon_pos = header_bytes.find(b':')

        if first_colon_pos != -1:
            self.imd_version = header_bytes[:first_colon_pos].decode('ascii', errors='ignore').strip()
            comment_bytes = header_bytes[first_colon_pos + 1:]
            try:
                comment_text = comment_bytes.decode('cp437', errors='replace')
                timestamp_pattern = r"^\s*\d{1,2}/\d{1,2}/\d{4}\s+\d{1,2}:\d{2}:\d{2}\s*[:]?\s*"
                self.comment = re.sub(timestamp_pattern, '', comment_text, count=1).strip()
            except Exception as e:
                logger.warning(f"Failed to decode comment: {e}")
                self.comment = "[Comment Decode Error]"
        else:
            logger.warning("No colon in header, using fallback parsing")
            self.imd_version = header_bytes.decode('ascii', errors='ignore').strip() if len(header_bytes) < 30 else ""
            self.comment = header_bytes.decode('cp437', errors='replace').strip() if len(header_bytes) >= 30 else ""

        offset = header_end_pos + 1
        max_cyl = -1
        max_head = -1
        parsed_track_count = 0

        while offset < len(self.image_data):
            if offset + 5 > len(self.image_data):
                if all(b == 0 for b in self.image_data[offset:]):
                    logger.debug("Reached padded end of file")
                    break
                raise ValueError(f"Incomplete track header at offset {offset}")

            mode, cyl, head_flags, num_sectors, sector_size_code = struct.unpack_from("<BBBBB", self.image_data, offset)
            offset += 5

            if num_sectors == 0:
                logger.warning(f"Track C:{cyl} H:{head_flags & 1} has zero sectors, stopping parse")
                break

            track_info = IMDTrackInfo(mode, cyl, head_flags, num_sectors, sector_size_code)
            max_cyl = max(max_cyl, cyl)
            max_head = max(max_head, track_info.head)

            # Parse sector numbering map
            if offset + num_sectors > len(self.image_data):
                raise ValueError(f"EOF in sector map for C:{cyl} H:{track_info.head}")
            track_info.sector_num_map = list(struct.unpack_from(f"<{num_sectors}B", self.image_data, offset))
            offset += num_sectors

            # Parse optional maps
            for flag, map_name, attr in [
                (track_info.has_cyl_map, "Cylinder", "sector_cyl_map"),
                (track_info.has_head_map, "Head", "sector_head_map")
            ]:
                if flag:
                    if offset + num_sectors > len(self.image_data):
                        raise ValueError(f"EOF in {map_name} map for C:{cyl} H:{track_info.head}")
                    map_data = struct.unpack_from(f"<{num_sectors}B", self.image_data, offset)
                    setattr(track_info, attr, {num: val & 1 if map_name == "Head" else val
                                              for num, val in zip(track_info.sector_num_map, map_data)})
                    offset += num_sectors

            if track_info.sector_size_code == 0xFF:
                if offset + num_sectors * 2 > len(self.image_data):
                    raise ValueError(f"EOF in size map for C:{cyl} H:{track_info.head}")
                track_info.sector_size_map = dict(zip(track_info.sector_num_map,
                                                     struct.unpack_from(f"<{num_sectors}H", self.image_data, offset)))
                offset += num_sectors * 2

            # Parse sector data
            for sector_num in track_info.sector_num_map:
                if offset >= len(self.image_data):
                    raise ValueError(f"EOF before sector type for C:{cyl} H:{track_info.head} S:{sector_num}")
                data_type = self.image_data[offset]
                data_offset = offset + 1
                sector_size = track_info.get_sector_size(sector_num)

                data_size = 0 if data_type == IMD_SECTOR_UNAVAILABLE else \
                           1 if data_type in (IMD_SECTOR_COMPRESSED, IMD_SECTOR_COMPRESSED_DEL,
                                             IMD_SECTOR_COMPRESSED_ERR, IMD_SECTOR_COMPRESSED_DEL_ERR) else \
                           sector_size if data_type in (IMD_SECTOR_NORMAL, IMD_SECTOR_NORMAL_DEL,
                                                       IMD_SECTOR_NORMAL_ERR, IMD_SECTOR_NORMAL_DEL_ERR) else None

                if data_size is None:
                    raise ValueError(f"Unknown sector type {data_type} for C:{cyl} H:{track_info.head} S:{sector_num}")

                if offset + 1 + data_size > len(self.image_data):
                    raise ValueError(f"EOF in sector data for C:{cyl} H:{track_info.head} S:{sector_num}")

                track_info.sector_data_info[sector_num] = (data_offset, data_type, data_size)
                offset += 1 + data_size

            self.tracks[(cyl, track_info.head)] = track_info
            parsed_track_count += 1

        logger.info(f"Parsed {parsed_track_count} tracks. Max Cyl={max_cyl}, Max Head={max_head}")
        if parsed_track_count:
            self._derive_physical_format(max_cyl, max_head)

    def _derive_physical_format(self, max_cyl_idx: int, max_head_idx: int):
        if not self.tracks:
            logger.warning("No tracks to derive format from")
            return

        ref_track_info = self.tracks.get((0, 0)) or self.tracks[min(self.tracks.keys())]
        rate_kbps, encoding, _ = IMD_MODE_MAP.get(ref_track_info.mode, (500, "MFM", ""))
        ref_spt = ref_track_info.num_sectors
        ref_sector_size = ref_track_info.get_sector_size(ref_track_info.sector_num_map[0])

        rpm = 300
        if rate_kbps == 500 and encoding == "MFM":
            rpm = 360 if ref_spt == 15 else 300
        elif encoding == "FM":
            rpm = 360

        try:
            track_format = TrackFormat(0, max_cyl_idx, 0, max_head_idx, ref_spt, encoding, rate_kbps, 84, 1)
            self.physical_format = PhysicalFormat(max_cyl_idx + 1, max_head_idx + 1, rpm, False, ref_sector_size, [track_format])
            logger.info(f"Derived format: Cyl={max_cyl_idx + 1}, Heads={max_head_idx + 1}, SPT={ref_spt}, BPS={ref_sector_size}")
        except Exception as e:
            logger.error(f"Failed to create PhysicalFormat: {e}")
            self.physical_format = None

    def _read_original_sector(self, cylinder: int, head: int, sector: int) -> bytes:
        track_info = self.tracks.get((cylinder, head))
        expected_size = self._get_sector_size(track_info, cylinder, head, sector)
        if not track_info or sector not in track_info.sector_data_info:
            return bytes(expected_size)

        offset, data_type, size = track_info.sector_data_info[sector]
        if data_type == IMD_SECTOR_UNAVAILABLE:
            return bytes(expected_size)
        elif data_type in (IMD_SECTOR_COMPRESSED, IMD_SECTOR_COMPRESSED_DEL, IMD_SECTOR_COMPRESSED_ERR, IMD_SECTOR_COMPRESSED_DEL_ERR):
            return bytes([self.image_data[offset]] * expected_size)
        elif data_type in (IMD_SECTOR_NORMAL, IMD_SECTOR_NORMAL_DEL, IMD_SECTOR_NORMAL_ERR, IMD_SECTOR_NORMAL_DEL_ERR):
            data = self.image_data[offset:offset + size]
            return data.ljust(expected_size, b'\0') if len(data) < expected_size else data[:expected_size]
        return bytes(expected_size)

    def _get_sector_size(self, track_info: Optional[IMDTrackInfo], cylinder: int, head: int, sector: int) -> int:
        cache_key = (cylinder, head, sector)
        if cache_key in self._sector_size_cache:
            return self._sector_size_cache[cache_key]

        size = self.physical_format.bytes_per_sector if self.physical_format else 512
        if track_info and sector in track_info.sector_data_info:
            size = track_info.get_sector_size(sector)
        self._sector_size_cache[cache_key] = size
        return size
