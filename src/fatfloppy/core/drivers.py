# src/fatfloppy/core/drivers.py
import copy
from dataclasses import dataclass
from typing import List, Optional

from .utils.logging_config import get_logger
from greaseweazle.tools import util
from greaseweazle.codec import codec
from greaseweazle.tools import read

logger = get_logger()

@dataclass
class PhysicalFormat:
    encoding: str  # FM/MFM
    rate: int      # Data rate (kbps)
    rpm: int       # Rotations per minute
    gap3: int = 84 # Gap3 size
    cskew: int = 0 # Sector skew
    interleave: int = 1
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
        self.verify_writes = True  # Enable write verification by default
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
                self._create_and_set_custom_diskdef()
            except Exception as e:
                self.logger.error(f"Failed to create disk definition for writing: {e}")
                return

        # Pre-read tracks that are not fully dirty
        tracks_to_read = [track_id for track_id in self.dirty_tracks if len(self.dirty_sectors.get(track_id, {})) < self.physical_format.sectors_per_track]
        for track_id in tracks_to_read:
            cylinder, head = track_id
            try:
                self._read_track(cylinder, head)
            except Exception as e:
                self.logger.error(f"Failed to read track C:{cylinder} H:{head}: {e}")

        # Get a list of dirty tracks to process
        tracks_to_write = sorted(list(self.dirty_tracks))
        successfully_written = []

        # Process each track individually to maintain proper drive selection context
        for track_id in tracks_to_write:
            cylinder, head = track_id

            # Use a wrapper function to capture the result
            result = [False]  # Use a list to make it mutable from the inner function

            def write_track_wrapper():
                try:
                    self.logger.info(f"Writing track C:{cylinder} H:{head}")

                    # Generate flux list for the track
                    flux_list = self._convert_to_flux(cylinder, head)

                    # Seek and write - this will now happen within the drive_selected context
                    self.usb.seek(cylinder, head)

                    # Write the track
                    self.usb.write_track(
                        flux_list=flux_list,
                        cue_at_index=True,
                        terminate_at_index=True
                    )

                    self.logger.info(f"Successfully wrote track C:{cylinder} H:{head}")
                    result[0] = True
                except Exception as e:
                    self.logger.error(f"Error writing track C:{cylinder} H:{head}: {e}", exc_info=True)

            # Execute the write operation within proper drive selection context
            try:
                util.with_drive_selected(write_track_wrapper, self.usb, self.drive_obj, motor=True)

                if result[0]:
                    successfully_written.append(track_id)
                else:
                    self.logger.warning(f"Failed to write track C:{cylinder} H:{head}")
            except Exception as e:
                self.logger.error(f"Drive selection error for track C:{cylinder} H:{head}: {e}", exc_info=True)

        # Clear successful tracks from the dirty list and invalidate track_data cache
        for track_id in successfully_written:
            if track_id in self.dirty_tracks:
                self.dirty_tracks.remove(track_id)
                if track_id in self.dirty_sectors:
                    del self.dirty_sectors[track_id]
            # Invalidate the track_data cache for this track
            if track_id in self.track_data:
                del self.track_data[track_id]
                self.logger.debug(f"Cleared track_data cache for track {track_id}")

        if successfully_written:
            self.logger.info(f"Successfully wrote {len(successfully_written)} tracks")
        else:
            self.logger.warning("No tracks were successfully written")

    def set_physical_format(self, physical_format: PhysicalFormat) -> None:
        """Sets or updates the physical format used for track reading/writing."""
        if not isinstance(physical_format, PhysicalFormat):
            raise TypeError("physical_format must be a PhysicalFormat object")

        self.logger.info(f"Setting physical format: Enc={physical_format.encoding}, Rate={physical_format.rate}kbps, "
                        f"RPM={physical_format.rpm}, SPT={physical_format.sectors_per_track}, "
                        f"Heads={physical_format.heads}, SectorSize={physical_format.sector_size}, "
                        f"Gap3={physical_format.gap3}")
        # --- Store a DEEP COPY to prevent modifying the original shared object ---
        self.physical_format = copy.deepcopy(physical_format)
        # --- END CHANGE ---
        self.fmt_cls = None # Reset cached format class
        self.using_custom_diskdef = False # Reset custom definition flag
        self.last_successful_format = None # Clear last successful format

    def _create_and_set_custom_diskdef(self, cylinders: Optional[int] = None): # Added optional cylinders arg
        """Create and set a custom disk definition based on detected parameters or explicit values"""
        if not self.physical_format:
            self.logger.warning("Cannot create custom diskdef: physical format not set")
            return

        self.logger.debug("Creating custom disk definition")

        # --- UPDATED CYLINDER LOGIC ---
        # Prioritize explicitly passed cylinders
        if cylinders is not None:
            final_cylinders = cylinders
            self.logger.debug(f"Using explicitly provided cylinder count: {final_cylinders}")
        else:
            # Fallback to internal logic if not provided
            self.logger.debug("Determining cylinder count internally based on drive size/rate...")
            final_cylinders = 80  # Default for 3.5" disks
            if hasattr(self, 'drive_size') and self.drive_size == "5.25":
                # For 5.25" disks, DD is typically 40 cylinders, HD is 80
                # Determine based on data rate - 250Kbps is DD, 500Kbps is HD
                if self.physical_format.rate == 250:
                    final_cylinders = 40
            # TODO: Add logic for 8" drives if needed
            self.logger.debug(f"Internally determined cylinder count: {final_cylinders}")
        # --- END UPDATED CYLINDER LOGIC ---

        params = {
            'cyls': final_cylinders, # Use the determined cylinder count
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
            elif params['encoding'] == "FM":
                 format_name = "ibm.fm"
            else:
                 self.logger.warning(f"Unsupported encoding '{params['encoding']}' for custom diskdef, defaulting to ibm.mfm")
                 format_name = "ibm.mfm"


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
            self.logger.info(f"Custom disk definition created: Cyls={params['cyls']}, Heads={params['heads']}, "
                        f"{params['sectors_per_track']} sectors, {params['sector_size']} bytes/sector, {params['encoding']} encoding")
        except Exception as e:
            self.logger.error(f"Failed to create custom disk definition: {e}", exc_info=True)
            self.fmt_cls = None # Ensure fmt_cls is None on failure
            self.using_custom_diskdef = False

    def _read_track(self, cylinder: int, head: int) -> bool:
        """Read a track and detect its format"""
        # Ensure driver is initialized before attempting to read
        self.initialize()

        self.logger.info(f"Reading track C:{cylinder} H:{head}")
        import types
        from greaseweazle.codec import codec

        # List of formats to try
        formats_to_try = []

        # If we have a custom disk definition or successful format, use it first
        if self.fmt_cls and self.using_custom_diskdef:
            self.logger.debug("Using custom disk definition first")
            formats_to_try.append(("custom", None))
        elif self.last_successful_format:
            self.logger.debug(f"Using last successful format first: {self.last_successful_format}")
            formats_to_try.append(self.last_successful_format)

        # Only add ibm.scan for auto-detection if no custom format is defined
        if not self.fmt_cls or not self.using_custom_diskdef:
            # Add ibm.scan as a reliable detector
            formats_to_try.append(("ibm.scan", None))

            # Only add other formats as fallbacks if we don't have a known good format
            if not self.last_successful_format:
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

                        # Create a custom disk definition if we detected sectors using ibm.scan
                        # and don't already have a custom definition
                        if format_tuple[0] == "ibm.scan" and not self.using_custom_diskdef and self.physical_format:
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
        self.logger.debug(f"Converting track C:{cylinder} H:{head} to flux")

        # Make sure we have a format definition
        if not self.fmt_cls:
            if not self.physical_format:
                error_msg = "No physical format defined for writing"
                self.logger.error(error_msg)
                raise ValueError(error_msg)

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

        # Get the track definition
        track_def = None
        track_coords = (cylinder, head)

        if track_coords in self.fmt_cls.track_map:
            track_def = self.fmt_cls.track_map[track_coords]
            self.logger.debug(f"Found specific track definition for C:{cylinder} H:{head}")
        else:
            # Fallback to any track definition
            for key, value in self.fmt_cls.track_map.items():
                track_def = value
                self.logger.debug(f"Using generic track definition")
                break

        if not track_def:
            error_msg = "No track definition found"
            self.logger.error(error_msg)
            raise ValueError(error_msg)

        # Create the track with the proper definition
        track = track_def.mk_track(cylinder, head)
        track_id = (cylinder, head)

        # Set sector data from self.track_data if available
        if track_id in self.track_data:
            for s in track.sectors:
                if hasattr(s, 'idam') and hasattr(s.idam, 'r'):
                    sector_num = s.idam.r
                    if sector_num in self.track_data[track_id]:
                        s.dam.data = bytearray(self.track_data[track_id][sector_num])
                        s.crc = s.idam.crc = s.dam.crc = 0

        # Then apply dirty sectors
        if track_id in self.dirty_sectors:
            self.logger.debug(f"Applying {len(self.dirty_sectors[track_id])} dirty sectors to track")
            for s in track.sectors:
                if hasattr(s, 'idam') and hasattr(s.idam, 'r'):
                    sector_num = s.idam.r
                    if sector_num in self.dirty_sectors[track_id]:
                        sector_data = self.dirty_sectors[track_id][sector_num]
                        # Ensure data length matches expected sector size
                        expected_size = len(s.dam.data)
                        if len(sector_data) != expected_size:
                            if len(sector_data) < expected_size:
                                sector_data = sector_data + bytes(expected_size - len(sector_data))
                            else:
                                sector_data = sector_data[:expected_size]
                            self.logger.debug(f"Adjusted sector {sector_num} data to {expected_size} bytes")
                        s.dam.data = bytearray(sector_data)
                        s.crc = s.idam.crc = s.dam.crc = 0
                        self.logger.debug(f"Applied dirty sector {sector_num} to track")

        # Generate the master track
        master_track = track.master_track()

        # Ensure we have drive_ticks_per_rev
        if not self.drive_ticks_per_rev:
            self.logger.debug("No drive_ticks_per_rev available, using default based on physical format")
            if self.physical_format and self.physical_format.rpm:
                self.drive_ticks_per_rev = (60.0 / self.physical_format.rpm) * self.usb.sample_freq
                self.logger.info(f"Using RPM from format: {self.physical_format.rpm}")
            else:
                self.drive_ticks_per_rev = 0.2 * self.usb.sample_freq  # Default 300 RPM
                self.logger.info("Using default 300 RPM (0.2s per revolution)")

        # Set the time per revolution in the master track
        master_track.time_per_rev = self.drive_ticks_per_rev / self.usb.sample_freq

        # Generate writeout flux
        wflux = master_track.flux_for_writeout(cue_at_index=True)

        # Generate flux list with proper timing
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

    def _write_track(self, cylinder: int, head: int) -> bool:
        """Write a track with proper format handling"""
        self.logger.info(f"Writing track C:{cylinder} H:{head}")

        track_id = (cylinder, head)
        if track_id not in self.dirty_sectors:
            self.logger.debug(f"No dirty sectors for track C:{cylinder} H:{head}, nothing to write")
            return True

        try:
            # Make sure the custom format is properly set up before writing
            if not self.fmt_cls and self.physical_format:
                self._create_and_set_custom_diskdef()

            # Generate flux list for the track
            flux_list = self._convert_to_flux(cylinder, head)

            # Seek to the track
            self.usb.seek(cylinder, head)

            # Write the track
            self.usb.write_track(
                flux_list=flux_list,
                cue_at_index=True,
                terminate_at_index=True
            )

            # Verify the written track if enabled
            if hasattr(self, 'verify_writes') and self.verify_writes:
                self.logger.debug(f"Verifying written track C:{cylinder} H:{head}")
                # Clear track cache to force a fresh read
                if track_id in self.track_data:
                    del self.track_data[track_id]
                if track_id in self.dirty_tracks:
                    self.dirty_tracks.remove(track_id)
                # Read the track back and verify
                success = self._read_track(cylinder, head)
                if not success:
                    self.logger.error(f"Track verification failed for C:{cylinder} H:{head}")
                    return False

            self.logger.info(f"Successfully wrote track C:{cylinder} H:{head}")
            return True
        except Exception as e:
            self.logger.error(f"Error writing track C:{cylinder} H:{head}: {e}", exc_info=True)
            return False

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
        """Reads a single sector from the image buffer using CHS addressing."""
        # Allow reading boot sector even if geometry/format not fully set
        is_boot_sector = (cylinder == 0 and head == 0 and sector == 1)

        if not self.physical_format and not is_boot_sector:
            # If format isn't set (implying geometry isn't either), fail unless it's the boot sector
            error_msg = "Physical format not set, cannot read sector"
            self.logger.error(error_msg)
            raise ValueError(error_msg)

        # Determine sector size: use format if available, default to 512 otherwise (esp. for boot sector)
        sector_size = 512
        if self.physical_format and self.physical_format.sector_size > 0:
            sector_size = self.physical_format.sector_size
        elif not is_boot_sector:
             self.logger.warning("Physical format has zero sector size, using default 512.")


        try:
             offset = self._calculate_sector_offset(cylinder, head, sector, sector_size)
             # self.logger.debug(f"Reading sector C:{cylinder} H:{head} S:{sector} (Size:{sector_size}) from offset {offset}")
        except ValueError as e: # Catch calculation errors from invalid geometry
             self.logger.error(f"Cannot calculate offset for C:{cylinder} H:{head} S:{sector}: {e}")
             # Return empty sector matching expected size? Or raise? Return empty.
             return b'\x00' * sector_size

        # Check if read is within bounds of current image data
        if offset >= len(self.image_data):
             self.logger.warning(f"Read attempt beyond image size: Offset {offset} >= Size {len(self.image_data)} for C:{cylinder} H:{head} S:{sector}. Returning empty sector.")
             return b'\x00' * sector_size

        end_offset = offset + sector_size
        data = self.image_data[offset:end_offset]

        # Pad if read was short (e.g., reading last sector of a smaller-than-expected image)
        if len(data) < sector_size:
             self.logger.warning(f"Read short data ({len(data)} bytes) from image at offset {offset}. Padding to {sector_size} bytes.")
             data = data + bytes(sector_size - len(data))

        return bytes(data)

    def write_sector(self, cylinder: int, head: int, sector: int, data: bytes) -> None:
        """Writes a single sector to the image buffer using CHS addressing."""
        if not self.physical_format:
            error_msg = "Physical format not set, cannot write sector"
            self.logger.error(error_msg)
            raise ValueError(error_msg)

        sector_size = self.physical_format.sector_size
        if sector_size <= 0:
             error_msg = f"Invalid sector size ({sector_size}) in physical format."
             self.logger.error(error_msg)
             raise ValueError(error_msg)

        # Ensure data matches sector size (pad or truncate)
        if len(data) != sector_size:
            self.logger.warning(f"RawImageDriver.write_sector received data size {len(data)} != sector size {sector_size} for C:{cylinder} H:{head} S:{sector}. Adjusting.")
            if len(data) < sector_size:
                data = data + bytes(sector_size - len(data))
            else:
                data = data[:sector_size]

        try:
            offset = self._calculate_sector_offset(cylinder, head, sector, sector_size)
            # self.logger.debug(f"Writing sector C:{cylinder} H:{head} S:{sector} ({len(data)} bytes) at image offset {offset}")
        except ValueError as e:
            self.logger.error(f"Cannot calculate offset for write C:{cylinder} H:{head} S:{sector}: {e}")
            raise IOError(f"Failed to calculate offset for writing sector C:{cylinder} H:{head} S:{sector}") from e


        # Ensure image buffer is large enough
        required_size = offset + sector_size
        if required_size > len(self.image_data):
            self.logger.info(f"Extending image size from {len(self.image_data)} to {required_size} bytes.")
            try:
                 self.image_data.extend(b'\x00' * (required_size - len(self.image_data)))
            except MemoryError:
                 self.logger.error(f"MemoryError extending image buffer to {required_size} bytes.")
                 raise IOError("Not enough memory to extend image buffer")

        # Write the data
        try:
            self.image_data[offset : offset + sector_size] = data
            self.dirty = True
        except IndexError:
             # This should ideally not happen after the extension check, but log if it does
             self.logger.error(f"IndexError during image buffer write! Offset: {offset}, Size: {sector_size}, Buffer size: {len(self.image_data)}", exc_info=True)
             raise IOError("Internal error writing to image buffer")
        except Exception as e:
             self.logger.error(f"Unexpected error writing image buffer at offset {offset}: {e}", exc_info=True)
             raise IOError("Failed to write to image buffer") from e

    # ... (flush remains the same) ...

    def set_physical_format(self, physical_format: PhysicalFormat) -> None:
        """Sets the physical format/geometry for the image."""
        if not isinstance(physical_format, PhysicalFormat):
            raise TypeError("physical_format must be a PhysicalFormat object")
        self.logger.info(f"Setting physical format for image: Enc={physical_format.encoding}, Rate={physical_format.rate}kbps, "
                       f"SPT={physical_format.sectors_per_track}, Heads={physical_format.heads}, SectorSize={physical_format.sector_size}")
        # --- Store a DEEP COPY to prevent modifying the original shared object ---
        self.physical_format = copy.deepcopy(physical_format)
        # --- END CHANGE ---
        # Mark geometry as set if format is valid? Or rely on Disk.set_geometry?
        # Disk.set_geometry calls this, so setting self.physical_format is sufficient.
        # self.geometry_set = True # Maybe not needed here, Disk manages geometry object


    def _calculate_sector_offset(self, cylinder: int, head: int, sector: int, sector_size: int) -> int:
        """Calculates the byte offset for a sector using CHS addressing."""
        # Requires physical_format to be set to get dimensions
        if not self.physical_format:
            # Allow calculation only if called for boot sector (C=0,H=0,S=1) with default geom
             if cylinder == 0 and head == 0 and sector == 1:
                  self.logger.debug("Calculating offset for boot sector without full format set (using defaults).")
                  # Assume default geometry for boot sector offset calculation only
                  sectors_per_track = 18 # A common default
                  heads = 2 # Assume 2 heads initially
             else:
                  raise ValueError("Cannot calculate sector offset: Physical format not set.")
        else:
            sectors_per_track = self.physical_format.sectors_per_track
            heads = self.physical_format.heads

        if sectors_per_track <= 0 or heads <= 0 or sector_size <= 0:
             raise ValueError(f"Invalid geometry parameters in physical format (SPT={sectors_per_track}, Heads={heads}, Size={sector_size})")

        # Basic validation of CHS values (sector is 1-based)
        # Let Disk class handle strict bounds checking against cylinders
        if head < 0 or sector < 1:
             raise ValueError(f"Invalid CHS values for offset calculation (H={head}, S={sector})")

        # Standard LBA calculation based on CHS interleaving
        lba = (cylinder * heads + head) * sectors_per_track + (sector - 1)
        byte_offset = lba * sector_size

        # self.logger.debug(f"Calculated offset for C:{cylinder} H:{head} S:{sector} (Size:{sector_size}) -> LBA {lba} -> Offset {byte_offset}")
        return byte_offset

    def flush(self) -> None:
        if self.dirty:
            self.logger.info(f"Flushing changes to image file: {self.file_path}")
            with open(self.file_path, 'wb') as f:
                f.write(self.image_data)
            self.logger.info(f"Wrote {len(self.image_data)} bytes to {self.file_path}")
            self.dirty = False
        else:
            self.logger.debug("No changes to flush")

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
