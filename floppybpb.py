import math
import struct

# Floppy disk format definitions based on standard geometries
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


class FloppyBPB:
    def __init__(self, disk_manager):
        """
        Initialize FloppyBPB with a DiskManager instance.

        Args:
            disk_manager (DiskManager): The disk access manager.

        Raises:
            ValueError: If boot sector signature is invalid.
        """
        self.disk_manager = disk_manager
        self.decode_boot_sector()

    def decode_boot_sector(self):
        """
        Decode the BPB from the boot sector using the disk manager.
        Stores fields as instance attributes.
        """
        boot_sector = self.disk_manager.read_bytes(0, 512)
        self.jump_code = boot_sector[0x000:0x003]
        self.oem_id = boot_sector[0x003:0x00B].decode('cp437').strip()
        self.bytes_per_sector = struct.unpack_from('<H', boot_sector, 0x00B)[0]
        self.sectors_per_cluster = struct.unpack_from('<B', boot_sector, 0x00D)[0]
        self.reserved_sectors = struct.unpack_from('<H', boot_sector, 0x00E)[0]
        self.num_fats = struct.unpack_from('<B', boot_sector, 0x010)[0]
        self.root_entries = struct.unpack_from('<H', boot_sector, 0x011)[0]
        self.total_sectors = struct.unpack_from('<H', boot_sector, 0x013)[0]
        self.media_descriptor = struct.unpack_from('<B', boot_sector, 0x015)[0]
        self.sectors_per_fat = struct.unpack_from('<H', boot_sector, 0x016)[0]
        self.sectors_per_track = struct.unpack_from('<H', boot_sector, 0x018)[0]
        self.num_heads = struct.unpack_from('<H', boot_sector, 0x01A)[0]
        self.hidden_sectors = struct.unpack_from('<I', boot_sector, 0x01C)[0]
        self.total_sectors_large = struct.unpack_from('<I', boot_sector, 0x020)[0]
        self.drive_number = struct.unpack_from('<B', boot_sector, 0x024)[0]
        self.flags = struct.unpack_from('<B', boot_sector, 0x025)[0]
        self.signature_ext = struct.unpack_from('<B', boot_sector, 0x026)[0]
        self.volume_serial = struct.unpack_from('<I', boot_sector, 0x027)[0]
        self.volume_label = boot_sector[0x02B:0x036].decode('cp437').strip()
        self.fs_type = boot_sector[0x036:0x03E].decode('cp437').strip()
        if self.total_sectors == 0:
            self.total_sectors = self.total_sectors_large
        self.bootstrap_code = boot_sector[0x03E:0x1FE]
        self.signature = struct.unpack_from('<H', boot_sector, 0x1FE)[0]
        if self.signature != 0xAA55:
            raise ValueError("Invalid boot sector signature")
        # Set geometry for FloppyDiskManager
        # if isinstance(self.disk_manager, FloppyDiskManager):
            # num_cylinders = (self.total_sectors // self.sectors_per_track) // self.num_heads
            # self.disk_manager.set_geometry(self.sectors_per_track, self.num_heads, num_cylinders, self.bytes_per_sector)

    @staticmethod
    def calculate_sectors_per_fat(total_sectors, reserved_sectors, num_fats, root_dir_sectors,
                                 sectors_per_cluster, sector_size):
        """
        Calculate the number of sectors per FAT for FAT12.
        """
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
    def from_parameters(cls, sector_size, sectors_per_track, num_tracks, num_heads, num_fats,
                        root_entries, sectors_per_cluster, media_descriptor=0xF0, reserved_sectors=1,
                        oem_id="MSDOS5.0", disk_manager=None):
        """
        Create a new FloppyBPB instance from specified parameters, optionally using a provided disk manager.

        Args:
            sector_size (int): Bytes per sector (e.g., 512).
            sectors_per_track (int): Sectors per track.
            num_tracks (int): Number of tracks.
            num_heads (int): Number of heads (e.g., 2 for double-sided).
            num_fats (int): Number of FAT copies (typically 2).
            root_entries (int): Number of root directory entries (e.g., 224).
            sectors_per_cluster (int): Sectors per cluster (e.g., 1).
            media_descriptor (int): Media descriptor byte (default 0xF0).
            reserved_sectors (int): Reserved sectors (default 1).
            oem_id (str): OEM identifier (default "MSDOS5.0").
            disk_manager (DiskManager, optional): An instance of DiskManager to use. If None, a MemoryDiskManager will be created.

        Returns:
            FloppyBPB: A new instance with the disk image managed by the provided or default disk manager.
        """
        # Calculate total sectors and other derived parameters
        total_sectors = num_tracks * sectors_per_track * num_heads
        root_dir_sectors = (root_entries * 32 + sector_size - 1) // sector_size
        # This assumes a helper method to calculate sectors per FAT; adjust as needed
        sectors_per_fat = cls.calculate_sectors_per_fat(total_sectors, reserved_sectors, num_fats,
                                                        root_dir_sectors, sectors_per_cluster, sector_size)

        # Create the boot sector
        boot_sector = bytearray(512)
        boot_sector[0:3] = b'\xEB\xFE\x90'  # Jump instruction
        boot_sector[3:11] = oem_id.encode('cp437').ljust(8)  # OEM ID
        struct.pack_into('<H', boot_sector, 0x00B, sector_size)
        struct.pack_into('<B', boot_sector, 0x00D, sectors_per_cluster)
        struct.pack_into('<H', boot_sector, 0x00E, reserved_sectors)
        struct.pack_into('<B', boot_sector, 0x010, num_fats)
        struct.pack_into('<H', boot_sector, 0x011, root_entries)
        if total_sectors < 65536:
            struct.pack_into('<H', boot_sector, 0x013, total_sectors)
            struct.pack_into('<I', boot_sector, 0x020, 0)
        else:
            struct.pack_into('<H', boot_sector, 0x013, 0)
            struct.pack_into('<I', boot_sector, 0x020, total_sectors)
        struct.pack_into('<B', boot_sector, 0x015, media_descriptor)
        struct.pack_into('<H', boot_sector, 0x016, sectors_per_fat)
        struct.pack_into('<H', boot_sector, 0x018, sectors_per_track)
        struct.pack_into('<H', boot_sector, 0x01A, num_heads)
        struct.pack_into('<I', boot_sector, 0x01C, 0)  # Hidden sectors
        struct.pack_into('<B', boot_sector, 0x024, 0)  # Drive number
        struct.pack_into('<B', boot_sector, 0x025, 0)  # Flags
        struct.pack_into('<B', boot_sector, 0x026, 0x29)  # Extended boot signature
        struct.pack_into('<I', boot_sector, 0x027, 0)  # Volume serial number
        boot_sector[0x02B:0x036] = 'NO NAME    '.encode('cp437')  # Volume label
        boot_sector[0x036:0x03E] = 'FAT12   '.encode('cp437')    # Filesystem type
        struct.pack_into('<H', boot_sector, 0x1FE, 0xAA55)  # Boot signature

        # Handle the disk manager
        if disk_manager is None:
            # Default to MemoryDiskManager if none provided
            image_size = total_sectors * sector_size
            image_data = bytearray(image_size)
            image_data[0:512] = boot_sector
            # TODO: fix this, do we need default manager?
            # disk_manager = MemoryDiskManager(image_data)
        else:
            # Use the provided disk manager and write the boot sector
            disk_manager.write_bytes(0, boot_sector)

        # Return the FloppyBPB instance with the disk manager
        return cls(disk_manager)

    def get_fat12_params(self):
        """Return BPB parameters for FAT12FileSystem."""
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
        """
        Return disk type based on media descriptor and geometry.
        First tries to match by media descriptor, then by geometry if that fails.
        """
        # Media descriptor mapping for quick identification
        media_descriptor_map = {
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

        # First try to identify by media descriptor
        if self.media_descriptor in media_descriptor_map:
            return media_descriptor_map[self.media_descriptor]

        # If media descriptor doesn't match, try to identify by geometry
        for fmt in FLOPPY_FORMATS:
            if (self.total_sectors == fmt["total_sectors"] and
                self.sectors_per_track == fmt["sectors"] and
                self.num_heads == fmt["heads"] and
                self.bytes_per_sector == fmt["sector_size"]):
                capacity_mb = fmt['capacity'] / (1024 * 1024)
                return f"{fmt['size']} {fmt['type']} {capacity_mb:.2f} MB"

        # If no match found
        return f"Unknown (Media Descriptor: 0x{self.media_descriptor:02X}, Sectors: {self.total_sectors})"
