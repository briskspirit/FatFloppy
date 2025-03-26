import struct
from abc import ABC, abstractmethod
from types import SimpleNamespace

from greaseweazle.codec import codec
from greaseweazle.codec.ibm import ibm
from greaseweazle.tools import read, util


class DiskManager(ABC):
    """Abstract base class for disk management."""
    def __init__(self):
        # Initialize geometry attributes with default values
        self.sectors_per_track = None
        self.num_heads = None
        self.num_cylinders = None
        self.sector_size = None
        self.total_sectors = None

    @property
    def is_dirty(self):
        return False

    @abstractmethod
    def read_bytes(self, offset, length, progress_callback=None):
        pass

    @abstractmethod
    def write_bytes(self, offset, data):
        pass

    @abstractmethod
    def flush(self, progress_callback=None):
        pass

    def ensure_geometry(self):
        """Ensure geometry parameters are set with at least default values."""
        if not self.sectors_per_track or self.sectors_per_track <= 0:
            self.sectors_per_track = 18  # Default for 1.44MB floppy
        if not self.num_heads or self.num_heads <= 0:
            self.num_heads = 2  # Default for 1.44MB floppy
        if not self.num_cylinders or self.num_cylinders <= 0:
            self.num_cylinders = 80  # Default for 1.44MB floppy
        if not self.sector_size or self.sector_size <= 0:
            self.sector_size = 512  # Default for most floppies

        # Calculate total sectors if not set
        if not self.total_sectors or self.total_sectors <= 0:
            self.total_sectors = self.sectors_per_track * self.num_heads * self.num_cylinders

    def read_bpb_geometry(self):
        """Read BPB to get disk geometry."""
        try:
            # Read boot sector
            boot_sector = self.read_bytes(0, 512)

            # Parse BPB fields
            self.sector_size = struct.unpack_from('<H', boot_sector, 0x0B)[0]
            self.sectors_per_track = struct.unpack_from('<H', boot_sector, 0x18)[0]
            self.num_heads = struct.unpack_from('<H', boot_sector, 0x1A)[0]

            # Get total sectors
            total_sectors = struct.unpack_from('<H', boot_sector, 0x13)[0]
            if total_sectors == 0:
                total_sectors = struct.unpack_from('<I', boot_sector, 0x20)[0]

            self.total_sectors = total_sectors

            # Calculate number of cylinders
            if self.sectors_per_track and self.num_heads and self.total_sectors:
                self.num_cylinders = self.total_sectors // (self.sectors_per_track * self.num_heads)

            print(f"BPB geometry: {self.sectors_per_track} sectors/track, {self.num_heads} heads, {self.num_cylinders} cylinders, {self.sector_size} bytes/sector")
            return True
        except Exception as e:
            print(f"Warning: Error reading BPB geometry: {e}")
            return False

    def calculate_track_and_offset(self, byte_offset):
        """Map byte offset to cylinder, head, and track offset."""
        self.ensure_geometry()

        bytes_per_track = self.sectors_per_track * self.sector_size
        track_num = byte_offset // bytes_per_track
        cyl = track_num // self.num_heads
        head = track_num % self.num_heads
        offset_in_track = byte_offset % bytes_per_track
        return cyl, head, offset_in_track

