# drivers.py
from dataclasses import dataclass
from typing import List

from greaseweazle.tools import util
from greaseweazle import usb as USB
from greaseweazle.codec import codec
from greaseweazle.tools import read
from logging_config import get_logger

logger = get_logger()

@dataclass
class PhysicalFormat:
    encoding: str  # FM/MFM
    rate: int      # Data rate (kbps)
    rpm: int       # Rotations per minute
    gap3: int = 84 # Gap3 size
    cskew: int = 0 # Sector skew
    interleave: int = 1
    # Additional fields needed for disk geometry
    sectors_per_track: int = 18
    heads: int = 2
    sector_size: int = 512

class DiskIODriver:
    def __init__(self):
        self.logger = get_logger(self.__class__.__name__)

    def read_sector(self, cylinder: int, head: int, sector: int) -> bytes:
        raise NotImplementedError("Subclasses must implement read_sector")

    def write_sector(self, cylinder: int, head: int, sector: int, data: bytes) -> None:
        raise NotImplementedError("Subclasses must implement write_sector")

    def flush(self) -> None:
        raise NotImplementedError("Subclasses must implement flush")

    def set_physical_format(self, physical_format: PhysicalFormat) -> None:
        raise NotImplementedError("Subclasses must implement set_physical_format")

class GreaseweazleDriver(DiskIODriver):
    def __init__(self, device_name=None, drive='A', drive_size="3.5"):
        super().__init__()
        self.device_name = device_name
        self.drive = drive
        self.drive_size = drive_size
        self.physical_format = None
        self.dirty_sectors = {}
        self.dirty_tracks = set()
        self.track_data = {}       # Track sector cache
        self.sector_cache = {}     # Direct sector cache for faster lookups
        self.initialized = False
        self.fmt_cls = None
        self.drive_ticks_per_rev = None
        self.last_successful_format = None  # Store the last successful format
        self.using_custom_diskdef = False
        self.scan_track_object = None
        self.logger.debug(f"GreaseweazleDriver initialized with device={device_name}, drive={drive}, size={drive_size}\"")

    def initialize(self):
        if self.initialized:
            self.logger.debug("Driver already initialized, skipping")
            return

        self.logger.debug(f"Initializing GreaseweazleDriver with device={self.device_name}")
        self.usb = util.usb_open(self.device_name)
        self.drive_obj = util.Drive()(self.drive)

        # Measure drive RPM
        try:
            def measure_rpm():
                self.logger.debug("Measuring drive RPM")
                flux = self.usb.read_track(2)
                self.drive_ticks_per_rev = flux.ticks_per_rev
                rpm = 60 / (self.drive_ticks_per_rev / self.usb.sample_freq)
                self.logger.info(f"Drive RPM measured: {rpm:.1f}")

            util.with_drive_selected(measure_rpm, self.usb, self.drive_obj)
        except Exception as e:
            self.logger.warning(f"Failed to measure RPM: {e}")
            # Use a reasonable default
            self.logger.info("Using default RPM value")
            self.drive_ticks_per_rev = 0.2 * self.usb.sample_freq

        self.initialized = True
        self.logger.debug("GreaseweazleDriver initialization complete")

    def read_sector(self, cylinder: int, head: int, sector: int) -> bytes:
        self.initialize()

        # Check direct sector cache first for fastest access
        sector_key = (cylinder, head, sector)
        if sector_key in self.sector_cache:
            self.logger.debug(f"Sector cache hit for C:{cylinder} H:{head} S:{sector}")
            return self.sector_cache[sector_key]

        # Then check track cache
        track_id = (cylinder, head)
        if track_id not in self.track_data:
            self.logger.debug(f"Track cache miss for C:{cylinder} H:{head}, reading track")
            try:
                self._read_track(cylinder, head)
            except Exception as e:
                self.logger.error(f"Error reading track C:{cylinder} H:{head}: {e}")
                # Create empty track data to prevent future retries
                self.track_data[track_id] = {}
                self.logger.debug(f"Created empty track data for C:{cylinder} H:{head}")

        # After track read, check if sector is available
        if track_id in self.track_data and sector in self.track_data[track_id]:
            self.logger.debug(f"Found sector {sector} in track data for C:{cylinder} H:{head}")
            # Cache the sector directly for faster future access
            self.sector_cache[sector_key] = self.track_data[track_id][sector]
            return self.track_data[track_id][sector]

        # Sector not found - return empty sector
        sector_size = 512
        if self.physical_format:
            sector_size = self.physical_format.sector_size
        self.logger.warning(f"Sector not found: C:{cylinder} H:{head} S:{sector}, returning empty sector")
        return b'\x00' * sector_size

    def write_sector(self, cylinder: int, head: int, sector: int, data: bytes) -> None:
        self.initialize()
        track_id = (cylinder, head)

        self.logger.debug(f"Writing sector C:{cylinder} H:{head} S:{sector}, {len(data)} bytes")
        if track_id not in self.dirty_sectors:
            self.dirty_sectors[track_id] = {}

        self.dirty_sectors[track_id][sector] = data
        self.dirty_tracks.add(track_id)

        # Clear cache for this sector
        sector_key = (cylinder, head, sector)
        if sector_key in self.sector_cache:
            del self.sector_cache[sector_key]
            self.logger.debug(f"Cleared sector cache for C:{cylinder} H:{head} S:{sector}")

    def flush(self) -> None:
        if not self.initialized or not self.dirty_tracks:
            self.logger.debug("No dirty tracks to flush")
            return

        self.logger.info(f"Flushing {len(self.dirty_tracks)} dirty tracks")

        # Make sure fmt_cls is set before writing
        if not self.fmt_cls and self.physical_format:
            try:
                if self.physical_format.encoding == "MFM":
                    format_name = "ibm.mfm"
                else:
                    format_name = "ibm.fm"
                self.logger.debug(f"Getting disk definition for format: {format_name}")
                self.fmt_cls = codec.get_diskdef(format_name)
            except Exception as e:
                self.logger.error(f"Failed to get disk definition for writing: {e}")
                return

        def write_tracks():
            for track_id in sorted(self.dirty_tracks):
                cylinder, head = track_id
                self.logger.info(f"Writing track C:{cylinder} H:{head}")
                self.usb.seek(cylinder, head)
                try:
                    flux_list = self._convert_to_flux(cylinder, head)
                    self.usb.write_track(
                        flux_list=flux_list,
                        cue_at_index=True,
                        terminate_at_index=True
                    )
                    self.logger.info(f"Successfully wrote track C:{cylinder} H:{head}")
                except Exception as e:
                    self.logger.error(f"Error writing track C:{cylinder} H:{head}: {e}", exc_info=True)

        util.with_drive_selected(write_tracks, self.usb, self.drive_obj)
        self.dirty_tracks.clear()
        self.dirty_sectors.clear()
        self.logger.debug("Flush completed, cleared dirty tracks and sectors")

    def set_physical_format(self, physical_format: PhysicalFormat) -> None:
        self.logger.info(f"Setting physical format: {physical_format.encoding}, {physical_format.rate}kbps, "
                        f"{physical_format.sectors_per_track} sectors/track")
        self.physical_format = physical_format

        # Update fmt_cls based on the new physical format
        if self.initialized:
            try:
                if physical_format.encoding == "MFM":
                    format_name = "ibm.mfm"
                else:
                    format_name = "ibm.fm"
                self.logger.debug(f"Getting disk definition for format: {format_name}")
                self.fmt_cls = codec.get_diskdef(format_name)
            except Exception as e:
                self.logger.warning(f"Failed to get disk definition: {e}")

    def _create_and_set_custom_diskdef(self):
        """Create and set a custom disk definition based on detected parameters"""
        if not self.physical_format:
            self.logger.warning("Cannot create custom diskdef: physical format not set")
            return

        self.logger.debug("Creating custom disk definition")
        # Get geometry information from physical format
        params = {
            'cyls': 80,  # Assume 80 cylinders
            'heads': self.physical_format.heads,
            'sectors_per_track': self.physical_format.sectors_per_track,
            'sector_size': self.physical_format.sector_size,
            'encoding': self.physical_format.encoding,
            'rate': self.physical_format.rate,
            'gap3': self.physical_format.gap3
        }

        # Create the custom disk definition
        try:
            from greaseweazle.codec import codec
            from greaseweazle.codec.ibm import ibm

            disk_def = codec.DiskDef()
            disk_def.cyls = params['cyls']
            disk_def.heads = params['heads']

            if params['encoding'] == "MFM":
                format_name = "ibm.mfm"
            else:
                format_name = "ibm.fm"

            track_def = ibm.IBMTrack_FixedDef(format_name)

            # Set track parameters
            track_def.add_param("secs", str(params['sectors_per_track']))
            track_def.add_param("bps", str(params['sector_size']))
            track_def.add_param("gap3", str(params['gap3']))
            track_def.add_param("rate", str(params['rate']))

            # Finalize the track definition
            track_def.finalise()

            # Add the track definition to all cylinders and heads
            for c in range(disk_def.cyls):
                for h in range(disk_def.heads):
                    disk_def.track_map[(c, h)] = track_def

            # Finalize the disk definition
            disk_def.finalise()

            self.fmt_cls = disk_def
            self.using_custom_diskdef = True
            self.logger.info(f"Custom disk definition created: {params['sectors_per_track']} sectors, "
                          f"{params['sector_size']} bytes/sector, {params['encoding']} encoding")
        except Exception as e:
            self.logger.error(f"Failed to create custom disk definition: {e}", exc_info=True)

    def _read_track(self, cylinder: int, head: int) -> bool:
        """Read a track and detect its format"""
        self.logger.info(f"Reading track C:{cylinder} H:{head}")
        import types
        from greaseweazle.codec import codec
        from greaseweazle.codec.ibm import ibm

        # List of formats to try
        formats_to_try = []

        # If we have a custom disk definition or successful format, use it first
        if self.fmt_cls and self.using_custom_diskdef:
            self.logger.debug("Using custom disk definition first")
            formats_to_try.append(("custom", None))
        elif self.last_successful_format:
            self.logger.debug(f"Using last successful format first: {self.last_successful_format}")
            formats_to_try.append(self.last_successful_format)

        # Add ibm.scan as a reliable detector
        formats_to_try.append(("ibm.scan", None))

        # Only add other formats as fallbacks if we don't have a known good format
        if not self.last_successful_format and not (self.fmt_cls and self.using_custom_diskdef):
            if self.physical_format:
                if self.physical_format.encoding == "MFM":
                    rate = self.physical_format.rate
                    formats_to_try.append(("ibm.mfm", rate))
                else:
                    rate = self.physical_format.rate
                    formats_to_try.append(("ibm.fm", rate))

        # Initialize empty track data
        self.track_data[(cylinder, head)] = {}

        # Try each format until we find one that works
        for format_tuple in formats_to_try:
            if format_tuple[0] == "custom":
                fmt_cls = self.fmt_cls
                self.logger.debug("Trying custom disk definition")
            else:
                format_name, rate = format_tuple
                self.logger.debug(f"Trying format: {format_name}")
                try:
                    fmt_cls = codec.get_diskdef(format_name)
                except Exception as e:
                    self.logger.error(f"Failed to get disk definition for {format_name}: {e}")
                    continue

            args = types.SimpleNamespace(
                revs=3,
                raw=False,
                fmt_cls=fmt_cls,
                tracks=util.TrackSet(f'c={cylinder}:h={head}'),
                retries=2,
                seek_retries=0,
                reverse=False,
                adjust_speed=None,
                fake_index=None,
                hard_sectors=False,
                drive=self.drive_obj,
                ticks=0,
                drive_ticks_per_rev=self.drive_ticks_per_rev
            )

            success = False

            def read_track_wrapper():
                nonlocal success

                track_iterator = util.TrackSet.TrackIter(args.tracks)
                next(track_iterator)
                flux, dat = read.read_with_retry(self.usb, args, track_iterator)

                sectors = None
                if dat is not None:
                    # Store the track object for additional info extraction
                    self.scan_track_object = dat
                    self.logger.debug(f"Track data successfully read with format {format_tuple[0]}")

                    if hasattr(dat, 'track') and hasattr(dat.track, 'sectors'):
                        sectors = dat.track.sectors
                    elif hasattr(dat, 'sectors'):
                        sectors = dat.sectors

                if sectors:
                    sector_data = {
                        s.idam.r: bytes(s.dam.data)
                        for s in sectors
                        if hasattr(s, 'idam') and hasattr(s, 'dam') and hasattr(s.dam, 'data')
                    }

                    if sector_data:
                        num_sectors = len(sector_data)
                        self.logger.info(f"Found {num_sectors} sectors on track C:{cylinder} H:{head}")
                        # Use this data
                        self.track_data[(cylinder, head)] = sector_data
                        self.fmt_cls = fmt_cls

                        # Important: Update physical format with the actual detected sector count
                        if self.physical_format:
                            # Always update the physically detected sector count
                            if self.physical_format.sectors_per_track != num_sectors:
                                self.logger.info(f"Updating physical format with detected sector count: {num_sectors}")
                                self.physical_format.sectors_per_track = num_sectors

                        # Update physical format with detected track info
                        if format_tuple[0] == "ibm.scan" and hasattr(dat, 'track'):
                            track_obj = dat.track
                            if hasattr(track_obj, 'mode'):
                                mode = track_obj.mode
                                encoding = "MFM" if str(mode) == "IBM MFM" else "FM"

                                # Get rate from track_obj
                                if hasattr(track_obj, 'clock'):
                                    if encoding == "MFM":
                                        rate = int(1.0 / (track_obj.clock * 2000))
                                    else:
                                        rate = int(1.0 / (track_obj.clock * 1000))

                                    self.logger.debug(f"Detected encoding: {encoding}, rate: {rate}kbps")
                                    # Create or update physical format with detected values
                                    if self.physical_format is None:
                                        self.physical_format = PhysicalFormat(
                                            encoding=encoding,
                                            rate=rate,
                                            rpm=300,
                                            gap3=84,
                                            sectors_per_track=num_sectors,
                                            heads=2,
                                            sector_size=512
                                        )
                                    else:
                                        self.physical_format.encoding = encoding
                                        self.physical_format.rate = rate
                                        self.physical_format.sectors_per_track = num_sectors

                        # Remember this format for future use
                        if format_tuple[0] != "custom":
                            self.last_successful_format = format_tuple
                            self.logger.debug(f"Setting last successful format to {format_tuple}")

                        # Create a custom disk definition if we detected sectors and don't already have one
                        if not self.using_custom_diskdef and self.physical_format:
                            self._create_and_set_custom_diskdef()

                        success = True

            try:
                util.with_drive_selected(read_track_wrapper, self.usb, self.drive_obj)
                if success:
                    return True
            except Exception as e:
                self.logger.error(f"Error reading track with format {format_tuple[0]}: {e}")
                continue

        # If we couldn't read any sectors, log it
        if not self.track_data[(cylinder, head)]:
            self.logger.warning(f"No sectors found on track C:{cylinder} H:{head} after trying all formats")

        return False

    def _convert_to_flux(self, cylinder: int, head: int) -> List[int]:
        from greaseweazle.track import MasterTrack
        self.logger.debug(f"Converting track C:{cylinder} H:{head} to flux")

        if not self.fmt_cls:
            if self.physical_format.encoding == "MFM":
                format_name = "ibm.mfm"
            else:
                format_name = "ibm.fm"
            try:
                self.logger.debug(f"Getting disk definition for format: {format_name}")
                self.fmt_cls = codec.get_diskdef(format_name)
            except Exception as e:
                error_msg = f"Failed to get disk definition: {e}"
                self.logger.error(error_msg)
                raise ValueError(error_msg)

        track_def = None
        for key, value in self.fmt_cls.track_map.items():
            track_def = value
            break

        if not track_def:
            error_msg = "No track definition found"
            self.logger.error(error_msg)
            raise ValueError(error_msg)

        track = track_def.mk_track(cylinder, head)
        track_id = (cylinder, head)

        # Apply dirty sectors to the track
        if track_id in self.dirty_sectors:
            self.logger.debug(f"Applying {len(self.dirty_sectors[track_id])} dirty sectors to track")
            for s in track.sectors:
                if hasattr(s, 'idam') and hasattr(s.idam, 'r'):
                    sector_num = s.idam.r
                    if sector_num in self.dirty_sectors[track_id]:
                        s.dam.data = bytearray(self.dirty_sectors[track_id][sector_num])
                        s.crc = s.idam.crc = s.dam.crc = 0
                        self.logger.debug(f"Applied dirty sector {sector_num} to track")

        master_track = track.master_track()

        # Ensure we have drive_ticks_per_rev
        if not self.drive_ticks_per_rev:
            def measure_rpm():
                self.logger.debug("Measuring drive RPM (late initialization)")
                flux = self.usb.read_track(2)
                self.drive_ticks_per_rev = flux.ticks_per_rev
                rpm = 60 / (self.drive_ticks_per_rev / self.usb.sample_freq)
                self.logger.info(f"Drive RPM measured: {rpm:.1f}")

            util.with_drive_selected(measure_rpm, self.usb, self.drive_obj)

        master_track.time_per_rev = self.drive_ticks_per_rev / self.usb.sample_freq
        wflux = master_track.flux_for_writeout(cue_at_index=True)

        # Generate flux list
        factor = self.drive_ticks_per_rev / wflux.ticks_to_index
        rem = 0.0
        wflux_list = []
        for x in wflux.list:
            y = x * factor + rem
            val = round(y)
            rem = y - val
            wflux_list.append(val)

        self.logger.debug(f"Converted track to {len(wflux_list)} flux transitions")
        return wflux_list

