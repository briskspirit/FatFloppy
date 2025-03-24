from types import SimpleNamespace
from abc import ABC, abstractmethod
from greaseweazle.codec import codec
from greaseweazle.tools import util, read
from greaseweazle.codec.ibm import ibm


class DiskManager(ABC):
    """Abstract base class for disk management."""
    @abstractmethod
    def read_bytes(self, offset, length):
        pass

    @abstractmethod
    def write_bytes(self, offset, data):
        pass

    @abstractmethod
    def flush(self):
        pass

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
        self.tracks = tracks if tracks else util.TrackSet('c=0-79:h=0-1')
        self.usb = util.usb_open(device_name)
        self.drive_obj = util.Drive()(drive)

        if format_params:
            self.fmt_cls = self.create_custom_diskdef(format_params)
        else:
            self.fmt_cls = codec.get_diskdef(format_name)
            print(dir(self.fmt_cls))
            print(self.fmt_cls.cyls)
            print(self.fmt_cls.heads)
            print(self.fmt_cls.track_map[(0, 0)].rpm)
            print(self.fmt_cls.track_map[(0, 0)].rate)
            print(dir(self.fmt_cls.track_map[(0, 0)]))
        # if self.fmt_cls is None:
            # raise ValueError(f"Format '{format_name}' not recognized.")
        self.track_data = {}
        self.dirty_tracks = set()
        self.sectors_per_track = None
        self.num_heads = None
        self.num_cylinders = None
        self.sector_size = None

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
        return disk_def

    def set_geometry(self, sectors_per_track, num_heads, num_cylinders, sector_size):
        """Set disk geometry."""
        self.sectors_per_track = sectors_per_track
        self.num_heads = num_heads
        self.num_cylinders = num_cylinders
        self.sector_size = sector_size

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
                for t in args.tracks:
                    flux, dat = read.read_with_retry(self.usb, args, t)
                    sectors = dat.track.sectors if hasattr(dat, 'track') else dat.sectors
                    # print(dir(dat))
                    # print(dir(dat.track))
                    # print(dat.track.iams)
                    # print(dat.nsec)
                    # print(dat.track.mode)
                    # print(dat.track.gapbyte)
                    # print(dat.track.gap_presync)
                    # print(dat.track.head)
                    # print(len(dat.track.sectors[0].dam.data))
                    if dat is not None:
                        # Store sector data with their IDs
                        sector_data = {s.idam.r: bytes(s.dam.data) for s in sectors if s.dam.data}
                        self.track_data[track_id] = sector_data
                        break
                    else:
                        raise ValueError(f"Failed to read track {cyl}.{head}")
            util.with_drive_selected(read_track_wrapper, self.usb, self.drive_obj)
        return self.track_data[track_id]

    def write_track(self, cyl, head, data):
        """Mark a track as dirty and store its data."""
        track_id = (cyl, head)
        self.track_data[track_id] = data  # Data is a dict of {sector_id: bytes}
        self.dirty_tracks.add(track_id)

    def flush(self):
        """Write all dirty tracks back to the disk using Greaseweazle."""
        if not self.dirty_tracks:
            print("No dirty tracks to write.")
            return

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

        def write_tracks():
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
        track_def = self.fmt_cls.track_map[(cyl, head)]
        track = track_def.mk_track(cyl, head)  # Creates an IBMTrack_Fixed instance
        track_data = self.track_data[(cyl, head)]

        # Set sector data from stored track_data
        # TODO: fix for IBMTrack_Scan when using ibm.scan instead of ibm.1440
        for s in track.sectors:
            if s.idam.r in track_data:
                s.dam.data = bytearray(track_data[s.idam.r])

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

        # Debug: Verify total duration
        total_time = sum(wflux_list) / self.usb.sample_freq
        print(f"Flux for C{cyl}H{head}: {total_time:.3f}s (target: {drive_ticks_per_rev / self.usb.sample_freq:.3f}s)")

        return wflux_list

    def convert_to_flux(self, cyl, head, drive_ticks_per_rev):
        """Convert track data to flux list for writing, adjusted for drive RPM."""
        # Get the track definition from the disk format
        track_def = self.fmt_cls.track_map.get((cyl, head))
        if track_def is None:
            raise ValueError(f"No track definition found for cylinder {cyl}, head {head}")

        track = track_def.mk_track(cyl, head)
        track_data = self.track_data[(cyl, head)]

        # Handle different track types
        if isinstance(track, ibm.IBMTrack_Scan):
            # For IBMTrack_Scan, get the actual track instance
            if hasattr(track, 'track') and not isinstance(track.track, ibm.IBMTrack_Empty):
                track = track.track
            else:
                # If no actual track data is available yet, force a read
                flux, dat = read.read_with_retry(self.usb, SimpleNamespace(
                    revs=2, raw=False, fmt_cls=self.fmt_cls,
                    tracks=util.TrackSet(f'c={cyl}:h={head}'),
                    retries=3, seek_retries=0, reverse=False,
                    adjust_speed=None, fake_index=None, hard_sectors=False,
                    drive=self.drive_obj, ticks=0, drive_ticks_per_rev=None
                ), util.TrackSet.TrackIter(util.TrackSet(f'c={cyl}:h={head}')))

                if dat is not None and not isinstance(dat, ibm.IBMTrack_Empty):
                    track = dat
                else:
                    # If we still don't have track data, create a basic format
                    from greaseweazle.codec.ibm.ibm import IBMTrack_FixedDef
                    basic_def = IBMTrack_FixedDef('ibm.mfm')
                    basic_def.secs = self.sectors_per_track
                    basic_def.sz = [2]  # 512 bytes per sector
                    basic_def.finalise()
                    track = basic_def.mk_track(cyl, head)

        # Set sector data from stored track_data
        for s in track.sectors:
            if s.idam.r in track_data:
                s.dam.data = bytearray(track_data[s.idam.r])

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

    def calculate_track_and_offset(self, byte_offset):
        """Map byte offset to cylinder, head, and track offset."""
        if not all([self.sectors_per_track, self.num_heads, self.sector_size]):
            self.sectors_per_track = 18
            self.num_heads = 2
            self.sector_size = 512
            # raise ValueError("Disk geometry not set.")
        bytes_per_track = self.sectors_per_track * self.sector_size
        track_num = byte_offset // bytes_per_track
        cyl = track_num // self.num_heads
        head = track_num % self.num_heads
        offset_in_track = byte_offset % bytes_per_track
        return cyl, head, offset_in_track

    def read_bytes(self, offset, length):
        """Read arbitrary byte range from the disk."""
        if not all([self.sectors_per_track, self.num_heads, self.sector_size]):
            self.sectors_per_track = 18
            self.num_heads = 2
            self.sector_size = 512
        bytes_per_track = self.sectors_per_track * self.sector_size
        track_num = offset // bytes_per_track
        cyl = track_num // self.num_heads
        head = track_num % self.num_heads
        offset_in_track = offset % bytes_per_track
        sector_num = offset_in_track // self.sector_size + 1  # Sector IDs start from 1
        offset_in_sector = offset_in_track % self.sector_size

        data = b''
        while length > 0:
            track_id = (cyl, head)
            if track_id not in self.track_data:
                self.read_track(cyl, head)
            sector_data = self.track_data[track_id].get(sector_num, b'\x00' * self.sector_size)
            chunk = sector_data[offset_in_sector:offset_in_sector + length]
            data += chunk
            length -= len(chunk)
            offset_in_sector = 0
            sector_num += 1
            if sector_num > self.sectors_per_track:
                sector_num = 1
                track_num += 1
                cyl = track_num // self.num_heads
                head = track_num % self.num_heads
        return data

    def write_bytes(self, offset, data):
        """Write arbitrary byte range to the disk."""
        if not all([self.sectors_per_track, self.num_heads, self.sector_size]):
            self.sectors_per_track = 18
            self.num_heads = 2
            self.sector_size = 512
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
                self.read_track(cyl, head)
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
        self.file_path = file_path
        if image_data is not None:
            self.image_data = bytearray(image_data)
            self.dirty = True
        else:
            with open(file_path, 'rb') as f:
                self.image_data = bytearray(f.read())
            self.dirty = False

    def read_bytes(self, offset, length):
        """Read byte range from the image."""
        return self.image_data[offset:offset + length]

    def write_bytes(self, offset, data):
        """Write byte range to the image and mark as dirty."""
        self.image_data[offset:offset + len(data)] = data
        self.dirty = True

    def flush(self):
        """Write the image back to the file if modified."""
        if self.dirty:
            with open(self.file_path, 'wb') as f:
                f.write(self.image_data)
            self.dirty = False

class MemoryDiskManager(DiskManager):
    """A simple DiskManager implementation that operates on an in-memory bytearray."""
    def __init__(self, image_data):
        """
        Initialize with a bytearray representing the disk image.

        Args:
            image_data (bytearray): The disk image data.
        """
        self.image_data = image_data

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
