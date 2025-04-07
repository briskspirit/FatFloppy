import datetime
import math
import struct

from floppy_formats import FLOPPY_FORMATS


class FATBPB:
    """Unified BIOS Parameter Block parser for FAT file systems"""

    BOOT_SECTOR_FIELDS = [
        ('jump_code',          0x000, None, 3),
        ('oem_id',             0x003, 'str', 8),
        # DOS 2.0
        ('bytes_per_sector',   0x00B, '<H', 2),
        ('sectors_per_cluster',0x00D, '<B', 1),
        ('reserved_sectors',   0x00E, '<H', 2),
        ('num_fats',           0x010, '<B', 1),
        ('root_entries',       0x011, '<H', 2),
        ('total_sectors',      0x013, '<H', 2),
        ('media_descriptor',   0x015, '<B', 1),
        ('sectors_per_fat',    0x016, '<H', 2),
        # DOS 3.31
        ('sectors_per_track',  0x018, '<H', 2),
        ('num_heads',          0x01A, '<H', 2),
        ('hidden_sectors',     0x01C, '<I', 4),
        ('total_sectors_large',0x020, '<I', 4),
        # DOS 4.0
        ('drive_number',       0x024, '<B', 1),
        ('flags',              0x025, '<B', 1),
        ('signature_ext',      0x026, '<B', 1),
        ('volume_serial',      0x027, '<I', 4),
        ('volume_label',       0x02B, 'str', 11),
        ('fs_type',            0x036, 'str', 8),
        ('bootstrap_code',     0x03E, None, 448),
        ('signature',          0x1FE, '<H', 2),
    ]

    VALID_BOOT_SIGNATURE = 0xAA55

    def __init__(self, read_bytes_func):
        self.read_bytes = read_bytes_func
        self.decode_boot_sector()
        if not self.is_valid():
            print("FATBPB; Warning: BPB invalid or missing; inferring parameters.")
            self.infer_parameters()

    def decode_boot_sector(self):
        boot_sector = self.read_bytes(0, 512)

        for name, offset, fmt, size in self.BOOT_SECTOR_FIELDS:
            if fmt == 'str':
                value = boot_sector[offset:offset+size].decode('cp437').strip()
            elif fmt is None:
                value = boot_sector[offset:offset+size]
            else:
                value = struct.unpack_from(fmt, boot_sector, offset)[0]

            setattr(self, name, value)

        if self.total_sectors == 0:
            self.total_sectors = self.total_sectors_large

    def infer_parameters(self):
        image_size = self.guess_image_size()
        if not image_size:
            self.set_default_geometry()
            return

        for fmt in FLOPPY_FORMATS:
            expected_size = fmt['total_sectors'] * fmt['sector_size']
            if image_size == expected_size:
                self.bytes_per_sector = fmt['sector_size']
                self.sectors_per_track = fmt['sectors']
                self.num_heads = fmt['heads']
                self.total_sectors = fmt['total_sectors']
                self.num_cylinders = fmt['tracks']
                self.root_entries = fmt['root_directory']
                self.reserved_sectors = 1
                self.num_fats = 2
                self.hidden_sectors = 0
                self.media_descriptor = fmt['mdb']
                # Use sectors_per_fat from format if available, otherwise detect it
                self.detect_fat_params_for_sectors_per_fat()
                self.detect_sectors_per_cluster_and_fat()
                if self.media_descriptor != fmt['mdb']:
                    continue
                print(f"FATBPB; Inferred geometry: {self.sectors_per_track} sectors/track, "
                      f"{self.num_heads} heads, {self.num_cylinders} cylinders, "
                      f"{self.bytes_per_sector} bytes/sector")
                return

    def guess_image_size(self):
        chunk_size = 512
        total_read = 0
        try:
            while True:
                data = self.read_bytes(total_read, chunk_size)
                if not data:
                    break
                total_read += len(data)
                if len(data) < chunk_size:
                    break
            return total_read
        except Exception:
            return None

    def detect_sectors_per_cluster_and_fat(self):
        root_dir_sectors = math.ceil((self.root_entries * 32) / self.bytes_per_sector)
        data_start_sector = self.reserved_sectors + (self.num_fats * self.sectors_per_fat) + root_dir_sectors
        data_sectors = self.total_sectors - data_start_sector

        possible_sc = []
        for sc in [1, 2, 4, 8, 16]:
            if data_sectors % sc != 0:
                continue
            num_clusters = data_sectors // sc
            if num_clusters < 2:
                continue
            fat_bytes_needed = math.ceil((num_clusters + 2) * 1.5)
            fat_sectors_needed = math.ceil(fat_bytes_needed / self.bytes_per_sector)
            if fat_sectors_needed <= self.sectors_per_fat:
                possible_sc.append(sc)

        if not possible_sc:
            print("No suitable sectors_per_cluster found; defaulting to 1.")
            self.sectors_per_cluster = 1
        else:
            self.sectors_per_cluster = min(possible_sc)  # Prefer smallest valid value

        num_clusters = data_sectors // self.sectors_per_cluster
        fat_bytes_needed = math.ceil((num_clusters + 2) * 1.5)
        self.sectors_per_fat = math.ceil(fat_bytes_needed / self.bytes_per_sector)

    def detect_fat_params_for_sectors_per_fat(self):
        fat_offsets = []
        for sector in range(min(20, self.total_sectors)):
            offset = sector * self.bytes_per_sector
            data = self.read_bytes(offset, 3)
            if len(data) < 3:
                break
            mdb = data[0]
            if (mdb & 0xF0) == 0xF0 and data == bytes([mdb, 0xFF, 0xFF]):
                fat_offsets.append(offset)
                if len(fat_offsets) >= 2:
                    break

        if len(fat_offsets) >= 2:
            self.sectors_per_fat = (fat_offsets[1] - fat_offsets[0]) // self.bytes_per_sector
            self.reserved_sectors = fat_offsets[0] // self.bytes_per_sector
            self.num_fats = 2
            self.media_descriptor = mdb
        elif fat_offsets:
            self.sectors_per_fat = 2  # Default guess if only one FAT detected
            self.reserved_sectors = fat_offsets[0] // self.bytes_per_sector
            self.num_fats = 1
            self.media_descriptor = mdb
        else:
            self.sectors_per_fat = 2  # Fallback default

    def set_default_geometry(self):
        self.bytes_per_sector = 512
        self.sectors_per_track = 18
        self.num_heads = 2
        self.num_cylinders = 80
        self.total_sectors = self.sectors_per_track * self.num_heads * self.num_cylinders
        self.sectors_per_cluster = 1
        self.reserved_sectors = 1
        self.num_fats = 2
        self.media_descriptor = 0xF0
        self.root_entries = 112
        self.sectors_per_fat = self.calculate_sectors_per_fat(
            self.total_sectors, self.reserved_sectors, self.num_fats,
            (self.root_entries * 32 + self.bytes_per_sector - 1) // self.bytes_per_sector,
            self.sectors_per_cluster, self.bytes_per_sector
        )

    def get_fat_type(self):
        root_dir_sectors = (self.root_entries * 32 + self.bytes_per_sector - 1) // self.bytes_per_sector
        fat_sectors = self.num_fats * self.sectors_per_fat
        data_sectors = self.total_sectors - (self.reserved_sectors + fat_sectors + root_dir_sectors)
        total_clusters = data_sectors // self.sectors_per_cluster

        if total_clusters < 4085:
            return "FAT12"
        elif total_clusters < 65525:
            return "FAT16"
        else:
            return "FAT32"

    def is_valid(self):
        valid_media_descriptors = {0xF0, 0xF8, 0xF9, 0xFA, 0xFB, 0xFC, 0xFD, 0xFE, 0xFF}
        if self.media_descriptor not in valid_media_descriptors:
            return False

        valid_bytes_per_sector = {128, 256, 512, 1024, 2048, 4096}
        if self.bytes_per_sector not in valid_bytes_per_sector:
            return False

        valid_sectors_per_cluster = {1, 2, 4, 8, 16, 32, 64, 128}
        if self.sectors_per_cluster not in valid_sectors_per_cluster:
            return False

        if not (0 < self.total_sectors <= 5760):
            return False

        if self.num_heads not in {1, 2}:
            return False

        if self.sectors_per_track is not None and not (8 <= self.sectors_per_track <= 36):
            print(f"Warning: sectors_per_track ({self.sectors_per_track}) is unusual but not invalidating BPB")
        return True

    def is_boot_signature_valid(self):
        return self.signature == self.VALID_BOOT_SIGNATURE

    def get_params(self):
        return {
            'bytes_per_sector': self.bytes_per_sector,
            'sectors_per_cluster': self.sectors_per_cluster,
            'reserved_sectors': self.reserved_sectors,
            'num_fats': self.num_fats,
            'root_entries': self.root_entries,
            'total_sectors': self.total_sectors,
            'sectors_per_fat': self.sectors_per_fat,
            'media_descriptor': self.media_descriptor,
            'sectors_per_track': self.sectors_per_track,
            'num_heads': self.num_heads,
            'hidden_sectors': self.hidden_sectors,
        }

    def get_disk_type(self):
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

    @classmethod
    def match_disk_by_geometry(cls, geometry):
        for fmt in FLOPPY_FORMATS:
            if (geometry['total_sectors'] == fmt["total_sectors"] and
                geometry['sectors_per_track'] == fmt["sectors"] and
                geometry['num_heads'] == fmt["heads"] and
                geometry['bytes_per_sector'] == fmt["sector_size"]):
                capacity_mb = fmt['capacity'] / 1024
                return f"{fmt['size']} {fmt['type']} {capacity_mb:.2f} KB"
        return f"Unknown (Sectors: {geometry['total_sectors']})"

    @classmethod
    def create_boot_sector(cls, params):
        boot_sector = bytearray(512)
        boot_sector[0:3] = b'\xEB\xFE\x90'

        for name, offset, fmt, size in cls.BOOT_SECTOR_FIELDS:
            if name == 'jump_code':
                continue
            if name == 'signature':
                value = cls.VALID_BOOT_SIGNATURE
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

        return cls(disk_manager.read_bytes)

