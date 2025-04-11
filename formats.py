# formats.py

from dataclasses import dataclass
import struct
from typing import Dict, List, Optional, Union, Tuple

from disk import Disk, DiskGeometry
from drivers import PhysicalFormat

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
        boot_sector[0:3] = b'\xEB\xFE\x90'  # Jump instruction
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
        struct.pack_into('<B', boot_sector, 0x025, 0)  # Flags
        struct.pack_into('<B', boot_sector, 0x026, 0x29)  # Extended boot signature
        struct.pack_into('<I', boot_sector, 0x027, self.volume_serial)
        boot_sector[0x02B:0x036] = self.volume_label.encode('cp437').ljust(11)
        boot_sector[0x036:0x03E] = self.fs_type.encode('cp437').ljust(8)
        struct.pack_into('<H', boot_sector, 0x1FE, 0xAA55)  # Boot signature

        return bytes(boot_sector)

    @classmethod
    def from_bytes(cls, data: bytes) -> 'BootSectorData':
        if len(data) < 512:
            raise ValueError("Boot sector data too short")

        boot_sig = struct.unpack_from('<H', data, 0x1FE)[0]
        if boot_sig != 0xAA55:
            raise ValueError("Invalid boot signature")

        result = cls()
        result.oem_id = data[3:11].decode('cp437').strip()
        result.bytes_per_sector = struct.unpack_from('<H', data, 0x00B)[0]
        result.sectors_per_cluster = data[0x00D]
        result.reserved_sectors = struct.unpack_from('<H', data, 0x00E)[0]
        result.num_fats = data[0x010]
        result.root_entries = struct.unpack_from('<H', data, 0x011)[0]
        result.total_sectors = struct.unpack_from('<H', data, 0x013)[0]
        if result.total_sectors == 0:
            result.total_sectors = struct.unpack_from('<I', data, 0x020)[0]
        result.media_descriptor = data[0x015]
        result.sectors_per_fat = struct.unpack_from('<H', data, 0x016)[0]
        result.sectors_per_track = struct.unpack_from('<H', data, 0x018)[0]
        result.num_heads = struct.unpack_from('<H', data, 0x01A)[0]
        result.hidden_sectors = struct.unpack_from('<I', data, 0x01C)[0]
        result.drive_number = data[0x024]
        result.volume_serial = struct.unpack_from('<I', data, 0x027)[0]
        result.volume_label = data[0x02B:0x036].decode('cp437').strip()
        result.fs_type = data[0x036:0x03E].decode('cp437').strip()

        return result

@dataclass
class FormatProfile:
    name: str
    description: str
    geometry: DiskGeometry
    physical_format: PhysicalFormat
    boot_sector: BootSectorData = None
    media_descriptor: int = 0xF0

    @property
    def capacity_kb(self) -> float:
        return self.geometry.total_bytes / 1024

class FormatManager:
    def __init__(self):
        self.known_formats: Dict[str, FormatProfile] = {}
        self._initialize_known_formats()

    def _initialize_known_formats(self):
        # 360K 5.25" DD
        self.known_formats["ibm_5.25_360k"] = FormatProfile(
            name="ibm_5.25_360k",
            description="5.25\" DD 360KB (40 tracks, 2 heads, 9 sectors)",
            geometry=DiskGeometry(
                cylinders=40,
                heads=2,
                sectors_per_track=9,
                sector_size=512
            ),
            physical_format=PhysicalFormat(
                encoding="MFM",
                rate=250,
                rpm=300,
                gap3=84
            ),
            media_descriptor=0xFD,
            boot_sector=BootSectorData(
                sectors_per_track=9,
                num_heads=2,
                total_sectors=720,
                media_descriptor=0xFD,
                root_entries=112,
                sectors_per_fat=2,
                sectors_per_cluster=2
            )
        )

        # 1.2M 5.25" HD
        self.known_formats["ibm_5.25_1.2m"] = FormatProfile(
            name="ibm_5.25_1.2m",
            description="5.25\" HD 1.2MB (80 tracks, 2 heads, 15 sectors)",
            geometry=DiskGeometry(
                cylinders=80,
                heads=2,
                sectors_per_track=15,
                sector_size=512
            ),
            physical_format=PhysicalFormat(
                encoding="MFM",
                rate=500,
                rpm=360,
                gap3=84
            ),
            media_descriptor=0xF9,
            boot_sector=BootSectorData(
                sectors_per_track=15,
                num_heads=2,
                total_sectors=2400,
                media_descriptor=0xF9,
                root_entries=224,
                sectors_per_fat=7,
                sectors_per_cluster=1
            )
        )

        # 720K 3.5" DD
        self.known_formats["ibm_3.5_720k"] = FormatProfile(
            name="ibm_3.5_720k",
            description="3.5\" DD 720KB (80 tracks, 2 heads, 9 sectors)",
            geometry=DiskGeometry(
                cylinders=80,
                heads=2,
                sectors_per_track=9,
                sector_size=512
            ),
            physical_format=PhysicalFormat(
                encoding="MFM",
                rate=250,
                rpm=300,
                gap3=84
            ),
            media_descriptor=0xF9,
            boot_sector=BootSectorData(
                sectors_per_track=9,
                num_heads=2,
                total_sectors=1440,
                media_descriptor=0xF9,
                root_entries=112,
                sectors_per_fat=3,
                sectors_per_cluster=2
            )
        )

        # 1.44M 3.5" HD
        self.known_formats["ibm_3.5_1.44m"] = FormatProfile(
            name="ibm_3.5_1.44m",
            description="3.5\" HD 1.44MB (80 tracks, 2 heads, 18 sectors)",
            geometry=DiskGeometry(
                cylinders=80,
                heads=2,
                sectors_per_track=18,
                sector_size=512
            ),
            physical_format=PhysicalFormat(
                encoding="MFM",
                rate=500,
                rpm=300,
                gap3=84
            ),
            media_descriptor=0xF0,
            boot_sector=BootSectorData(
                sectors_per_track=18,
                num_heads=2,
                total_sectors=2880,
                media_descriptor=0xF0,
                root_entries=224,
                sectors_per_fat=9,
                sectors_per_cluster=1
            )
        )

    def detect_format(self, disk: Disk) -> Optional[str]:
        try:
            # Try direct read if driver supports it
            if hasattr(disk.driver, 'read_bytes_direct'):
                boot_sector = disk.driver.read_bytes_direct(0, 512)
            else:
                # Fall back to regular sector read
                boot_sector = disk.read_sector(0, 0, 1)

            boot_data = BootSectorData.from_bytes(boot_sector)

            for format_name, profile in self.known_formats.items():
                if (profile.boot_sector and
                    profile.boot_sector.sectors_per_track == boot_data.sectors_per_track and
                    profile.boot_sector.num_heads == boot_data.num_heads and
                    profile.boot_sector.total_sectors == boot_data.total_sectors and
                    profile.boot_sector.media_descriptor == boot_data.media_descriptor):
                    return format_name

            return None
        except Exception as e:
            print(f"Format detection error: {e}")
            return None

    def list_known_formats(self) -> List[Tuple[str, str]]:
        return [(name, profile.description) for name, profile in self.known_formats.items()]

    def get_format_by_name(self, name: str) -> Optional[FormatProfile]:
        return self.known_formats.get(name)
