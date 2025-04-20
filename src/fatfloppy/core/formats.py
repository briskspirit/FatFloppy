# src/fatfloppy/core/formats.py
import struct
from dataclasses import dataclass
from typing import Optional

from .disk import DiskGeometry
from .physical_format import PhysicalFormat


@dataclass
class BootSectorData:
    oem_id: str = "MSDOS5.0"
    bytes_per_sector: int = 512
    sectors_per_cluster: int = 1
    reserved_sectors: int = 1
    num_fats: int = 2
    root_entries: int = 224
    total_sectors: int = 2880
    media_descriptor: int = 0xF0
    sectors_per_fat: int = 9
    sectors_per_track: int = 18
    num_heads: int = 2
    hidden_sectors: int = 0
    drive_number: int = 0
    volume_serial: int = 0
    volume_label: str = "NO NAME    "
    fs_type: str = "FAT12   "

    def to_bytes(self) -> bytes:
        boot_sector = bytearray(512)
        boot_sector[0:3] = b'\xEB\xFE\x90'
        boot_sector[3:11] = self.oem_id.encode('cp437').ljust(8)
        struct.pack_into('<H', boot_sector, 0x00B, self.bytes_per_sector)
        struct.pack_into('<B', boot_sector, 0x00D, self.sectors_per_cluster)
        struct.pack_into('<H', boot_sector, 0x00E, self.reserved_sectors)
        struct.pack_into('<B', boot_sector, 0x010, self.num_fats)
        struct.pack_into('<H', boot_sector, 0x011, self.root_entries)
        if self.total_sectors < 65536:
            struct.pack_into('<H', boot_sector, 0x013, self.total_sectors)
            struct.pack_into('<I', boot_sector, 0x020, 0)
        else:
            struct.pack_into('<H', boot_sector, 0x013, 0)
            struct.pack_into('<I', boot_sector, 0x020, self.total_sectors)
        struct.pack_into('<B', boot_sector, 0x015, self.media_descriptor)
        struct.pack_into('<H', boot_sector, 0x016, self.sectors_per_fat)
        struct.pack_into('<H', boot_sector, 0x018, self.sectors_per_track)
        struct.pack_into('<H', boot_sector, 0x01A, self.num_heads)
        struct.pack_into('<I', boot_sector, 0x01C, self.hidden_sectors)
        struct.pack_into('<B', boot_sector, 0x024, self.drive_number)
        struct.pack_into('<B', boot_sector, 0x025, 0)
        struct.pack_into('<B', boot_sector, 0x026, 0x29)
        struct.pack_into('<I', boot_sector, 0x027, self.volume_serial)
        boot_sector[0x02B:0x036] = self.volume_label.encode('cp437').ljust(11)
        boot_sector[0x036:0x03E] = self.fs_type.encode('cp437').ljust(8)
        struct.pack_into('<H', boot_sector, 0x1FE, 0xAA55)
        return bytes(boot_sector)

    @classmethod
    def from_bytes(cls, data: bytes) -> 'BootSectorData':
        if len(data) < 512:
            raise ValueError("Boot sector data too short")
        boot_sig = struct.unpack_from('<H', data, 0x1FE)[0]
        if boot_sig != 0xAA55:
            pass
        result = cls()
        try:
            result.oem_id = data[3:11].decode('cp437', errors='replace').strip()
            result.bytes_per_sector = struct.unpack_from('<H', data, 0x00B)[0]
            result.sectors_per_cluster = data[0x00D]
            result.reserved_sectors = struct.unpack_from('<H', data, 0x00E)[0]
            result.num_fats = data[0x010]
            result.root_entries = struct.unpack_from('<H', data, 0x011)[0]
            total_sectors_16 = struct.unpack_from('<H', data, 0x013)[0]
            total_sectors_32 = struct.unpack_from('<I', data, 0x020)[0]
            result.total_sectors = total_sectors_32 if total_sectors_16 == 0 else total_sectors_16
            result.media_descriptor = data[0x015]
            result.sectors_per_fat = struct.unpack_from('<H', data, 0x016)[0]
            result.sectors_per_track = struct.unpack_from('<H', data, 0x018)[0]
            result.num_heads = struct.unpack_from('<H', data, 0x01A)[0]
            result.hidden_sectors = struct.unpack_from('<I', data, 0x01C)[0]
            ext_sig = data[0x026] if len(data) > 0x26 else 0
            if ext_sig in [0x28, 0x29]:
                result.drive_number = data[0x024] if len(data) > 0x24 else 0
                result.volume_serial = struct.unpack_from('<I', data, 0x027)[0] if len(data) > 0x2A else 0
                result.volume_label = data[0x02B:0x036].decode('cp437', errors='replace').strip() if len(data) > 0x35 else "NO NAME"
                result.fs_type = data[0x036:0x03E].decode('cp437', errors='replace').strip() if len(data) > 0x3D else "FAT12"
            else:
                result.volume_label = "NO NAME"
                result.fs_type = "FAT12"
            if result.bytes_per_sector == 0 or result.sectors_per_track == 0 or result.num_heads == 0:
                raise ValueError(
                    f"Invalid geometry in BPB: BPS={result.bytes_per_sector}, "
                    f"SPT={result.sectors_per_track}, Heads={result.num_heads}"
                )
        except (struct.error, IndexError) as e:
            raise ValueError(f"Failed to parse boot sector BPB: {e}") from e
        return result


@dataclass
class FormatProfile:
    name: str
    description: str
    geometry: DiskGeometry
    physical_format: PhysicalFormat
    boot_sector: Optional[BootSectorData] = None
    media_descriptor: int = 0xF0

    @property
    def capacity_kb(self) -> float:
        return self.geometry.total_bytes / 1024
