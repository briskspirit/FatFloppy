import datetime
import math
import struct


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
        self.cluster_size = self.params['sectors_per_cluster'] * self.sector_size

    def initialize_fats(self):
        """Initialize FATs for a new file system."""
        media_descriptor = self.params['media_descriptor']
        for fat in range(self.num_fats):
            offset = self.fat_start + (fat * self.sectors_per_fat * self.sector_size)
            self.disk_manager.write_bytes(offset, bytes([media_descriptor, 0xFF, 0xFF]))

    # FAT Date/Time utilities
    @staticmethod
    def fat_time(dt):
        """Convert datetime to FAT time format."""
        second = dt.second // 2
        minute = dt.minute
        hour = dt.hour
        return (hour << 11) | (minute << 5) | second

    @staticmethod
    def fat_date(dt):
        """Convert datetime to FAT date format."""
        day = dt.day
        month = dt.month
        year = dt.year - 1980
        return (year << 9) | (month << 5) | day

    @staticmethod
    def fat_time_to_datetime(fat_time, fat_date):
        """Convert FAT time and date to datetime object."""
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
        """Convert FAT attribute byte to human-readable string."""
        attributes = []
        if attr_byte & 0x01: attributes.append("RO")
        if attr_byte & 0x02: attributes.append("H")
        if attr_byte & 0x04: attributes.append("S")
        if attr_byte & 0x20: attributes.append("A")
        return " ".join(attributes) if attributes else "-"

    # FAT12 entry manipulation
    def read_fat_entry(self, cluster):
        """Read a FAT12 entry for the given cluster."""
        offset = self.fat_start + int(cluster * 1.5)
        value_bytes = self.disk_manager.read_bytes(offset, 2)
        value = struct.unpack('<H', value_bytes)[0]
        if cluster % 2 == 0:
            # Even cluster: uses the low 12 bits
            return value & 0x0FFF
        else:
            # Odd cluster: uses the high 12 bits
            return (value >> 4) & 0x0FFF

    def set_fat_entry(self, cluster, value):
        """Set FAT entry for all FAT copies."""
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

    def get_cluster_chain(self, start_cluster):
        """Get the chain of clusters starting from the given cluster."""
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
        """Find a free cluster in the FAT."""
        for i in range(2, self.num_clusters + 2):
            if self.read_fat_entry(i) == 0x000:
                return i
        return None

    def allocate_cluster_chain(self, num_clusters):
        """Allocate a chain of clusters and link them together.

        Args:
            num_clusters: Number of clusters to allocate

        Returns:
            List of allocated cluster numbers or None if allocation fails
        """
        clusters = []
        for _ in range(num_clusters):
            cluster = self.find_free_cluster()
            if cluster is None:
                # Clean up if allocation fails
                for c in clusters:
                    self.set_fat_entry(c, 0x000)
                return None
            clusters.append(cluster)
            self.set_fat_entry(cluster, 0xFFF)  # Mark as end of chain initially

        # Link clusters together
        for i in range(len(clusters) - 1):
            self.set_fat_entry(clusters[i], clusters[i + 1])

        return clusters

    def free_cluster_chain(self, start_cluster):
        """Free a chain of clusters starting from the given cluster."""
        if start_cluster < 2:  # Invalid cluster numbers
            return

        cluster = start_cluster
        while cluster < 0xFF8 and cluster >= 2:
            next_cluster = self.read_fat_entry(cluster)
            self.set_fat_entry(cluster, 0x000)
            cluster = next_cluster
            if next_cluster >= 0xFF8:
                break

    def get_cluster_data(self, cluster_chain):
        """Read data from a chain of clusters.

        Args:
            cluster_chain: List of cluster numbers

        Returns:
            Bytes object containing the concatenated data from all clusters
        """
        result = bytearray()
        for cluster in cluster_chain:
            offset = self.data_area_start + (cluster - 2) * self.cluster_size
            data = self.disk_manager.read_bytes(offset, self.cluster_size)
            result.extend(data)
        return bytes(result)

    def write_cluster_data(self, cluster_chain, data):
        """Write data to a chain of clusters.

        Args:
            cluster_chain: List of cluster numbers
            data: Data bytes to write

        Returns:
            None
        """
        remaining = len(data)
        data_pos = 0

        for i, cluster in enumerate(cluster_chain):
            offset = self.data_area_start + (cluster - 2) * self.cluster_size

            # Calculate how much data to write to this cluster
            chunk_size = min(remaining, self.cluster_size)
            chunk = data[data_pos:data_pos + chunk_size]

            # Write the data chunk
            self.disk_manager.write_bytes(offset, chunk)

            # If chunk doesn't fill the cluster, zero out the rest
            if chunk_size < self.cluster_size:
                self.disk_manager.write_bytes(offset + chunk_size,
                                           b'\x00' * (self.cluster_size - chunk_size))

            # Update counters
            data_pos += chunk_size
            remaining -= chunk_size

            if remaining <= 0:
                break

    def zero_cluster(self, cluster):
        """Zero out an entire cluster.

        Args:
            cluster: Cluster number to zero

        Returns:
            None
        """
        offset = self.data_area_start + (cluster - 2) * self.cluster_size
        self.disk_manager.write_bytes(offset, b'\x00' * self.cluster_size)

    # Directory entry utilities
    def parse_dir_entry(self, entry):
        """Parse a 32-byte directory entry.

        Args:
            entry: 32-byte directory entry data

        Returns:
            Dictionary with parsed entry data or None if entry is invalid
        """
        # Skip invalid, deleted, or system entries
        if len(entry) < 32 or entry[0] == 0x00 or entry[0] == 0xE5 or entry[11] & 0x08:
            return None

        name = entry[0:8].decode('cp437').strip()
        ext = entry[8:11].decode('cp437').strip()
        full_name = f"{name}.{ext}" if ext else name
        is_dir = bool(entry[11] & 0x10)
        starting_cluster = struct.unpack('<H', entry[26:28])[0]
        file_size = struct.unpack('<I', entry[28:32])[0]

        # Get datetime
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
        """Create a 32-byte directory entry.

        Args:
            name: File or directory name (8.3 format)
            is_dir: Whether this is a directory entry
            starting_cluster: First cluster number
            size: File size in bytes (ignored for directories)
            dt: Datetime for timestamp (defaults to current time)

        Returns:
            32-byte directory entry
        """
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
        """Read raw directory data from disk.

        Args:
            cluster: Cluster number for directory (ignored if is_root=True)
            is_root: Whether this is the root directory

        Returns:
            Bytes containing all directory entry data
        """
        if is_root:
            return self.disk_manager.read_bytes(self.root_dir_start, self.root_dir_sectors * self.sector_size)
        else:
            chain = self.get_cluster_chain(cluster)
            return self.get_cluster_data(chain)

    # Path manipulation
    def normalize_path(self, path):
        """Normalize a path to standard format."""
        return "/" + path.strip("/") if path != "/" else "/"

    def split_path(self, path):
        """Split a path into parent path and name components."""
        path = self.normalize_path(path.upper())
        if path == "/":
            return "/", ""

        parts = path.strip("/").split("/")
        name = parts[-1]
        parent_path = "/" + "/".join(parts[:-1]) if len(parts) > 1 else "/"

        return parent_path, name

    # Directory operations
    def scan_directory(self, start_cluster, is_root=False):
        """Scan a directory and return its entries with their offsets.

        Args:
            start_cluster: Starting cluster of directory (ignored if is_root=True)
            is_root: Whether this is the root directory

        Returns:
            List of tuples (entry_dict, offset) for each valid entry
        """
        entries = []
        dir_data = self.read_directory_data(start_cluster, is_root)

        if is_root:
            base_offset = self.root_dir_start
        else:
            base_offset = self.data_area_start + (start_cluster - 2) * self.cluster_size

        # Process all 32-byte entries
        pos = 0
        while pos + 32 <= len(dir_data):
            entry_data = dir_data[pos:pos+32]

            # End of directory marker
            if entry_data[0] == 0x00:
                break

            # Skip deleted entries but continue processing
            if entry_data[0] == 0xE5:
                pos += 32
                continue

            # Parse the entry
            entry = self.parse_dir_entry(entry_data)
            if entry is not None:
                entries.append((entry, base_offset + pos))

            pos += 32

        return entries

    def parse_directory(self, start_cluster, current_path, is_root=False):
        """Parse a directory and return its file entries recursively.

        Args:
            start_cluster: Starting cluster of directory (ignored if is_root=True)
            current_path: Current path string for building full paths
            is_root: Whether this is the root directory

        Returns:
            List of file and directory entries with full paths
        """
        files = []
        entries = self.scan_directory(start_cluster, is_root)

        for entry, _ in entries:
            # Skip . and .. entries
            if entry['name'] in ['.', '..']:
                continue

            # Add path to entry
            file_path = f"{current_path}/{entry['name']}" if current_path else entry['name']
            entry_with_path = entry.copy()
            entry_with_path['name'] = file_path
            files.append(entry_with_path)

            # Recursively process subdirectories
            if entry['is_dir']:
                files.extend(self.parse_directory(entry['starting_cluster'], file_path))

        return files

    def list_files(self):
        """List all files in the filesystem."""
        return self.parse_directory(0, '', is_root=True)

    def find_directory_cluster(self, path):
        """Find the cluster number for a directory path."""
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
        """Find a file or directory entry by path."""
        parent_path, target_name = self.split_path(path)

        # Handle root directory case
        if path == "/":
            return None

        parent_cluster = self.find_directory_cluster(parent_path)
        if parent_cluster is None:
            return None

        for entry, offset in self.scan_directory(parent_cluster, parent_cluster == 0):
            if entry['name'].lower() == target_name.lower():
                return parent_cluster, offset, entry

        return None

    def is_directory_empty(self, cluster):
        """Check if a directory is empty (contains only . and .. entries)."""
        entries = self.scan_directory(cluster, False)
        return len([e for e, _ in entries if e['name'] not in ['.', '..']]) == 0

    def find_free_entry_offset(self, parent_cluster):
        """Find a free directory entry slot in the given directory.

        Args:
            parent_cluster: Cluster number of directory to search

        Returns:
            Offset of free entry or None if no space available
        """
        if parent_cluster == 0:
            # Root directory - fixed size
            dir_start = self.root_dir_start
            dir_size = self.root_dir_sectors * self.sector_size
            dir_data = self.disk_manager.read_bytes(dir_start, dir_size)

            for i in range(0, dir_size, 32):
                entry = dir_data[i:i+32]
                if entry[0] in [0x00, 0xE5]:
                    return dir_start + i
            return None
        else:
            # Regular directory - can be extended
            chain = self.get_cluster_chain(parent_cluster)
            for cluster in chain:
                offset = self.data_area_start + (cluster - 2) * self.cluster_size
                dir_data = self.disk_manager.read_bytes(offset, self.cluster_size)

                for i in range(0, self.cluster_size, 32):
                    if i + 32 > len(dir_data):
                        break
                    entry = dir_data[i:i+32]
                    if entry[0] in [0x00, 0xE5]:
                        return offset + i

            # No free entry found, allocate a new cluster
            new_cluster = self.find_free_cluster()
            if new_cluster is None:
                return None

            # Link to directory chain
            last_cluster = chain[-1] if chain else parent_cluster
            self.set_fat_entry(last_cluster, new_cluster)
            self.set_fat_entry(new_cluster, 0xFFF)

            # Zero out the new cluster
            self.zero_cluster(new_cluster)

            # Return first entry in the new cluster
            return self.data_area_start + (new_cluster - 2) * self.cluster_size

    # File system modification operations
    def create_directory(self, parent_path, new_dir_name, dt=None, progress_callback=None):
        """Create a new directory."""
        if not self.is_valid_83_name(new_dir_name):
            raise ValueError("Invalid 8.3 name")

        parent_cluster = self.find_directory_cluster(parent_path)
        if parent_cluster is None:
            raise ValueError("Parent directory not found")

        # Check if name already exists
        if any(e['name'].upper() == new_dir_name.upper()
               for e, _ in self.scan_directory(parent_cluster, parent_cluster == 0)):
            raise ValueError("Name already exists")

        free_offset = self.find_free_entry_offset(parent_cluster)
        if free_offset is None:
            raise ValueError("No space in parent directory")

        new_cluster = self.find_free_cluster()
        if new_cluster is None:
            raise ValueError("No free clusters")

        # Mark cluster as end of chain
        self.set_fat_entry(new_cluster, 0xFFF)

        # Zero out the new directory cluster
        self.zero_cluster(new_cluster)

        if dt is None:
            dt = datetime.datetime.now()

        # Write . and .. entries
        dot_entry = self.create_dir_entry(".", True, new_cluster, 0, dt)
        dotdot_entry = self.create_dir_entry("..", True, parent_cluster if parent_cluster != 0 else 0, 0, dt)

        cluster_offset = self.data_area_start + (new_cluster - 2) * self.cluster_size
        self.disk_manager.write_bytes(cluster_offset, dot_entry)
        self.disk_manager.write_bytes(cluster_offset + 32, dotdot_entry)

        # Write the new directory entry in the parent directory
        self.disk_manager.write_bytes(free_offset,
                                     self.create_dir_entry(new_dir_name.upper(), True, new_cluster, 0, dt))

        # Flush changes to disk
        self.disk_manager.flush(progress_callback=progress_callback)

    def delete_item(self, path, progress_callback=None):
        """Delete a file or directory."""
        entry_info = self.find_entry(path)
        if entry_info is None:
            raise ValueError("Entry not found")

        _, offset, entry = entry_info

        # Check if directory is empty
        if entry['is_dir'] and not self.is_directory_empty(entry['starting_cluster']):
            raise ValueError("Directory not empty")

        # Mark entry as deleted
        current_entry = self.disk_manager.read_bytes(offset, 32)
        self.disk_manager.write_bytes(offset, b'\xE5' + current_entry[1:])

        # Free the cluster chain
        self.free_cluster_chain(entry['starting_cluster'])

        # Flush changes to disk
        self.disk_manager.flush(progress_callback=progress_callback)

    # File operations
    def extract_file(self, path):
        """Extract a file's contents by path."""
        entry_info = self.find_entry(path)
        if not entry_info or entry_info[2]['is_dir']:
            raise ValueError("File not found or is a directory")

        _, _, entry = entry_info

        # Read cluster chain
        if entry['starting_cluster'] < 2:
            return b''  # Empty file

        cluster_chain = self.get_cluster_chain(entry['starting_cluster'])
        file_data = self.get_cluster_data(cluster_chain)

        # Trim to actual file size
        return file_data[:entry['size']]

    def insert_file(self, parent_path, file_name, file_data, dt=None, progress_callback=None):
        """Insert a file into the filesystem."""
        if not self.is_valid_83_name(file_name):
            raise ValueError("Invalid 8.3 name")

        parent_cluster = self.find_directory_cluster(parent_path)
        if parent_cluster is None:
            raise ValueError("Parent directory not found")

        # Check if name already exists
        if any(e['name'].upper() == file_name.upper()
               for e, _ in self.scan_directory(parent_cluster, parent_cluster == 0)):
            raise ValueError("Name already exists")

        free_offset = self.find_free_entry_offset(parent_cluster)
        if free_offset is None:
            raise ValueError("No space in parent directory")

        # Calculate needed clusters
        num_needed = math.ceil(len(file_data) / self.cluster_size)
        if num_needed == 0:
            num_needed = 1  # Always allocate at least one cluster

        # Allocate clusters
        clusters = self.allocate_cluster_chain(num_needed)
        if clusters is None:
            raise ValueError("Not enough free clusters")

        # Write file data to clusters
        self.write_cluster_data(clusters, file_data)

        # Create directory entry
        if dt is None:
            dt = datetime.datetime.now()

        self.disk_manager.write_bytes(free_offset,
                                     self.create_dir_entry(file_name.upper(), False, clusters[0], len(file_data), dt))

        # Flush changes to disk
        self.disk_manager.flush(progress_callback=progress_callback)

    # Validation functions
    def is_valid_83_name(self, name):
        """Check if a filename is valid 8.3 format."""
        parts = name.split('.')
        if len(parts) > 2 or not parts[0]:
            return False
        if len(parts[0]) > 8 or (len(parts) == 2 and len(parts[1]) > 3):
            return False
        return True