class RawImageDriver(DiskIODriver):
    def __init__(self, file_path, image_data=None):
        super().__init__()
        self.file_path = file_path
        self.physical_format = None
        self.dirty = False
        self.geometry_set = False

        if image_data is not None:
            self.logger.debug(f"Initializing RawImageDriver with provided image data ({len(image_data)} bytes)")
            self.image_data = bytearray(image_data)
            self.dirty = True
        else:
            self.logger.debug(f"Loading image data from file: {file_path}")
            with open(file_path, 'rb') as f:
                self.image_data = bytearray(f.read())
            self.logger.info(f"Loaded {len(self.image_data)} bytes from {file_path}")

    def read_sector(self, cylinder: int, head: int, sector: int) -> bytes:
        # For the boot sector (and a few others), support reading without geometry
        if not self.geometry_set and cylinder == 0 and head == 0 and sector <= 3:
            # Use standard 512-byte sectors as a fallback
            sector_size = 512 if not self.physical_format else self.physical_format.sector_size
            offset = (sector - 1) * sector_size
            self.logger.debug(f"Reading early sector C:{cylinder} H:{head} S:{sector} without geometry")
            if offset + sector_size <= len(self.image_data):
                return bytes(self.image_data[offset:offset + sector_size])
            self.logger.warning(f"Sector beyond image size: C:{cylinder} H:{head} S:{sector}, returning empty sector")
            return b'\x00' * sector_size

        if not self.physical_format:
            error_msg = "Physical format not set"
            self.logger.error(error_msg)
            raise ValueError(error_msg)

        offset = self._calculate_sector_offset(cylinder, head, sector)
        self.logger.debug(f"Reading sector C:{cylinder} H:{head} S:{sector} from offset {offset}")
        if offset + self.physical_format.sector_size <= len(self.image_data):
            return bytes(self.image_data[offset:offset + self.physical_format.sector_size])
        self.logger.warning(f"Sector beyond image size: C:{cylinder} H:{head} S:{sector}, returning empty sector")
        return b'\x00' * self.physical_format.sector_size

    def write_sector(self, cylinder: int, head: int, sector: int, data: bytes) -> None:
        if not self.physical_format:
            error_msg = "Physical format not set"
            self.logger.error(error_msg)
            raise ValueError(error_msg)

        offset = self._calculate_sector_offset(cylinder, head, sector)
        self.logger.debug(f"Writing sector C:{cylinder} H:{head} S:{sector} to offset {offset}, {len(data)} bytes")

        # Ensure image_data is large enough
        if offset + len(data) > len(self.image_data):
            self.logger.info(f"Extending image size from {len(self.image_data)} to {offset + len(data)} bytes")
            self.image_data.extend(b'\x00' * (offset + len(data) - len(self.image_data)))

        self.image_data[offset:offset + len(data)] = data
        self.dirty = True

    def flush(self) -> None:
        if self.dirty:
            self.logger.info(f"Flushing changes to image file: {self.file_path}")
            with open(self.file_path, 'wb') as f:
                f.write(self.image_data)
            self.logger.info(f"Wrote {len(self.image_data)} bytes to {self.file_path}")
            self.dirty = False
        else:
            self.logger.debug("No changes to flush")

    def set_physical_format(self, physical_format: PhysicalFormat) -> None:
        self.logger.info(f"Setting physical format: {physical_format.encoding}, {physical_format.rate}kbps, "
                       f"{physical_format.sectors_per_track} sectors/track")
        self.physical_format = physical_format
        self.geometry_set = True

    def _calculate_sector_offset(self, cylinder: int, head: int, sector: int) -> int:
        sectors_per_track = self.physical_format.sectors_per_track
        heads = self.physical_format.heads
        sector_size = self.physical_format.sector_size

        sectors_per_cylinder = sectors_per_track * heads
        byte_offset = ((cylinder * sectors_per_cylinder) +
                       (head * sectors_per_track) +
                       (sector - 1)) * sector_size
        return byte_offset

    # Allow direct read of bytes for initial format detection
    def read_bytes_direct(self, offset: int, length: int) -> bytes:
        self.logger.debug(f"Direct read {length} bytes from offset {offset}")
        if offset + length <= len(self.image_data):
            return bytes(self.image_data[offset:offset + length])
        self.logger.warning(f"Direct read beyond image size: offset {offset}, length {length}")
        if offset < len(self.image_data):
            available = len(self.image_data) - offset
            self.logger.debug(f"Returning {available} bytes of data plus {length - available} zeros")
            return bytes(self.image_data[offset:]) + b'\x00' * (length - available)
        return b'\x00' * length