class FloppyDiskManager(DiskManager):
    def __init__(self, device_name=None, drive='A', format_name='ibm.scan', format_params=None, tracks=None):
        """
        Initialize the FloppyDiskManager for Greaseweazle.

        Args:
            device_name (str): Path to the Greaseweazle device (e.g., 'COM3').
            drive (str): Drive identifier (default 'A').
            format_name (str): Disk format (e.g., 'ibm.1440').
            tracks (TrackSet, optional): Tracks to manage; defaults to 80 cyl, 2 heads.
        """
        super().__init__()
        self.tracks = tracks if tracks else util.TrackSet('c=0-79:h=0-1')
        self.usb = util.usb_open(device_name)
        self.drive_obj = util.Drive()(drive)
        self.format_name = format_name

        if format_params:
            self.fmt_cls = self.create_custom_diskdef(format_params)
        else:
            self.fmt_cls = codec.get_diskdef(format_name)

        # Initialize track data storage
        self.track_data = {}
        self.dirty_tracks = set()

        # Initialize geometry based on format class if available
        self.init_geometry_from_format()

    @property
    def is_dirty(self):
        return len(self.dirty_tracks) > 0

    def init_geometry_from_format(self):
        """Initialize disk geometry from format class or read from disk."""
        try:
            if self.format_name == 'ibm.scan':
                # For ibm.scan, we need to be more aggressive about detecting format
                self.read_and_detect_format()

                # Always try to read the BPB as well
                self.read_bpb_geometry()

                # If we still don't have complete geometry, get from disk
                if not self.has_complete_geometry():
                    self.detect_geometry_from_disk()
            else:
                # For specific format, get geometry from format definition
                self.init_geometry_from_fmt_cls()

                # For completeness, also try to read BPB
                if not self.has_complete_geometry():
                    self.read_bpb_geometry()
        except Exception as e:
            print(f"Warning: Could not initialize geometry from format: {e}")

        # Ensure we have values for all geometry properties
        self.ensure_geometry()

    def has_complete_geometry(self):
        """Check if we have complete geometry information."""
        return (self.sectors_per_track and self.num_heads and
                self.num_cylinders and self.sector_size)

    def read_and_detect_format(self):
        """Read track 0 and detect format, then set geometry accordingly."""
        try:
            # Create temporary args for reading track 0
            args = SimpleNamespace(
                revs=2, raw=False, fmt_cls=self.fmt_cls,
                tracks=util.TrackSet('c=0:h=0'),
                retries=3, seek_retries=0, reverse=False,
                adjust_speed=None, fake_index=None, hard_sectors=False,
                drive=self.drive_obj, ticks=0, drive_ticks_per_rev=None
            )

            # Function to read track 0
            def read_track_zero():
                for t in args.tracks:
                    _, dat = read.read_with_retry(self.usb, args, t)
                    if dat is not None:
                        # Try to get track information
                        track = None
                        if hasattr(dat, 'track'):
                            track = dat.track
                        else:
                            track = dat

                        # Extract sector information if available
                        if hasattr(track, 'sectors') and track.sectors:
                            sectors = track.sectors
                            if sectors:
                                # Get sector details
                                self.sector_size = len(sectors[0].dam.data) if hasattr(sectors[0].dam, 'data') else 512
                                self.sectors_per_track = len(sectors)
                                self.num_heads = 2  # Assume double-sided
                                self.num_cylinders = 80  # Assume 80 tracks
                                print(f"Detected from track 0: {self.sectors_per_track} sectors, {self.sector_size} bytes per sector")

                        # Try to extract more info from track properties
                        if hasattr(track, 'mode'):
                            if track.mode.name == 'MFM':
                                print("Detected MFM encoding")
                                if not self.sectors_per_track:
                                    # MFM typical values
                                    if self.sector_size == 512:
                                        # High-density
                                        self.sectors_per_track = 18
                                        self.num_cylinders = 80
                                        self.num_heads = 2
                            elif track.mode.name == 'FM':
                                print("Detected FM encoding")
                                if not self.sectors_per_track:
                                    # FM typical values
                                    self.sectors_per_track = 9
                                    self.num_cylinders = 40
                                    self.num_heads = 2

            # Read track 0 to get format information
            util.with_drive_selected(read_track_zero, self.usb, self.drive_obj)

        except Exception as e:
            print(f"Warning: Error detecting format from track 0: {e}")

    def detect_geometry_from_disk(self):
        """Attempt to detect disk geometry by reading multiple tracks."""
        print("Attempting to detect disk geometry from disk...")
        try:
            # First, try to detect sectors per track by reading track 0 on both heads
            max_sectors = 0
            for head in [0, 1]:
                args = SimpleNamespace(
                    revs=2, raw=False, fmt_cls=self.fmt_cls,
                    tracks=util.TrackSet(f'c=0:h={head}'),
                    retries=1, seek_retries=0, reverse=False,
                    adjust_speed=None, fake_index=None, hard_sectors=False,
                    drive=self.drive_obj, ticks=0, drive_ticks_per_rev=None
                )

                def read_track():
                    for t in args.tracks:
                        _, dat = read.read_with_retry(self.usb, args, t)
                        if dat is not None:
                            track = getattr(dat, 'track', dat)
                            if hasattr(track, 'sectors'):
                                nonlocal max_sectors
                                max_sectors = max(max_sectors, len(track.sectors))

                try:
                    util.with_drive_selected(read_track, self.usb, self.drive_obj)
                except Exception:
                    # Continue with next head if this one fails
                    pass

            if max_sectors > 0:
                self.sectors_per_track = max_sectors
                print(f"Detected {max_sectors} sectors per track")

            # Then try to detect number of cylinders by seeking until we hit a limit
            if not self.num_cylinders:
                max_cylinder = 40  # Start with a safe default

                def test_seek():
                    nonlocal max_cylinder
                    for test_cyl in [40, 80]:
                        try:
                            self.usb.seek(test_cyl, 0)
                            max_cylinder = test_cyl
                        except Exception:
                            break

                try:
                    util.with_drive_selected(test_seek, self.usb, self.drive_obj)
                    self.num_cylinders = max_cylinder + 1
                    print(f"Detected {self.num_cylinders} cylinders")
                except Exception as e:
                    print(f"Error detecting cylinders: {e}")

            # Detect number of heads
            if not self.num_heads:
                has_head1 = False

                def test_head1():
                    nonlocal has_head1
                    try:
                        self.usb.seek(0, 1)
                        has_head1 = True
                    except Exception:
                        pass

                try:
                    util.with_drive_selected(test_head1, self.usb, self.drive_obj)
                    self.num_heads = 2 if has_head1 else 1
                    print(f"Detected {self.num_heads} heads")
                except Exception as e:
                    print(f"Error detecting heads: {e}")

        except Exception as e:
            print(f"Error detecting geometry from disk: {e}")

    def init_geometry_from_fmt_cls(self):
        """Initialize geometry from format class."""
        if not self.fmt_cls or not hasattr(self.fmt_cls, 'tracks'):
            return

        try:
            # Get cylinder and head counts
            self.num_cylinders = self.fmt_cls.cyls
            self.num_heads = self.fmt_cls.heads

            # Get first track definition to determine sector size and count
            for track_coord, track_def in self.fmt_cls.track_map.items():
                if hasattr(track_def, 'secs'):
                    self.sectors_per_track = track_def.secs
                if hasattr(track_def, 'sz') and track_def.sz:
                    # Convert sector size code to bytes (128 << n)
                    n = track_def.sz[0] if isinstance(track_def.sz, list) else track_def.sz
                    self.sector_size = 128 << n
                break

            # Calculate total sectors
            if self.sectors_per_track and self.num_heads and self.num_cylinders:
                self.total_sectors = self.sectors_per_track * self.num_heads * self.num_cylinders
        except Exception as e:
            print(f"Warning: Error initializing geometry from format: {e}")

    def create_custom_diskdef(self, params):
        """
        Create a custom DiskDef object with specified parameters.

        Args:
            params (dict): Dictionary containing disk and track parameters.
                - cyls (int): Number of cylinders
                - heads (int): Number of heads
                - format_name (str): Base format (e.g., 'ibm.mfm' or 'ibm.fm')
                - track_params (dict): Track-specific parameters (e.g., secs, bps, gap3, rate)

        Returns:
            codec.DiskDef: Configured DiskDef object
        """
        # Create DiskDef instance
        disk_def = codec.DiskDef()
        disk_def.cyls = params['cyls']
        disk_def.heads = params['heads']

        # Create TrackDef instance with a recognized format name
        track_def = ibm.IBMTrack_FixedDef(params['format_name'])  # e.g., 'ibm.mfm'

        # Set track parameters
        for key, val in params['track_params'].items():
            track_def.add_param(key, str(val))  # Convert values to strings as required

        # Finalize TrackDef
        track_def.finalise()

        # Assign TrackDef to all tracks in the disk
        for c in range(disk_def.cyls):
            for h in range(disk_def.heads):
                disk_def.track_map[(c, h)] = track_def

        # Finalize DiskDef
        disk_def.finalise()

        # Set geometry attributes based on params
        self.num_cylinders = params['cyls']
        self.num_heads = params['heads']
        if 'sector_size' in params:
            self.sector_size = params['sector_size']
        elif 'track_params' in params and 'bps' in params['track_params']:
            self.sector_size = params['track_params']['bps']

        if 'track_params' in params and 'secs' in params['track_params']:
            self.sectors_per_track = params['track_params']['secs']

        # Calculate total sectors
        self.ensure_geometry()

        return disk_def

    def set_geometry(self, sectors_per_track, num_heads, num_cylinders, sector_size):
        """Set disk geometry."""
        self.sectors_per_track = sectors_per_track
        self.num_heads = num_heads
        self.num_cylinders = num_cylinders
        self.sector_size = sector_size
        self.total_sectors = sectors_per_track * num_heads * num_cylinders

    def read_track(self, cyl, head):
        """Read a track from the disk if not cached."""
        track_id = (cyl, head)
        if track_id not in self.track_data:
            args = SimpleNamespace(
                revs=2, raw=False, fmt_cls=self.fmt_cls,
                tracks=util.TrackSet(f'c={cyl}:h={head}'),
                retries=3, seek_retries=0, reverse=False,
                adjust_speed=None, fake_index=None, hard_sectors=False,
                drive=self.drive_obj, ticks=0, drive_ticks_per_rev=None
            )
            def read_track_wrapper():
                track_iterator = util.TrackSet.TrackIter(args.tracks)
                next(track_iterator)  # Initialize the iterator
                flux, dat = read.read_with_retry(self.usb, args, track_iterator)

                sectors = None
                if hasattr(dat, 'track') and hasattr(dat.track, 'sectors'):
                    sectors = dat.track.sectors
                elif hasattr(dat, 'sectors'):
                    sectors = dat.sectors

                if dat is not None and sectors:
                    # Store sector data with their IDs
                    sector_data = {s.idam.r: bytes(s.dam.data) for s in sectors if hasattr(s, 'dam') and hasattr(s.dam, 'data')}
                    self.track_data[track_id] = sector_data

                    # Update geometry information if not set
                    if not self.sectors_per_track or self.sectors_per_track < len(sectors):
                        self.sectors_per_track = len(sectors)
                        # If we found more sectors, recalculate total_sectors
                        if self.num_heads and self.num_cylinders:
                            self.total_sectors = self.sectors_per_track * self.num_heads * self.num_cylinders

                    if not self.sector_size and sectors:
                        self.sector_size = len(sectors[0].dam.data) if hasattr(sectors[0], 'dam') and hasattr(sectors[0].dam, 'data') else 512
                else:
                    raise ValueError(f"Failed to read track {cyl}.{head}")

            util.with_drive_selected(read_track_wrapper, self.usb, self.drive_obj)

        return self.track_data[track_id]

    def write_track(self, cyl, head, data):
        """Mark a track as dirty and store its data."""
        track_id = (cyl, head)
        self.track_data[track_id] = data  # Data is a dict of {sector_id: bytes}
        self.dirty_tracks.add(track_id)

    def flush(self, progress_callback=None):
        """Write all dirty tracks back to the disk using Greaseweazle."""
        if not self.dirty_tracks:
            print("No dirty tracks to write.")
            return

        # Check if writing is supported for the current codec
        if self.format_name == 'ibm.scan':
            print("Warning: Writing with ibm.scan codec may be unreliable.")
            # Consider switching to a fixed format for writing

        # Measure drive RPM once if not already set, with drive selected
        if not hasattr(self, 'drive_ticks_per_rev'):
            def measure_rpm():
                flux = self.usb.read_track(2)
                self.drive_ticks_per_rev = flux.ticks_per_rev
                print(f"Measured drive RPM: {60 / (self.drive_ticks_per_rev / self.usb.sample_freq):.1f}")

            try:
                # Ensure drive is selected and motor is on before measuring RPM
                util.with_drive_selected(measure_rpm, self.usb, self.drive_obj)
            except Exception as e:
                print("Failed to measure RPM:", e)
                raise

        total_tracks = len(self.dirty_tracks)
        tracks_written = 0

        def write_tracks():
            nonlocal tracks_written
            for cyl, head in sorted(self.dirty_tracks):
                print(f"Writing track {cyl}.{head}")
                # Seek to the track
                self.usb.seek(cyl, head)
                # Convert track data to flux
                flux_list = self.convert_to_flux(cyl, head, self.drive_ticks_per_rev)
                # Write the track
                self.usb.write_track(
                    flux_list=flux_list,
                    cue_at_index=True,
                    terminate_at_index=True
                )
                tracks_written += 1
                if progress_callback:
                    progress_callback(tracks_written / total_tracks)
            # Clear dirty tracks after successful write
            self.dirty_tracks.clear()

        try:
            util.with_drive_selected(write_tracks, self.usb, self.drive_obj)
            print("Write operation completed successfully.")
        except Exception as e:
            print(f"Error writing to disk: {e}")
            raise

    def convert_to_flux(self, cyl, head, drive_ticks_per_rev):
        """Convert track data to flux list for writing, adjusted for drive RPM."""
        # Get the track definition from the disk format
        track_def = self.fmt_cls.track_map.get((cyl, head))
        if track_def is None:
            # Try to find a default track definition
            for key, value in self.fmt_cls.track_map.items():
                track_def = value
                break
            if track_def is None:
                raise ValueError(f"No track definition found for cylinder {cyl}, head {head}")

        # Create a track object from the definition
        track = None

        # For ibm.scan tracks, we need special handling
        if isinstance(track_def, ibm.IBMTrack_ScanDef):
            # Try to use a fixed format instead for writing
            fixed_def = ibm.IBMTrack_FixedDef('ibm.mfm')
            fixed_def.secs = self.sectors_per_track or 18
            fixed_def.sz = [2]  # 512 bytes per sector
            fixed_def.finalise()
            track = fixed_def.mk_track(cyl, head)
        else:
            track = track_def.mk_track(cyl, head)

        # Get the track data to write
        track_data = self.track_data[(cyl, head)]

        # Set sector data in the track
        for s in track.sectors:
            if s.idam.r in track_data:
                s.dam.data = bytearray(track_data[s.idam.r])
                # Clear CRC error flags
                s.crc = s.idam.crc = s.dam.crc = 0

        # Generate MasterTrack
        master_track = track.master_track()

        # Set time_per_rev to match the drive's measured RPM
        master_track.time_per_rev = drive_ticks_per_rev / self.usb.sample_freq

        # Generate flux for writeout
        wflux = master_track.flux_for_writeout(cue_at_index=True)

        # Scale flux list to match drive_ticks_per_rev and convert to integers
        factor = drive_ticks_per_rev / wflux.ticks_to_index
        rem = 0.0
        wflux_list = []
        for x in wflux.list:
            y = x * factor + rem
            val = round(y)
            rem = y - val
            wflux_list.append(val)

        return wflux_list

    def read_bytes(self, offset, length, progress_callback=None):
        """Read arbitrary byte range from the disk."""
        self.ensure_geometry()

        bytes_per_track = self.sectors_per_track * self.sector_size
        start_track_num = offset // bytes_per_track
        end_track_num = (offset + length - 1) // bytes_per_track
        tracks_to_read = set()

        # Identify all unique tracks needed
        for track_num in range(start_track_num, end_track_num + 1):
            cyl = track_num // self.num_heads
            head = track_num % self.num_heads
            tracks_to_read.add((cyl, head))

        total_tracks = len(tracks_to_read)
        tracks_read = 0

        # Read all necessary tracks first, reporting progress
        for track_id in tracks_to_read:
            if track_id not in self.track_data:
                try:
                    self.read_track(*track_id)
                    tracks_read += 1
                    if progress_callback:
                        progress_callback(tracks_read / total_tracks)
                except Exception as e:
                    print(f"Error reading track {track_id}: {e}")
                    # Fill with zeros if track read fails during assembly

        # Assemble the bytes
        data = b''
        current_offset = offset
        remaining_length = length
        while remaining_length > 0:
            track_num = current_offset // bytes_per_track
            cyl = track_num // self.num_heads
            head = track_num % self.num_heads
            offset_in_track = current_offset % bytes_per_track
            sector_num = offset_in_track // self.sector_size + 1  # Sector IDs start from 1
            offset_in_sector = offset_in_track % self.sector_size

            track_id = (cyl, head)
            if track_id in self.track_data and sector_num in self.track_data[track_id]:
                sector_data = self.track_data[track_id][sector_num]
            else:
                sector_data = b'\x00' * self.sector_size  # Use zeros if sector unavailable

            chunk_size = min(remaining_length, self.sector_size - offset_in_sector)
            data += sector_data[offset_in_sector:offset_in_sector + chunk_size]
            remaining_length -= chunk_size
            current_offset += chunk_size

        return data

    def write_bytes(self, offset, data):
        """Write arbitrary byte range to the disk."""
        self.ensure_geometry()

        bytes_per_track = self.sectors_per_track * self.sector_size
        track_num = offset // bytes_per_track
        cyl = track_num // self.num_heads
        head = track_num % self.num_heads
        offset_in_track = offset % bytes_per_track
        sector_num = offset_in_track // self.sector_size + 1  # Sector IDs start from 1
        offset_in_sector = offset_in_track % self.sector_size

        data = bytearray(data)
        while data:
            track_id = (cyl, head)
            if track_id not in self.track_data:
                try:
                    self.read_track(cyl, head)
                except Exception as e:
                    print(f"Error reading track {cyl}.{head}: {e}")
                    # Initialize track data with empty sectors if read failed
                    self.track_data[track_id] = {}

            # Ensure track_data exists for this track
            if track_id not in self.track_data:
                self.track_data[track_id] = {}

            # Ensure sector_data is a bytearray
            if sector_num in self.track_data[track_id]:
                sector_data = bytearray(self.track_data[track_id][sector_num])
            else:
                sector_data = bytearray(b'\x00' * self.sector_size)

            write_length = min(len(data), self.sector_size - offset_in_sector)
            sector_data[offset_in_sector:offset_in_sector + write_length] = data[:write_length]
            self.track_data[track_id][sector_num] = sector_data
            self.write_track(cyl, head, self.track_data[track_id])  # Mark as dirty

            data = data[write_length:]
            offset_in_sector = 0
            sector_num += 1
            if sector_num > self.sectors_per_track:
                sector_num = 1
                track_num += 1
                cyl = track_num // self.num_heads
                head = track_num % self.num_heads