class FileSystemFactory:
    @staticmethod
    def create_filesystem(read_bytes_func, write_bytes_func=None, flush_func=None):
        """Create the appropriate file system instance based on BPB analysis."""
        bpb = FATBPB(read_bytes_func)
        fat_type = bpb.get_fat_type()
        if fat_type == "FAT12":
            return FAT12FileSystem(read_bytes_func, write_bytes_func, flush_func)
        elif fat_type == "FAT16":
            # For future implementation
            raise ValueError("FAT16 file system is not implemented yet")
        else:
            raise ValueError(f"Unsupported file system type: {fat_type}")

class FAT12FileSystem:
    def __init__(self, read_bytes_func, write_bytes_func, flush_func):
        self.read_bytes = read_bytes_func
        self.write_bytes = write_bytes_func
        self.flush_func = flush_func
        self.bpb = FATBPB(read_bytes_func)
        self.params = self.bpb.get_params()
        self.sector_size = self.params['bytes_per_sector']
        self.total_sectors = self.params['total_sectors']
        self.reserved_sectors = self.params['reserved_sectors']
        self.num_fats = self.params['num_fats']
        self.sectors_per_fat = self.params['sectors_per_fat']
        self.root_entries = self.params['root_entries']
        self.root_dir_sectors = (self.root_entries * 32 + self.sector_size - 1) // self.sector_size
        self.fat_start = self.reserved_sectors * self.sector_size
        self.root_dir_start = self.fat_start + (self.num_fats * self.sectors_per_fat * self.sector_size)
        self.data_area_start = self.root_dir_start + (self.root_dir_sectors * self.sector_size)
        self.num_clusters = (self.total_sectors - (self.reserved_sectors + self.num_fats * self.sectors_per_fat + self.root_dir_sectors)) // self.params['sectors_per_cluster']
        self.cluster_size = self.params['sectors_per_cluster'] * self.sector_size

        print(self.check_filesystem_integrity())

    def get_bpb_info(self):
        """Provide BPB information for GUI display."""
        return self.bpb.get_params()

    def get_disk_type(self):
        """Delegate to BPB for disk type identification."""
        return self.bpb.get_disk_type()

    def initialize_fats(self):
        media_descriptor = self.params['media_descriptor']
        for fat in range(self.num_fats):
            offset = self.fat_start + (fat * self.sectors_per_fat * self.sector_size)
            self.write_bytes(offset, bytes([media_descriptor, 0xFF, 0xFF]))

    @staticmethod
    def fat_time(dt):
        second = dt.second // 2
        minute = dt.minute
        hour = dt.hour
        return (hour << 11) | (minute << 5) | second

    @staticmethod
    def fat_date(dt):
        day = dt.day
        month = dt.month
        year = dt.year - 1980
        return (year << 9) | (month << 5) | day

    @staticmethod
    def fat_time_to_datetime(fat_time, fat_date):
        second = (fat_time & 0x1F) * 2
        minute = (fat_time >> 5) & 0x3F
        hour = (fat_time >> 11) & 0x1F
        day = fat_date & 0x1F
        month = (fat_date >> 5) & 0x0F
        year = 1980 + ((fat_date >> 9) & 0x7F)
        try:
            return datetime.datetime(year, month, day, hour, minute, second)
        except ValueError:
            return datetime.datetime(1980, 1, 1, 0, 0, 0)

    @staticmethod
    def get_attributes(attr_byte):
        attributes = []
        if attr_byte & 0x01: attributes.append("RO")
        if attr_byte & 0x02: attributes.append("H")
        if attr_byte & 0x04: attributes.append("S")
        if attr_byte & 0x20: attributes.append("A")
        return " ".join(attributes) if attributes else "-"

    def read_fat_entry(self, cluster):
        offset = self.fat_start + int(cluster * 1.5)
        value_bytes = self.read_bytes(offset, 2)
        value = struct.unpack('<H', value_bytes)[0]
        if cluster % 2 == 0:
            return value & 0x0FFF
        else:
            return (value >> 4) & 0x0FFF

    def set_fat_entry(self, cluster, value):
        value &= 0x0FFF
        for fat in range(self.num_fats):
            offset = self.fat_start + (fat * self.sectors_per_fat * self.sector_size) + int(cluster * 1.5)
            current_bytes = self.read_bytes(offset, 2)
            current = struct.unpack('<H', current_bytes)[0]
            if cluster % 2 == 0:
                new_value = (current & 0xF000) | (value & 0x0FFF)
            else:
                new_value = (current & 0x000F) | ((value & 0x0FFF) << 4)
            new_bytes = struct.pack('<H', new_value)
            self.write_bytes(offset, new_bytes)

    def get_cluster_chain(self, start_cluster):
        chain = []
        cluster = start_cluster
        while cluster < 0xFF8 and cluster >= 2:
            chain.append(cluster)
            next_cluster = self.read_fat_entry(cluster)
            if next_cluster >= 0xFF8:
                break
            cluster = next_cluster
        return chain

    def find_free_cluster(self):
        for i in range(2, self.num_clusters + 2):
            if self.read_fat_entry(i) == 0x000:
                return i
        return None

    def allocate_cluster_chain(self, num_clusters):
        if num_clusters <= 0:
            return []

        first_cluster = self.find_free_cluster()
        if first_cluster is None:
            return None

        self.set_fat_entry(first_cluster, 0xFFF)
        clusters = [first_cluster]
        prev_cluster = first_cluster
        for _ in range(1, num_clusters):
            cluster = self.find_free_cluster()
            if cluster is None:
                self.free_cluster_chain(first_cluster)
                return None

            self.set_fat_entry(prev_cluster, cluster)
            self.set_fat_entry(cluster, 0xFFF)
            clusters.append(cluster)
            prev_cluster = cluster

        return clusters

    def free_cluster_chain(self, start_cluster):
        if start_cluster < 2:
            return

        cluster = start_cluster
        while cluster < 0xFF8 and cluster >= 2:
            next_cluster = self.read_fat_entry(cluster)
            self.set_fat_entry(cluster, 0x000)
            cluster = next_cluster
            if next_cluster >= 0xFF8:
                break

    def get_cluster_data(self, cluster_chain, progress_callback=None):
        result = bytearray()
        total_clusters = len(cluster_chain)
        for i, cluster in enumerate(cluster_chain):
            offset = self.data_area_start + (cluster - 2) * self.cluster_size
            # Adjust progress for each cluster read
            def cluster_progress(p):
                if progress_callback and total_clusters > 0:
                    progress_callback((i + p) / total_clusters)
            data = self.read_bytes(offset, self.cluster_size, progress_callback=cluster_progress)
            result.extend(data)
        return bytes(result)

    def write_cluster_data(self, cluster_chain, data):
        remaining = len(data)
        data_pos = 0

        for i, cluster in enumerate(cluster_chain):
            offset = self.data_area_start + (cluster - 2) * self.cluster_size
            chunk_size = min(remaining, self.cluster_size)
            chunk = data[data_pos:data_pos + chunk_size]
            self.write_bytes(offset, chunk)
            if chunk_size < self.cluster_size:
                self.write_bytes(offset + chunk_size,
                               b'\x00' * (self.cluster_size - chunk_size))
            data_pos += chunk_size
            remaining -= chunk_size
            if remaining <= 0:
                break

    def zero_cluster(self, cluster):
        offset = self.data_area_start + (cluster - 2) * self.cluster_size
        self.write_bytes(offset, b'\x00' * self.cluster_size)

    def parse_dir_entry(self, entry):
        if len(entry) < 32 or entry[0] == 0x00 or entry[0] == 0xE5 or entry[11] & 0x08:
            return None

        name = entry[0:8].decode('cp437').strip()
        ext = entry[8:11].decode('cp437').strip()
        full_name = f"{name}.{ext}" if ext else name
        is_dir = bool(entry[11] & 0x10)
        starting_cluster = struct.unpack('<H', entry[26:28])[0]
        file_size = struct.unpack('<I', entry[28:32])[0]

        time_val = struct.unpack('<H', entry[22:24])[0]
        date_val = struct.unpack('<H', entry[24:26])[0]
        dt = self.fat_time_to_datetime(time_val, date_val)

        return {
            'name': full_name,
            'is_dir': is_dir,
            'starting_cluster': starting_cluster,
            'size': 0 if is_dir else file_size,
            'datetime': dt,
            'attributes': self.get_attributes(entry[11])
        }

    def create_dir_entry(self, name, is_dir, starting_cluster, size=0, dt=None):
        if dt is None:
            dt = datetime.datetime.now()

        parts = name.upper().split('.')
        name_part = (name if is_dir and name in [".", ".."] else parts[0]).ljust(8)
        ext = '   ' if len(parts) == 1 or name in [".", ".."] else parts[1].ljust(3)
        attr = 0x10 if is_dir else 0x00

        entry = bytearray(32)
        entry[0:8] = name_part.encode('cp437')
        entry[8:11] = ext.encode('cp437')
        entry[11] = attr
        entry[14:16] = struct.pack('<H', self.fat_time(dt))
        entry[16:18] = struct.pack('<H', self.fat_date(dt))
        entry[18:20] = struct.pack('<H', self.fat_date(dt))
        entry[22:24] = struct.pack('<H', self.fat_time(dt))
        entry[24:26] = struct.pack('<H', self.fat_date(dt))
        entry[26:28] = struct.pack('<H', starting_cluster)
        entry[28:32] = struct.pack('<I', size)

        return entry

    def read_directory_data(self, cluster, is_root=False):
        if is_root:
            return self.read_bytes(self.root_dir_start, self.root_dir_sectors * self.sector_size)
        else:
            chain = self.get_cluster_chain(cluster)
            return self.get_cluster_data(chain)

    def normalize_path(self, path):
        return "/" + path.strip("/") if path != "/" else "/"

    def split_path(self, path):
        path = self.normalize_path(path.upper())
        if path == "/":
            return "/", ""

        parts = path.strip("/").split("/")
        name = parts[-1]
        parent_path = "/" + "/".join(parts[:-1]) if len(parts) > 1 else "/"

        return parent_path, name

    def scan_directory(self, start_cluster, is_root=False):
        entries = []
        dir_data = self.read_directory_data(start_cluster, is_root)

        if is_root:
            base_offset = self.root_dir_start
        else:
            base_offset = self.data_area_start + (start_cluster - 2) * self.cluster_size

        pos = 0
        while pos + 32 <= len(dir_data):
            entry_data = dir_data[pos:pos+32]

            if entry_data[0] == 0x00:
                break

            if entry_data[0] == 0xE5:
                pos += 32
                continue

            entry = self.parse_dir_entry(entry_data)
            if entry is not None:
                entries.append((entry, base_offset + pos))

            pos += 32

        return entries

    def parse_directory(self, start_cluster, current_path, is_root=False):
        files = []
        entries = self.scan_directory(start_cluster, is_root)

        for entry, _ in entries:
            if entry['name'] in ['.', '..']:
                continue

            file_path = f"{current_path}/{entry['name']}" if current_path else entry['name']
            entry_with_path = entry.copy()
            entry_with_path['name'] = file_path
            files.append(entry_with_path)

            if entry['is_dir']:
                files.extend(self.parse_directory(entry['starting_cluster'], file_path))

        return files

    def list_files(self):
        return self.parse_directory(0, '', is_root=True)

    def find_directory_cluster(self, path):
        if path in ["/", ""]:
            return 0

        parts = path.strip("/").split("/")
        current_cluster = 0

        for part in parts:
            found = False
            for entry, _ in self.scan_directory(current_cluster, current_cluster == 0):
                if entry['name'].lower() == part.lower() and entry['is_dir']:
                    current_cluster = entry['starting_cluster']
                    found = True
                    break
            if not found:
                return None

        return current_cluster

    def find_entry(self, path):
        parent_path, target_name = self.split_path(path)

        if path == "/":
            return None

        parent_cluster = self.find_directory_cluster(parent_path)
        if parent_cluster is None:
            return None

        target_name_upper = target_name.upper()
        for entry, offset in self.scan_directory(parent_cluster, parent_cluster == 0):
            if entry['name'].upper() == target_name_upper:
                return parent_cluster, offset, entry

        return None

    def is_directory_empty(self, cluster):
        entries = self.scan_directory(cluster, False)
        return len([e for e, _ in entries if e['name'] not in ['.', '..']]) == 0

    def find_free_entry_offset(self, parent_cluster):
        if parent_cluster == 0:
            dir_start = self.root_dir_start
            dir_size = self.root_dir_sectors * self.sector_size
            dir_data = self.read_bytes(dir_start, dir_size)

            for i in range(0, dir_size, 32):
                entry = dir_data[i:i+32]
                if entry[0] in [0x00, 0xE5]:
                    return dir_start + i
            return None
        else:
            chain = self.get_cluster_chain(parent_cluster)
            for cluster in chain:
                offset = self.data_area_start + (cluster - 2) * self.cluster_size
                dir_data = self.read_bytes(offset, self.cluster_size)

                for i in range(0, self.cluster_size, 32):
                    if i + 32 > len(dir_data):
                        break
                    entry = dir_data[i:i+32]
                    if entry[0] in [0x00, 0xE5]:
                        return offset + i

            new_cluster = self.find_free_cluster()
            if new_cluster is None:
                return None

            last_cluster = chain[-1] if chain else parent_cluster
            self.set_fat_entry(last_cluster, new_cluster)
            self.set_fat_entry(new_cluster, 0xFFF)

            self.zero_cluster(new_cluster)

            return self.data_area_start + (new_cluster - 2) * self.cluster_size

    def create_directory(self, parent_path, new_dir_name, dt=None, progress_callback=None):
        if not self.is_valid_83_name(new_dir_name):
            raise ValueError("Invalid 8.3 name")

        parent_cluster = self.find_directory_cluster(parent_path)
        if parent_cluster is None:
            raise ValueError("Parent directory not found")

        if any(e['name'].upper() == new_dir_name.upper()
            for e, _ in self.scan_directory(parent_cluster, parent_cluster == 0)):
            raise ValueError("Name already exists")

        free_offset = self.find_free_entry_offset(parent_cluster)
        if free_offset is None:
            raise ValueError("No space in parent directory")

        new_cluster = self.find_free_cluster()
        if new_cluster is None:
            raise ValueError("No free clusters")

        self.set_fat_entry(new_cluster, 0xFFF)
        self.zero_cluster(new_cluster)

        if dt is None:
            dt = datetime.datetime.now()

        dot_entry = self.create_dir_entry(".", True, new_cluster, 0, dt)
        dotdot_entry = self.create_dir_entry("..", True, parent_cluster if parent_cluster != 0 else 0, 0, dt)

        cluster_offset = self.data_area_start + (new_cluster - 2) * self.cluster_size
        self.write_bytes(cluster_offset, dot_entry)
        self.write_bytes(cluster_offset + 32, dotdot_entry)

        self.write_bytes(free_offset,
                        self.create_dir_entry(new_dir_name.upper(), True, new_cluster, 0, dt))

        self.flush_func(progress_callback=progress_callback)

    def delete_item(self, path, progress_callback=None):
        entry_info = self.find_entry(path)
        if entry_info is None:
            raise ValueError("Entry not found")

        _, offset, entry = entry_info

        if entry['is_dir'] and not self.is_directory_empty(entry['starting_cluster']):
            raise ValueError("Directory not empty")

        current_entry = self.read_bytes(offset, 32)
        self.write_bytes(offset, b'\xE5' + current_entry[1:])

        self.free_cluster_chain(entry['starting_cluster'])
        self.flush_func(progress_callback=progress_callback)

    def extract_file(self, path, progress_callback=None):
        entry_info = self.find_entry(path)
        if not entry_info or entry_info[2]['is_dir']:
            raise ValueError("File not found or is a directory")

        _, _, entry = entry_info

        if entry['starting_cluster'] < 2:
            return b''

        cluster_chain = self.get_cluster_chain(entry['starting_cluster'])
        file_data = self.get_cluster_data(cluster_chain, progress_callback=progress_callback)

        return file_data[:entry['size']]

    def insert_file(self, parent_path, file_name, file_data, dt=None, progress_callback=None):
        if not self.is_valid_83_name(file_name):
            raise ValueError("Invalid 8.3 name")

        parent_cluster = self.find_directory_cluster(parent_path)
        if parent_cluster is None:
            raise ValueError("Parent directory not found")

        if any(e['name'].upper() == file_name.upper()
            for e, _ in self.scan_directory(parent_cluster, parent_cluster == 0)):
            raise ValueError("Name already exists")

        free_offset = self.find_free_entry_offset(parent_cluster)
        if free_offset is None:
            raise ValueError("No space in parent directory")

        num_needed = math.ceil(len(file_data) / self.cluster_size)
        if num_needed == 0:
            num_needed = 1

        clusters = self.allocate_cluster_chain(num_needed)
        if clusters is None:
            raise ValueError("Not enough free clusters")

        self.write_cluster_data(clusters, file_data)

        if dt is None:
            dt = datetime.datetime.now()

        self.write_bytes(free_offset,
                        self.create_dir_entry(file_name.upper(), False, clusters[0], len(file_data), dt))

        self.flush_func(progress_callback=progress_callback)

    def check_filesystem_integrity(self):
        cluster_owners = {}
        errors = []

        for file_info in self.list_files():
            if not file_info['is_dir'] and file_info['starting_cluster'] >= 2:
                chain = self.get_cluster_chain(file_info['starting_cluster'])
                for cluster in chain:
                    if cluster in cluster_owners:
                        errors.append(f"Cross-linked cluster {cluster} used by both "
                                    f"'{file_info['name']}' and '{cluster_owners[cluster]}'")
                    else:
                        cluster_owners[cluster] = file_info['name']

        for start_cluster in range(2, self.num_clusters + 2):
            value = self.read_fat_entry(start_cluster)
            if 2 <= value < 0xFF0:
                seen = {start_cluster}
                next_cluster = value
                while 2 <= next_cluster < 0xFF0:
                    if next_cluster in seen:
                        errors.append(f"Loop detected in cluster chain starting at {start_cluster}")
                        break
                    seen.add(next_cluster)
                    next_cluster = self.read_fat_entry(next_cluster)

        return errors

    def is_valid_83_name(self, name):
        invalid_chars = '"*/:<>?\\|+,;=[]'
        if any(c in invalid_chars for c in name):
            return False

        parts = name.split('.')
        if len(parts) > 2 or not parts[0]:
            return False
        if len(parts[0]) > 8 or (len(parts) == 2 and len(parts[1]) > 3):
            return False

        reserved_names = ["CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3",
                        "COM4", "LPT1", "LPT2", "LPT3", "LPT4"]
        base_name = parts[0].upper()
        if base_name in reserved_names:
            return False

        return True
