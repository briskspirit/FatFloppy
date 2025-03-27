import math
import struct

FLOPPY_FORMATS = [
    {"size": "8\"",    "type": "SD", "heads": 1, "tracks": 77, "sectors": 26, "sector_size": 128, "total_sectors": 2002, "capacity": 256256,     "rpm": 360, "encoding": "FM",  "codec": None},
    {"size": "8\"",    "type": "SD", "heads": 2, "tracks": 77, "sectors": 26, "sector_size": 128, "total_sectors": 4004, "capacity": 512512,     "rpm": 360, "encoding": "FM",  "codec": None},
    {"size": "8\"",    "type": "DD", "heads": 1, "tracks": 77, "sectors": 8,  "sector_size": 1024, "total_sectors": 616, "capacity": 630784,     "rpm": 360, "encoding": "MFM", "codec": None},
    {"size": "8\"",    "type": "DD", "heads": 2, "tracks": 77, "sectors": 8,  "sector_size": 1024, "total_sectors": 1232, "capacity": 1261568,    "rpm": 360, "encoding": "MFM", "codec": None},
    {"size": "5.25\"", "type": "DD", "heads": 1, "tracks": 40, "sectors": 8,  "sector_size": 512, "total_sectors": 320, "capacity": 163840,     "rpm": 300, "encoding": "MFM", "codec": "ibm.160"},
    {"size": "5.25\"", "type": "DD", "heads": 2, "tracks": 40, "sectors": 8,  "sector_size": 512, "total_sectors": 640, "capacity": 327680,     "rpm": 300, "encoding": "MFM", "codec": "ibm.320"},
    {"size": "5.25\"", "type": "DD", "heads": 1, "tracks": 40, "sectors": 9,  "sector_size": 512, "total_sectors": 360, "capacity": 184320,     "rpm": 300, "encoding": "MFM", "codec": "ibm.180"},
    {"size": "5.25\"", "type": "DD", "heads": 2, "tracks": 40, "sectors": 9,  "sector_size": 512, "total_sectors": 720, "capacity": 368640,     "rpm": 300, "encoding": "MFM", "codec": "ibm.360"},
    {"size": "5.25\"", "type": "QD", "heads": 1, "tracks": 80, "sectors": 8,  "sector_size": 512, "total_sectors": 640, "capacity": 327680,     "rpm": 300, "encoding": "MFM", "codec": None},
    {"size": "5.25\"", "type": "QD", "heads": 2, "tracks": 80, "sectors": 8,  "sector_size": 512, "total_sectors": 1280, "capacity": 655360,     "rpm": 300, "encoding": "MFM", "codec": None},
    {"size": "5.25\"", "type": "HD", "heads": 2, "tracks": 80, "sectors": 15, "sector_size": 512, "total_sectors": 2400, "capacity": 1228800,    "rpm": 360, "encoding": "MFM", "codec": "ibm.1200"},
    {"size": "3.5\"",  "type": "DD", "heads": 1, "tracks": 80, "sectors": 8,  "sector_size": 512, "total_sectors": 640, "capacity": 327680,     "rpm": 300, "encoding": "MFM", "codec": None},
    {"size": "3.5\"",  "type": "DD", "heads": 1, "tracks": 80, "sectors": 9,  "sector_size": 512, "total_sectors": 720, "capacity": 368640,     "rpm": 300, "encoding": "MFM", "codec": None},
    {"size": "3.5\"",  "type": "DD", "heads": 2, "tracks": 80, "sectors": 8,  "sector_size": 512, "total_sectors": 1280, "capacity": 655360,     "rpm": 300, "encoding": "MFM", "codec": None},
    {"size": "3.5\"",  "type": "DD", "heads": 2, "tracks": 80, "sectors": 9,  "sector_size": 512, "total_sectors": 1440, "capacity": 737280,     "rpm": 300, "encoding": "MFM", "codec": "ibm.720"},
    {"size": "3.5\"",  "type": "HD", "heads": 2, "tracks": 80, "sectors": 18, "sector_size": 512, "total_sectors": 2880, "capacity": 1474560,    "rpm": 300, "encoding": "MFM", "codec": "ibm.1440"},
    {"size": "3.5\"",  "type": "HD", "heads": 2, "tracks": 80, "sectors": 21, "sector_size": 512, "total_sectors": 3360, "capacity": 1720320,    "rpm": 300, "encoding": "MFM", "codec": "ibm.1680"},
    {"size": "3.5\"",  "type": "HD", "heads": 2, "tracks": 82, "sectors": 21, "sector_size": 512, "total_sectors": 3444, "capacity": 1763328,    "rpm": 300, "encoding": "MFM", "codec": None},
    {"size": "3.5\"",  "type": "ED", "heads": 2, "tracks": 80, "sectors": 36, "sector_size": 512, "total_sectors": 5760, "capacity": 2949120,    "rpm": 300, "encoding": "MFM", "codec": "ibm.2880"},
]