class ImageFileManager(DiskManager):
    def __init__(self, file_path, image_data=None):
        """
        Initialize ImageFileManager for disk image files.

        Args:
            file_path (str): Path to the image file.
            image_data (bytearray, optional): Initial image data for new images.
        """
        super().__init__()
        self.file_path = file_path
        if image_data is not None:
            self.image_data = bytearray(image_data)
            self.dirty = True
        else:
            with open(file_path, 'rb') as f:
                self.image_data = bytearray(f.read())
            self.dirty = False

        # Try to infer geometry from image size and BPB if available
        self._infer_geometry()

    @property
    def is_dirty(self):
        return self.dirty

    def _infer_geometry(self):
        """Infer disk geometry from image size and boot sector if available."""
        # Common floppy formats by size
        formats = {
            163840: (8, 1, 40, 512),    # 160K - 8 sectors, 1 head, 40 tracks
            184320: (9, 1, 40, 512),    # 180K - 9 sectors, 1 head, 40 tracks
            327680: (8, 2, 40, 512),    # 320K - 8 sectors, 2 heads, 40 tracks
            368640: (9, 2, 40, 512),    # 360K - 9 sectors, 2 heads, 40 tracks
            737280: (9, 2, 80, 512),    # 720K - 9 sectors, 2 heads, 80 tracks
            1228800: (15, 2, 80, 512),  # 1.2M - 15 sectors, 2 heads, 80 tracks
            1474560: (18, 2, 80, 512),  # 1.44M - 18 sectors, 2 heads, 80 tracks
            1720320: (21, 2, 80, 512),  # 1.68M - 21 sectors, 2 heads, 80 tracks
            2949120: (36, 2, 80, 512),  # 2.88M - 36 sectors, 2 heads, 80 tracks
        }

        # First try to read from BPB
        if not self.read_bpb_geometry():
            # If BPB failed, try to infer from image size
            image_size = len(self.image_data)
            if image_size in formats:
                self.sectors_per_track, self.num_heads, self.num_cylinders, self.sector_size = formats[image_size]
                self.total_sectors = self.sectors_per_track * self.num_heads * self.num_cylinders
            else:
                # Default to 1.44MB floppy format
                self.sectors_per_track = 18
                self.num_heads = 2
                self.sector_size = 512
                self.num_cylinders = image_size // (self.sectors_per_track * self.num_heads * self.sector_size)
                self.total_sectors = self.sectors_per_track * self.num_heads * self.num_cylinders

    def read_bytes(self, offset, length, progress_callback=None):
        """Read byte range from the image."""
        data = self.image_data[offset:offset + length]
        if progress_callback:
            progress_callback(1.0)  # Operation is instantaneous
        return data

    def write_bytes(self, offset, data):
        """Write byte range to the image and mark as dirty."""
        self.image_data[offset:offset + len(data)] = data
        self.dirty = True

    def flush(self, progress_callback=None):
        """Write the image back to the file if modified."""
        if self.dirty:
            total_bytes = len(self.image_data)
            chunk_size = 1024 * 1024  # 1MB chunks
            with open(self.file_path, 'wb') as f:
                for i in range(0, total_bytes, chunk_size):
                    chunk = self.image_data[i:i + chunk_size]
                    f.write(chunk)
                    if progress_callback:
                        progress_callback(min(i + len(chunk), total_bytes) / total_bytes)
            self.dirty = False


class MemoryDiskManager(DiskManager):
    """A simple DiskManager implementation that operates on an in-memory bytearray."""
    def __init__(self, image_data):
        """
        Initialize with a bytearray representing the disk image.

        Args:
            image_data (bytearray): The disk image data.
        """
        super().__init__()
        self.image_data = image_data

    @property
    def is_dirty(self):
        return self.dirty

    def read_bytes(self, offset, length):
        """
        Read a specified number of bytes from the given offset.

        Args:
            offset (int): Starting position.
            length (int): Number of bytes to read.

        Returns:
            bytearray: The requested bytes.
        """
        return self.image_data[offset:offset + length]

    def write_bytes(self, offset, data):
        """
        Write data to the specified offset.

        Args:
            offset (int): Starting position.
            data (bytes or bytearray): Data to write.
        """
        self.image_data[offset:offset + len(data)] = data

    def flush(self):
        """Write the image back to the file if modified."""
        pass
