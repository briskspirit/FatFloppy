import math
import struct
import datetime


class FAT12FileSystem:
    def __init__(self, disk_manager, params):
        """
        Initialize FAT12FileSystem with a DiskManager and parameters.

        Args:
            disk_manager (DiskManager): The disk access manager.
            params (dict): BPB parameters.
        """
        self.disk_manager = disk_manager
        self.params = params
        self.sector_size = params['bytes_per_sector']
        self.total_sectors = params['total_sectors']
        self.reserved_sectors = params['reserved_sectors']
        self.num_fats = params['num_fats']
        self.sectors_per_fat = params['sectors_per_fat']
        self.root_entries = params['root_entries']
        self.root_dir_sectors = (self.root_entries * 32 + self.sector_size - 1) // self.sector_size
        self.fat_start = self.reserved_sectors * self.sector_size
        self.root_dir_start = self.fat_start + (self.num_fats * self.sectors_per_fat * self.sector_size)
        self.data_area_start = self.root_dir_start + (self.root_dir_sectors * self.sector_size)
        self.num_clusters = (self.total_sectors - (self.reserved_sectors + self.num_fats * self.sectors_per_fat + self.root_dir_sectors)) // params['sectors_per_cluster']

    def initialize_fats(self):
        """Initialize FATs for a new file system."""
        media_descriptor = self.params['media_descriptor']
        for fat in range(self.num_fats):
            offset = self.fat_start + (fat * self.sectors_per_fat * self.sector_size)
            self.disk_manager.write_bytes(offset, bytes([media_descriptor, 0xFF, 0xFF]))

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

    def get_cluster_chain(self, start_cluster):
        chain = []
        cluster = start_cluster
        while cluster < 0xFF8 and cluster >= 2:
            chain.append(cluster)
            offset = self.fat_start + int(cluster * 1.5)
            value_bytes = self.disk_manager.read_bytes(offset, 2)
            value = struct.unpack('<H', value_bytes)[0]
            next_cluster = value & 0x0FFF if cluster % 2 == 0 else (value >> 4) & 0x0FFF
            if next_cluster >= 0xFF8:
                break
            cluster = next_cluster
        return chain

    def set_fat_entry(self, cluster, value):
        for fat in range(self.num_fats):
            offset = self.fat_start + (fat * self.sectors_per_fat * self.sector_size) + int(cluster * 1.5)
            current_bytes = self.disk_manager.read_bytes(offset, 2)
            current = struct.unpack('<H', current_bytes)[0]
            if cluster % 2 == 0:
                new_value = (current & 0xF000) | (value & 0x0FFF)
            else:
                new_value = (current & 0x000F) | ((value & 0x0FFF) << 4)
            new_bytes = struct.pack('<H', new_value)
            self.disk_manager.write_bytes(offset, new_bytes)

    def find_free_cluster(self):
        for i in range(2, self.num_clusters + 2):
            offset = self.fat_start + int(i * 1.5)
            value_bytes = self.disk_manager.read_bytes(offset, 2)
            value = struct.unpack('<H', value_bytes)[0]
            cluster_value = value & 0x0FFF if i % 2 == 0 else (value >> 4) & 0x0FFF
            if cluster_value == 0x000:
                return i
        return None

    def parse_directory(self, start_cluster, current_path, is_root=False):
        cluster_size = self.params['sectors_per_cluster'] * self.sector_size
        files = []
        if is_root:
            dir_data = self.disk_manager.read_bytes(self.root_dir_start, self.root_dir_sectors * self.sector_size)
        else:
            chain = self.get_cluster_chain(start_cluster)
            dir_data = b''.join(self.disk_manager.read_bytes(self.data_area_start + (c - 2) * cluster_size, cluster_size) for c in chain)
        for i in range(0, len(dir_data), 32):
            entry = dir_data[i:i+32]
            if len(entry) < 32 or entry[0] == 0x00:
                break
            if entry[0] == 0xE5 or entry[11] & 0x08:
                continue
            name = entry[0:8].decode('cp437').strip()
            ext = entry[8:11].decode('cp437').strip()
            full_name = f"{name}.{ext}" if ext else name
            if full_name in ['', '.', '..']:
                continue
            is_dir = bool(entry[11] & 0x10)
            starting_cluster = struct.unpack('<H', entry[26:28])[0]
            file_size = struct.unpack('<I', entry[28:32])[0]
            file_path = f"{current_path}/{full_name}" if current_path else full_name
            dt = self.fat_time_to_datetime(struct.unpack('<H', entry[22:24])[0], struct.unpack('<H', entry[24:26])[0])
            files.append({
                'name': file_path, 'is_dir': is_dir, 'starting_cluster': starting_cluster,
                'size': 0 if is_dir else file_size, 'datetime': dt, 'attributes': self.get_attributes(entry[11])
            })
            if is_dir:
                files.extend(self.parse_directory(starting_cluster, file_path))
        return files

    def list_files(self):
        return self.parse_directory(0, '', is_root=True)

    def get_directory_entries(self, cluster, is_root):
        cluster_size = self.params['sectors_per_cluster'] * self.sector_size
        entries = []
        if is_root:
            dir_start = self.root_dir_start
            dir_size = self.root_dir_sectors * self.sector_size
            dir_data = self.disk_manager.read_bytes(dir_start, dir_size)
            for i in range(0, len(dir_data), 32):
                entry = dir_data[i:i+32]
                if entry[0] == 0x00:
                    break
                if entry[0] == 0xE5 or entry[11] & 0x08:
                    continue
                name = entry[0:8].decode('cp437').strip()
                ext = entry[8:11].decode('cp437').strip()
                full_name = f"{name}.{ext}" if ext else name
                offset = dir_start + i
                entries.append(({'name': full_name, 'is_dir': bool(entry[11] & 0x10), 
                                'starting_cluster': struct.unpack('<H', entry[26:28])[0], 
                                'size': struct.unpack('<I', entry[28:32])[0]}, offset))
        else:
            chain = self.get_cluster_chain(cluster)
            for cluster_num in chain:
                offset = self.data_area_start + (cluster_num - 2) * cluster_size
                dir_data = self.disk_manager.read_bytes(offset, cluster_size)
                for i in range(0, len(dir_data), 32):
                    entry = dir_data[i:i+32]
                    if len(entry) < 32 or entry[0] == 0x00:
                        break
                    if entry[0] == 0xE5 or entry[11] & 0x08:
                        continue
                    name = entry[0:8].decode('cp437').strip()
                    ext = entry[8:11].decode('cp437').strip()
                    full_name = f"{name}.{ext}" if ext else name
                    entry_offset = offset + i
                    entries.append(({'name': full_name, 'is_dir': bool(entry[11] & 0x10), 
                                    'starting_cluster': struct.unpack('<H', entry[26:28])[0], 
                                    'size': struct.unpack('<I', entry[28:32])[0]}, entry_offset))
        return entries

    def find_directory_cluster(self, path):
        if path in ["/", ""]:
            return 0
        parts = path.strip("/").split("/")
        current_cluster = 0
        for part in parts:
            found = False
            for entry, _ in self.get_directory_entries(current_cluster, current_cluster == 0):
                if entry['name'].lower() == part.lower() and entry['is_dir']:
                    current_cluster = entry['starting_cluster']
                    found = True
                    break
            if not found:
                return None
        return current_cluster

    def find_entry(self, path):
        parts = path.strip("/").split("/")
        parent_path = "/" + "/".join(parts[:-1]) if len(parts) > 1 else "/"
        target_name = parts[-1]
        parent_cluster = self.find_directory_cluster(parent_path)
        if parent_cluster is None:
            return None
        for entry, offset in self.get_directory_entries(parent_cluster, parent_cluster == 0):
            if entry['name'].lower() == target_name.lower():
                return parent_cluster, offset, entry
        return None

    def extract_file(self, path):
        entry_info = self.find_entry(path)
        if not entry_info or entry_info[2]['is_dir']:
            raise ValueError("File not found or is a directory")
        _, _, entry = entry_info
        cluster_chain = self.get_cluster_chain(entry['starting_cluster'])
        cluster_size = self.params['sectors_per_cluster'] * self.sector_size
        file_data = b''.join(self.disk_manager.read_bytes(self.data_area_start + (c - 2) * cluster_size, cluster_size) for c in cluster_chain)
        return file_data[:entry['size']]

    def is_valid_83_name(self, name):
        parts = name.split('.')
        if len(parts) > 2 or not parts[0]:
            return False
        if len(parts[0]) > 8 or (len(parts) == 2 and len(parts[1]) > 3):
            return False
        return True

    def find_free_entry_offset(self, parent_cluster):
        cluster_size = self.params['sectors_per_cluster'] * self.sector_size
        if parent_cluster == 0:
            dir_start = self.root_dir_start
            dir_size = self.root_dir_sectors * self.sector_size
            dir_data = self.disk_manager.read_bytes(dir_start, dir_size)
            for i in range(0, dir_size, 32):
                entry = dir_data[i:i+32]
                if entry[0] in [0x00, 0xE5]:
                    return dir_start + i
            return None
        chain = self.get_cluster_chain(parent_cluster)
        for cluster in chain:
            offset = self.data_area_start + (cluster - 2) * cluster_size
            dir_data = self.disk_manager.read_bytes(offset, cluster_size)
            for i in range(0, len(dir_data), 32):
                entry = dir_data[i:i+32]
                if len(entry) < 32:
                    continue
                if entry[0] in [0x00, 0xE5]:
                    return offset + i
        new_cluster = self.find_free_cluster()
        if new_cluster is None:
            return None
        last_cluster = chain[-1] if chain else parent_cluster
        self.set_fat_entry(last_cluster, new_cluster)
        self.set_fat_entry(new_cluster, 0xFFF)
        return self.data_area_start + (new_cluster - 2) * cluster_size

    def create_dir_entry(self, name, is_dir, starting_cluster, size=0, dt=None):
        if dt is None:
            dt = datetime.datetime.now()
        parts = name.split('.')
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

    def create_directory(self, parent_path, new_dir_name, dt=None):
        if not self.is_valid_83_name(new_dir_name):
            raise ValueError("Invalid 8.3 name")
        parent_cluster = self.find_directory_cluster(parent_path)
        if parent_cluster is None:
            raise ValueError("Parent directory not found")
        if any(e['name'].lower() == new_dir_name.lower() for e, _ in self.get_directory_entries(parent_cluster, parent_cluster == 0)):
            raise ValueError("Name already exists")
        free_offset = self.find_free_entry_offset(parent_cluster)
        if free_offset is None:
            raise ValueError("No space in parent directory")
        new_cluster = self.find_free_cluster()
        if new_cluster is None:
            raise ValueError("No free clusters")
        self.set_fat_entry(new_cluster, 0xFFF)
        cluster_size = self.params['sectors_per_cluster'] * self.params['bytes_per_sector']
        cluster_offset = self.data_area_start + (new_cluster - 2) * cluster_size
        if dt is None:
            dt = datetime.datetime.now()
        # Write . and .. entries
        dot_entry = self.create_dir_entry(".", True, new_cluster, 0, dt)
        dotdot_entry = self.create_dir_entry("..", True, parent_cluster if parent_cluster != 0 else 0, 0, dt)
        self.disk_manager.write_bytes(cluster_offset, dot_entry)
        self.disk_manager.write_bytes(cluster_offset + 32, dotdot_entry)
        # Zero out the rest of the cluster
        self.disk_manager.write_bytes(cluster_offset + 64, b'\x00' * (cluster_size - 64))
        # Write the new directory entry in the parent directory
        self.disk_manager.write_bytes(free_offset, self.create_dir_entry(new_dir_name, True, new_cluster, 0, dt))
        # Flush changes to disk
        self.disk_manager.flush()

    def free_cluster_chain(self, start_cluster):
        cluster = start_cluster
        while cluster < 0xFF8 and cluster >= 2:
            offset = self.fat_start + int(cluster * 1.5)
            value_bytes = self.disk_manager.read_bytes(offset, 2)
            value = struct.unpack('<H', value_bytes)[0]
            next_cluster = value & 0x0FFF if cluster % 2 == 0 else (value >> 4) & 0x0FFF
            self.set_fat_entry(cluster, 0x000)
            cluster = next_cluster
            if next_cluster >= 0xFF8:
                break

    def is_directory_empty(self, cluster):
        entries = self.get_directory_entries(cluster, False)
        return len([e for e, _ in entries if e['name'] not in ['.', '..']]) == 0

    def delete_item(self, path):
        entry_info = self.find_entry(path)
        if entry_info is None:
            raise ValueError("Entry not found")
        _, offset, entry = entry_info
        if entry['is_dir'] and not self.is_directory_empty(entry['starting_cluster']):
            raise ValueError("Directory not empty")
        current_entry = self.disk_manager.read_bytes(offset, 32)
        self.disk_manager.write_bytes(offset, b'\xE5' + current_entry[1:])
        self.free_cluster_chain(entry['starting_cluster'])
        # Flush changes to disk
        self.disk_manager.flush()

    def insert_file(self, parent_path, file_name, file_data, dt=None):
        if not self.is_valid_83_name(file_name):
            raise ValueError("Invalid 8.3 name")
        parent_cluster = self.find_directory_cluster(parent_path)
        if parent_cluster is None:
            raise ValueError("Parent directory not found")
        if any(e['name'].lower() == file_name.lower() for e, _ in self.get_directory_entries(parent_cluster, parent_cluster == 0)):
            raise ValueError("Name already exists")
        free_offset = self.find_free_entry_offset(parent_cluster)
        if free_offset is None:
            raise ValueError("No space in parent directory")
        cluster_size = self.params['sectors_per_cluster'] * self.params['bytes_per_sector']
        num_needed = math.ceil(len(file_data) / cluster_size)
        clusters = []
        for _ in range(num_needed):
            cluster = self.find_free_cluster()
            if cluster is None:
                for c in clusters:
                    self.set_fat_entry(c, 0x000)
                raise ValueError("Not enough free clusters")
            clusters.append(cluster)
            self.set_fat_entry(cluster, 0xFFF)
        for i in range(len(clusters) - 1):
            self.set_fat_entry(clusters[i], clusters[i + 1])
        for i, cluster in enumerate(clusters):
            offset = self.data_area_start + (cluster - 2) * cluster_size
            start = i * cluster_size
            end = min(start + cluster_size, len(file_data))
            chunk = file_data[start:end]
            self.disk_manager.write_bytes(offset, chunk)
            if len(chunk) < cluster_size:
                self.disk_manager.write_bytes(offset + len(chunk), b'\x00' * (cluster_size - len(chunk)))
        if dt is None:
            dt = datetime.datetime.now()
        self.disk_manager.write_bytes(free_offset, self.create_dir_entry(file_name, False, clusters[0], len(file_data), dt))
        # Flush changes to disk
        self.disk_manager.flush()