MEDIA_DESCRIPTOR_MAP = {
    0xF0: "3.5\" HD 1.44 MB",
    0xF9: "3.5\" DD 720 KB",
    0xFD: "5.25\" DD 360 KB",
    0xFE: "5.25\" DD 160 KB",
    0xFF: "5.25\" DD 320 KB",
    0xFC: "5.25\" DD 180 KB",
    0xFB: "3.5\" DD 640 KB",
    0xFA: "5.25\" DD 120 KB",
    0xF8: "Fixed disk"
}

BOOT_SECTOR_FIELDS = [
    ('jump_code',          0x000, None, 3),
    ('oem_id',             0x003, 'str', 8),
    ('bytes_per_sector',   0x00B, '<H', 2),
    ('sectors_per_cluster',0x00D, '<B', 1),
    ('reserved_sectors',   0x00E, '<H', 2),
    ('num_fats',           0x010, '<B', 1),
    ('root_entries',       0x011, '<H', 2),
    ('total_sectors',      0x013, '<H', 2),
    ('media_descriptor',   0x015, '<B', 1),
    ('sectors_per_fat',    0x016, '<H', 2),
    ('sectors_per_track',  0x018, '<H', 2),
    ('num_heads',          0x01A, '<H', 2),
    ('hidden_sectors',     0x01C, '<I', 4),
    ('total_sectors_large',0x020, '<I', 4),
    ('drive_number',       0x024, '<B', 1),
    ('flags',              0x025, '<B', 1),
    ('signature_ext',      0x026, '<B', 1),
    ('volume_serial',      0x027, '<I', 4),
    ('volume_label',       0x02B, 'str', 11),
    ('fs_type',            0x036, 'str', 8),
    ('bootstrap_code',     0x03E, None, 448),
    ('signature',          0x1FE, '<H', 2),
]

