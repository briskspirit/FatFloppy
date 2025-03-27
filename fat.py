import datetime
import math
import struct

class FAT12FileSystem:
    def __init__(self, read_bytes_func, write_bytes_func, flush_func, params):
        self.read_bytes = read_bytes_func
        self.write_bytes = write_bytes_func
        self.flush_func = flush_func
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
        self.cluster_size = self.params['sectors_per_cluster'] * self.sector_size

        print(self.check_filesystem_integrity())

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

    def get_cluster_data(self, cluster_chain):
        result = bytearray()
        for cluster in cluster_chain:
            offset = self.data_area_start + (cluster - 2) * self.cluster_size
            data = self.read_bytes(offset, self.cluster_size)
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
        file_data = self.get_cluster_data(cluster_chain)

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