class FloppyBPB:
    def __init__(self, disk_manager):
        self.disk_manager = disk_manager
        self.decode_boot_sector()

    def decode_boot_sector(self):
        boot_sector = self.disk_manager.read_bytes(0, 512)

        for name, offset, fmt, size in BOOT_SECTOR_FIELDS:
            if fmt == 'str':
                value = boot_sector[offset:offset+size].decode('cp437').strip()
            elif fmt is None:
                value = boot_sector[offset:offset+size]
            else:
                value = struct.unpack_from(fmt, boot_sector, offset)[0]

            setattr(self, name, value)

        if self.total_sectors == 0:
            self.total_sectors = self.total_sectors_large

        if self.signature != 0xAA55:
            raise ValueError("Invalid boot sector signature")

    @classmethod
    def create_boot_sector(cls, params):
        boot_sector = bytearray(512)
        boot_sector[0:3] = b'\xEB\xFE\x90'

        for name, offset, fmt, size in BOOT_SECTOR_FIELDS:
            if name == 'jump_code':
                continue

            if name == 'signature':
                value = 0xAA55
            elif name in params:
                value = params[name]
            elif name == 'bootstrap_code':
                value = bytes(size)
            else:
                continue

            if fmt == 'str':
                field_data = value.encode('cp437').ljust(size).upper()
                boot_sector[offset:offset+size] = field_data
            elif fmt is None and isinstance(value, bytes):
                boot_sector[offset:offset+len(value)] = value
            elif fmt is not None:
                struct.pack_into(fmt, boot_sector, offset, value)

        total_sectors = params.get('total_sectors', 0)
        if total_sectors < 65536:
            struct.pack_into('<H', boot_sector, 0x013, total_sectors)
            struct.pack_into('<I', boot_sector, 0x020, 0)
        else:
            struct.pack_into('<H', boot_sector, 0x013, 0)
            struct.pack_into('<I', boot_sector, 0x020, total_sectors)

        return boot_sector

    @classmethod
    def calculate_sectors_per_fat(cls, total_sectors, reserved_sectors, num_fats, root_dir_sectors,
                                 sectors_per_cluster, sector_size):
        sectors_per_fat = 1
        while True:
            data_sectors = total_sectors - reserved_sectors - (num_fats * sectors_per_fat) - root_dir_sectors
            if data_sectors <= 0:
                raise ValueError("Invalid parameters: not enough sectors for data")
            num_clusters = data_sectors // sectors_per_cluster
            fat_size_bytes = math.ceil(num_clusters * 1.5)
            required_sectors = math.ceil(fat_size_bytes / sector_size)
            if required_sectors <= sectors_per_fat:
                return sectors_per_fat
            sectors_per_fat += 1

    @classmethod
    def match_disk_by_geometry(cls, geometry):
        for fmt in FLOPPY_FORMATS:
            if (geometry['total_sectors'] == fmt["total_sectors"] and
                geometry['sectors_per_track'] == fmt["sectors"] and
                geometry['num_heads'] == fmt["heads"] and
                geometry['bytes_per_sector'] == fmt["sector_size"]):
                capacity_mb = fmt['capacity'] / (1024 * 1024)
                return f"{fmt['size']} {fmt['type']} {capacity_mb:.2f} MB"

        return f"Unknown (Sectors: {geometry['total_sectors']})"

    @classmethod
    def from_parameters(cls, sector_size, sectors_per_track, num_tracks, num_heads, num_fats,
                        root_entries, sectors_per_cluster, media_descriptor=0xF0, reserved_sectors=1,
                        oem_id="MSDOS5.0", disk_manager=None):
        if disk_manager is None:
            raise ValueError("A disk_manager is required when creating from parameters")

        total_sectors = num_tracks * sectors_per_track * num_heads
        root_dir_sectors = (root_entries * 32 + sector_size - 1) // sector_size

        sectors_per_fat = cls.calculate_sectors_per_fat(
            total_sectors, reserved_sectors, num_fats,
            root_dir_sectors, sectors_per_cluster, sector_size
        )

        params = {
            'bytes_per_sector': sector_size,
            'sectors_per_cluster': sectors_per_cluster,
            'reserved_sectors': reserved_sectors,
            'num_fats': num_fats,
            'root_entries': root_entries,
            'total_sectors': total_sectors,
            'media_descriptor': media_descriptor,
            'sectors_per_fat': sectors_per_fat,
            'sectors_per_track': sectors_per_track,
            'num_heads': num_heads,
            'oem_id': oem_id,
            'volume_label': 'NO NAME',
            'fs_type': 'FAT12'
        }

        boot_sector = cls.create_boot_sector(params)
        disk_manager.write_bytes(0, boot_sector)

        return cls(disk_manager)

    def get_fat12_params(self):
        return {
            'bytes_per_sector': self.bytes_per_sector,
            'sectors_per_cluster': self.sectors_per_cluster,
            'reserved_sectors': self.reserved_sectors,
            'num_fats': self.num_fats,
            'root_entries': self.root_entries,
            'total_sectors': self.total_sectors,
            'sectors_per_fat': self.sectors_per_fat,
            'media_descriptor': self.media_descriptor
        }

    def get_disk_type(self):
        if self.media_descriptor in MEDIA_DESCRIPTOR_MAP:
            return MEDIA_DESCRIPTOR_MAP[self.media_descriptor]

        geometry = {
            'total_sectors': self.total_sectors,
            'sectors_per_track': self.sectors_per_track,
            'num_heads': self.num_heads,
            'bytes_per_sector': self.bytes_per_sector
        }

        disk_type = self.match_disk_by_geometry(geometry)

        if disk_type.startswith("Unknown"):
            return f"Unknown (Media Descriptor: 0x{self.media_descriptor:02X}, Sectors: {self.total_sectors})"

        return disk_type
