# src/fatfloppy/core/drivers/greaseweazle.py
"""
Disk I/O driver for interacting with physical floppy drives via a Greaseweazle device.

This module provides the `GreaseweazleDriver`, an implementation of the `DiskIODriver`
abstract base class. It uses the `greaseweazle` library to perform low-level
operations such as reading and writing tracks, which are then translated into
sector-based I/O for the rest of the application.

This driver handles:
- Initializing the USB connection to the Greaseweazle.
- Caching track and sector data to minimize physical disk access.
- Managing a "dirty" state for sectors and flushing changes back to the disk.
- Dynamically creating Greaseweazle disk definitions (`DiskDef`) from the
  application's `PhysicalFormat` objects.
- Attempting to auto-detect track formats on-the-fly if an explicit format
  is not perfectly matched.

Note:
    This driver requires the `greaseweazle` library to be installed. If the
    library is not found, an `ImportError` will be raised upon instantiation.
"""
import copy
import types
import logging
from typing import List, Optional, Tuple, Dict, Any, Set, ClassVar

from ..drivers.base_driver import DiskIODriver
from ..physical_format import PhysicalFormat, TrackFormat
from ..utils.logging_config import get_logger

try:
    from greaseweazle.tools import util, read
    from greaseweazle.codec import codec
    from greaseweazle.codec.ibm import ibm
    GREASEWEAZLE_AVAILABLE = True
except ImportError:
    GREASEWEAZLE_AVAILABLE = False
    # Define placeholder types for when the library is not available
    util = None
    read = None
    ibm = None
    codec = None

logger: logging.Logger = get_logger()


def create_greaseweazle_diskdef(
    physical_format: PhysicalFormat, logger_instance: logging.Logger
) -> Optional["codec.DiskDef"]:
    """
    Creates a Greaseweazle DiskDef object from a PhysicalFormat definition.

    This function translates the abstract PhysicalFormat used by this application
    into the specific DiskDef object required by the Greaseweazle library to
    understand the track layout of a disk.

    Args:
        physical_format: The format definition for the disk.
        logger_instance: The logger to use for recording creation progress.

    Returns:
        A Greaseweazle DiskDef object if creation is successful, otherwise None.
    """
    if not GREASEWEAZLE_AVAILABLE:
        logger_instance.error("Cannot create diskdef: Greaseweazle library not available.")
        return None

    if not physical_format:
        logger_instance.warning("Cannot create diskdef: No physical format provided")
        return None

    logger_instance.debug("Creating Greaseweazle disk definition")
    try:
        disk_def = codec.DiskDef()
        disk_def.cyls = physical_format.cylinders
        disk_def.heads = physical_format.heads

        for tf in physical_format.track_formats:
            if tf.encoding == "MFM":
                format_name = "ibm.mfm"
            elif tf.encoding == "FM":
                format_name = "ibm.fm"
            else:
                logger_instance.warning(f"Unsupported encoding '{tf.encoding}', defaulting to 'ibm.mfm'")
                format_name = "ibm.mfm"

            track_def = ibm.IBMTrack_FixedDef(format_name)
            track_def.add_param("secs", str(tf.sectors_per_track))
            track_def.add_param("bps", str(physical_format.bytes_per_sector))
            track_def.add_param("rate", str(tf.rate))
            track_def.add_param("interleave", str(tf.interleave))
            track_def.add_param("id", str(tf.id_start))
            track_def.add_param("iam", "yes" if tf.iam_present else "no")

            if tf.gap1_bytes is not None:
                track_def.add_param("gap1", str(tf.gap1_bytes))
            if tf.gap2_bytes is not None:
                track_def.add_param("gap2", str(tf.gap2_bytes))
            if tf.gap3_bytes is not None:
                track_def.add_param("gap3", str(tf.gap3_bytes))
            if tf.cskew is not None:
                track_def.add_param("cskew", str(tf.cskew))
            if tf.hskew is not None:
                track_def.add_param("hskew", str(tf.hskew))

            track_def.finalise()

            for c in range(tf.track_start, tf.track_end + 1):
                for h in range(tf.head_start, tf.head_end + 1):
                    disk_def.track_map[(c, h)] = track_def

        disk_def.finalise()
        logger_instance.info(f"Disk definition created: Cyls={disk_def.cyls}, Heads={disk_def.heads}")
        return disk_def
    except Exception as e:
        logger_instance.error(f"Failed to create disk definition: {e}", exc_info=True)
        return None


class GreaseweazleDriver(DiskIODriver):
    """
    Disk I/O driver for Greaseweazle hardware.
    """
    # Plugin metadata
    driver_type: ClassVar[str] = "physical"
    driver_file_extensions: ClassVar[List[str]] = []
    driver_category: ClassVar[str] = "physical"
    driver_description: ClassVar[str] = "Greaseweazle physical drive interface"

    def __init__(self, device_name: Optional[str] = None, drive: str = "A", drive_size: str = "3.5"):
        """
        Initializes the GreaseweazleDriver.

        Args:
            device_name: The serial number or path of the Greaseweazle device.
                         If None, the default device is used.
            drive: The drive letter to control ("A" or "B").
            drive_size: The physical size of the drive ("3.5", "5.25", etc.).

        Raises:
            ImportError: If the `greaseweazle` library is not installed.
        """
        super().__init__()
        self.logger.debug(f"Initializing GreaseweazleDriver for drive {drive}")
        if not GREASEWEAZLE_AVAILABLE:
            raise ImportError("Greaseweazle library not found")

        # --- Configuration ---
        self.device_name: Optional[str] = device_name
        self.drive: str = drive
        self.drive_size: str = drive_size
        self.physical_format: Optional[PhysicalFormat] = None
        self.verify_writes: bool = True
        self.uses_physical_heads: bool = True

        # --- State ---
        self.initialized: bool = False
        self.dirty_sectors: Dict[Tuple[int, int], Dict[int, bytes]] = {}
        self.dirty_tracks: Set[Tuple[int, int]] = set()
        self.track_data: Dict[Tuple[int, int], Dict[int, bytes]] = {}
        self.sector_cache: Dict[Tuple[int, int, int], bytes] = {}

        # --- Greaseweazle Specifics ---
        self.usb: Optional[Any] = None  # Greaseweazle USB object
        self.drive_obj: Optional[Any] = None  # Greaseweazle Drive object
        self.fmt_cls: Optional["codec.DiskDef"] = None
        self.drive_ticks_per_rev: Optional[float] = None
        self.last_successful_format: Optional[Tuple[str, Optional[int]]] = None
        self.using_custom_diskdef: bool = False
        self.scan_track_object: Optional[Any] = None

    # --- Properties ---

    @property
    def has_embedded_geometry(self) -> bool:
        """Physical disks can be scanned to detect geometry."""
        return True

    @property
    def allows_geometry_override(self) -> bool:
        """Geometry can be set externally to match known formats."""
        return True

    @property
    def supports_in_place_formatting(self) -> bool:
        """Physical disks can be formatted."""
        return True

    @property
    def supports_new_image_creation(self) -> bool:
        """Greaseweazle doesn't create image files, it accesses hardware."""
        return False

    @property
    def requires_initialization(self) -> bool:
        """Greaseweazle needs initialization to measure RPM."""
        return True

    def validate_state_for_opening(self) -> Tuple[bool, Optional[str]]:
        """
        Validates Greaseweazle driver state after opening.

        For physical drives, physical_format is not required at open time
        since it can be detected during I/O operations.

        Returns:
            Tuple of (is_valid, error_message).
        """
        # Check that Greaseweazle library is available
        if not GREASEWEAZLE_AVAILABLE:
            return False, "Greaseweazle library not available"

        # Physical drives don't need physical_format at open time
        # It will be set during format application or auto-detection
        return True, None

    def validate_for_opening(self, source: str, **kwargs) -> Tuple[bool, Optional[str]]:
        """
        Validates whether the Greaseweazle can be opened.

        Args:
            source: Device name (can be None for auto-detection).
            **kwargs: Must contain 'drive_letter' and 'drive_size'.

        Returns:
            Tuple of (is_valid, error_message).
        """
        if not GREASEWEAZLE_AVAILABLE:
            return False, "Greaseweazle library not installed"

        drive_letter = kwargs.get('drive_letter', self.drive)
        drive_size = kwargs.get('drive_size', self.drive_size)

        if drive_letter not in ['A', 'B']:
            return False, f"Invalid drive letter: {drive_letter}. Must be 'A' or 'B'"

        if drive_size not in ['3.5', '5.25', '8']:
            return False, f"Invalid drive size: {drive_size}. Must be '3.5', '5.25', or '8'"

        # Note: We can't validate USB connection until initialize() is called
        return True, None

    def set_physical_format(self, physical_format: PhysicalFormat) -> None:
        """
        Sets the physical disk format and recreates the Greaseweazle diskdef.
        """
        self.logger.debug("Setting physical format for Greaseweazle driver")
        if not isinstance(physical_format, PhysicalFormat):
            raise TypeError("physical_format must be a PhysicalFormat object")

        self.physical_format = copy.deepcopy(physical_format)

        # Reset any cached format info
        self.fmt_cls = None
        self.using_custom_diskdef = False
        self.last_successful_format = None

        # This is the crucial part: The driver itself is now responsible
        # for updating its internal state when the format is set.
        self._create_and_set_custom_diskdef()

        self.logger.info(f"Physical format set and diskdef recreated: Cyls={physical_format.cylinders}, Heads={physical_format.heads}")

    def get_format_requirements(self) -> dict:
        """
        Returns format requirements for Greaseweazle driver.

        Returns:
            Dictionary describing what format information is needed.
        """
        return {
            'needs_format_for_open': False,  # Can open without format
            'needs_format_for_io': False,    # Can scan tracks
            'can_derive_format': True,       # Can detect via track scanning
            'preferred_detection_method': 'auto'  # Should auto-detect
        }

    def prepare_for_format_application(self, format_info: dict) -> Tuple[bool, Optional[str]]:
        """
        Validates format compatibility for Greaseweazle driver.

        Args:
            format_info: Dictionary containing format parameters.

        Returns:
            Tuple of (is_ready, error_message).
        """
        # Check if we have any format information at all
        has_format = any(k in format_info for k in
                        ['format_name', 'physical_format', 'cylinders'])

        if not has_format:
            # No format provided is OK - we can auto-detect
            return True, None

        # If we have format info, we need to resolve it to validate
        # Import here to avoid circular dependency
        from ..format_definitions import FLOPPY_FORMATS

        # Try to resolve to a physical format
        if 'physical_format' in format_info:
            pf = format_info['physical_format']
        elif 'format_name' in format_info:
            format_name = format_info['format_name']
            profile = FLOPPY_FORMATS.get(format_name)
            if not profile or not profile.physical_format:
                return False, f"Unknown or invalid format name: {format_name}"
            pf = profile.physical_format
        elif 'cylinders' in format_info:
            # Custom geometry - build it to validate
            from ..physical_format import PhysicalFormat, TrackFormat
            try:
                cylinders = format_info.get('cylinders', 80)
                heads = format_info.get('heads', 2)
                sectors_per_track = format_info.get('sectors_per_track', 18)
                bytes_per_sector = format_info.get('bytes_per_sector', 512)
                encoding = format_info.get('encoding', 'MFM')
                rate = format_info.get('rate', 500)

                track_format = TrackFormat(
                    track_start=0, track_end=cylinders - 1,
                    head_start=0, head_end=heads - 1,
                    sectors_per_track=sectors_per_track,
                    encoding=encoding, rate=rate, interleave=1,
                    bytes_per_sector=bytes_per_sector
                )
                pf = PhysicalFormat(
                    cylinders=cylinders, heads=heads, rpm=300,
                    heads_inverted=False, bytes_per_sector=bytes_per_sector,
                    track_formats=[track_format]
                )
            except Exception as e:
                return False, f"Invalid geometry parameters: {e}"
        else:
            return False, "No recognizable format information provided"

        # Validate that the format is compatible with the drive size
        if self.drive_size == "3.5":
            if pf.cylinders > 84:
                return False, f"Format has {pf.cylinders} cylinders, 3.5\" drives support max 84"
        elif self.drive_size == "5.25":
            if pf.cylinders > 84:
                return False, f"Format has {pf.cylinders} cylinders, 5.25\" drives support max 84"
        elif self.drive_size == "8":
            if pf.cylinders > 80:
                return False, f"Format has {pf.cylinders} cylinders, 8\" drives support max 80"

        return True, None

    # --- Public API ---

    def initialize(self) -> None:
        """
        Connects to the Greaseweazle USB device and measures the drive's RPM.

        This method must be called before any read or write operations.
        It is called automatically by the first I/O operation if not done manually.
        """
        if not GREASEWEAZLE_AVAILABLE:
            self.logger.error("Cannot initialize: Greaseweazle library unavailable")
            return
        if self.initialized:
            self.logger.debug("Driver already initialized")
            return

        self.logger.debug(f"Opening USB device: {self.device_name or 'default'}")
        self.usb = util.usb_open(self.device_name)
        self.drive_obj = util.Drive()(self.drive)

        self.logger.debug("Attempting to measure drive RPM")
        try:
            def measure_rpm():
                flux = self.usb.read_track(2)
                self.drive_ticks_per_rev = flux.ticks_per_rev
            util.with_drive_selected(measure_rpm, self.usb, self.drive_obj)
            self.logger.debug(f"RPM measured: ticks_per_rev={self.drive_ticks_per_rev}")
        except Exception as e:
            # TODO: If failed to measure RPM - we should stop initializing and quit/close disk,
            #       as this tells us that either drive isn't working or something is wrong
            #       with floppy disk itself. Or it's not there?) CRYTICAL!
            self.logger.warning(f"RPM measurement failed: {e}")
            self.drive_ticks_per_rev = 0.2 * self.usb.sample_freq
            self.logger.debug(f"Defaulting to ticks_per_rev={self.drive_ticks_per_rev}")

        self.initialized = True
        self.logger.info(f"Driver initialized for drive {self.drive}")

    def read_sector(self, cylinder: int, head: int, sector: int) -> bytes:
        """
        Reads a single sector from the physical disk.

        Args:
            cylinder: The cylinder number.
            head: The head number.
            sector: The sector number.

        Returns:
            The sector data as a bytes object. Returns a zero-filled buffer on failure.
        """
        self.logger.debug(f"Reading sector C:{cylinder} H:{head} S:{sector}")
        self.initialize()
        sector_key = (cylinder, head, sector)
        if sector_key in self.sector_cache:
            self.logger.debug(f"Sector {sector_key} found in cache")
            return self.sector_cache[sector_key]

        track_id = (cylinder, head)
        if track_id not in self.track_data:
            try:
                self._read_track(cylinder, head)
            except Exception as e:
                self.logger.error(f"Failed to read track C:{cylinder} H:{head}: {e}")
                self.track_data[track_id] = {}

        if track_id in self.track_data and sector in self.track_data[track_id]:
            data = self.track_data[track_id][sector]
            self.sector_cache[sector_key] = data
            self.logger.debug(f"Sector {sector_key} cached")
            return data

        bytes_per_sector = self.physical_format.bytes_per_sector if self.physical_format else 512
        self.logger.warning(f"Returning default zero-filled data for unreadable sector {sector_key}")
        return b"\x00" * bytes_per_sector

    def write_sector(self, cylinder: int, head: int, sector: int, data: bytes) -> None:
        """
        Writes a single sector to an in-memory buffer.

        The actual write to the physical disk is deferred until `flush()` is called.

        Args:
            cylinder: The cylinder number.
            head: The head number.
            sector: The sector number.
            data: The sector data as a bytes object.
        """
        self.logger.debug(f"Writing sector C:{cylinder} H:{head} S:{sector}, {len(data)} bytes")
        self.initialize()
        track_id = (cylinder, head)
        self.dirty_sectors.setdefault(track_id, {})[sector] = data
        self.dirty_tracks.add(track_id)
        sector_key = (cylinder, head, sector)
        if sector_key in self.sector_cache:
            del self.sector_cache[sector_key]
            self.logger.debug(f"Cleared cache for sector {sector_key}")

    def flush(self) -> None:
        """
        Writes all buffered (dirty) sectors to the physical disk.
        """
        if not self.initialized or not self.dirty_tracks:
            self.logger.debug("Nothing to flush: not initialized or no dirty tracks")
            return

        self.logger.debug(f"Flushing {len(self.dirty_tracks)} dirty tracks")
        if not self.fmt_cls and self.physical_format:
            self._create_and_set_custom_diskdef()

        tracks_to_read = [
            tid for tid in self.dirty_tracks
            if len(self.dirty_sectors.get(tid, {})) < self.physical_format.get_sectors_per_track(tid[0], tid[1])
        ]
        for cylinder, head in tracks_to_read:
            try:
                self._read_track(cylinder, head)
            except Exception as e:
                self.logger.error(f"Pre-flush read failed for track C:{cylinder} H:{head}: {e}")

        for cylinder, head in sorted(self.dirty_tracks):
            result = [False]

            def write_track_wrapper():
                nonlocal result
                try:
                    flux_list = self._convert_to_flux(cylinder, head)
                    self.usb.seek(cylinder, head)
                    self.usb.write_track(flux_list=flux_list, cue_at_index=True, terminate_at_index=True)
                    result[0] = True
                except Exception as e:
                    self.logger.error(f"Write failed for track C:{cylinder} H:{head}: {e}", exc_info=True)

            try:
                util.with_drive_selected(write_track_wrapper, self.usb, self.drive_obj, motor=True)
                if not result[0]:
                    self.logger.warning(f"Track write incomplete C:{cylinder} H:{head}")
                    if (cylinder, head) in self.track_data:
                        del self.track_data[(cylinder, head)]
                else:
                    self._update_after_write(cylinder, head)
            except Exception as e:
                self.logger.error(f"Drive selection error during flush C:{cylinder} H:{head}: {e}", exc_info=True)

        self.logger.info("Flush operation completed")

    # --- Private Helper Methods ---

    def _create_and_set_custom_diskdef(self) -> None:
        """
        Creates a Greaseweazle DiskDef from the current physical format and sets it for use.
        """
        self.logger.debug("Creating and setting custom disk definition")
        if not self.physical_format:
            self.logger.warning("No physical format to create diskdef")
            self.fmt_cls = None
            self.using_custom_diskdef = False
            return

        disk_def = create_greaseweazle_diskdef(self.physical_format, self.logger)
        if disk_def:
            self.fmt_cls = disk_def
            self.using_custom_diskdef = True
            self.logger.debug("Custom diskdef set successfully")
        else:
            self.fmt_cls = None
            self.using_custom_diskdef = False
            self.logger.error("Failed to set custom diskdef")

    def _get_formats_to_try(self) -> List[Tuple[str, Optional[int]]]:
        """
        Determines the sequence of formats to attempt when reading a track.

        It prioritizes a custom definition, then the last successful format,
        and finally falls back to a generic scan or default MFM.

        Returns:
            A list of format tuples (format_name, rate) to try.
        """
        formats = []
        if self.fmt_cls and self.using_custom_diskdef:
            formats.append(("custom", None))
        if self.last_successful_format:
            formats.append(self.last_successful_format)
        if not self.fmt_cls or not self.using_custom_diskdef:
            formats.append(("ibm.scan", None))
        if not self.last_successful_format and self.physical_format:
            formats.append(("ibm.mfm", 500))  # Default fallback
        self.logger.debug(f"Formats to try: {formats}")
        return formats

    def _update_physical_format(self, dat: Any, num_sectors: int) -> None:
        """
        Updates the driver's physical_format based on a successful 'ibm.scan'.

        Args:
            dat: The data object returned from a successful Greaseweazle read.
            num_sectors: The number of sectors found on the track.
        """
        if not hasattr(dat, "track") or not hasattr(dat.track, "mode") or not hasattr(dat.track, "clock"):
            self.logger.debug("Insufficient data to update physical format")
            return

        mode = dat.track.mode
        encoding = "MFM" if str(mode) == "IBM MFM" else "FM"
        # Calculate rate in kbps from clock period in microseconds
        rate = int(1.0 / (dat.track.clock * (2000 if encoding == "MFM" else 1000)))
        self.logger.debug(f"Updating format: encoding={encoding}, rate={rate}, sectors={num_sectors}")

        track_format = TrackFormat(
            track_start=0, track_end=79, head_start=0, head_end=1,
            sectors_per_track=num_sectors, encoding=encoding, rate=rate, gap3_bytes=84, interleave=1
        )
        self.physical_format = PhysicalFormat(
            cylinders=80, heads=2, rpm=300, heads_inverted=False,
            bytes_per_sector=512, track_formats=[track_format]
        )

    def _read_track_with_format(
        self, cylinder: int, head: int, format_tuple: Tuple[str, Optional[int]]
    ) -> Optional[Dict[int, bytes]]:
        """
        Attempts to read a track using a single, specified Greaseweazle format.

        Args:
            cylinder: The cylinder number.
            head: The head number.
            format_tuple: A tuple of (format_name, rate) to use for the read attempt.

        Returns:
            A dictionary of {sector_number: sector_data} on success, otherwise None.
        """
        self.logger.debug(f"Reading track C:{cylinder} H:{head} with format {format_tuple}")
        format_name, _ = format_tuple
        fmt_cls = self.fmt_cls if format_name == "custom" else None
        if not fmt_cls:
            try:
                fmt_cls = codec.get_diskdef(format_name)
            except Exception as e:
                self.logger.error(f"Failed to get disk definition for {format_name}: {e}")
                return None

        args = types.SimpleNamespace(
            revs=3, raw=False, fmt_cls=fmt_cls, tracks=util.TrackSet(f"c={cylinder}:h={head}"),
            retries=2, seek_retries=0, reverse=False, adjust_speed=None, fake_index=None,
            hard_sectors=False, drive=self.drive_obj, ticks=0, drive_ticks_per_rev=self.drive_ticks_per_rev
        )
        success = False

        def read_track_wrapper():
            nonlocal success
            try:
                track_iter = util.TrackSet.TrackIter(args.tracks)
                next(track_iter)
                _, dat = read.read_with_retry(self.usb, args, track_iter)
                if dat and (sectors := getattr(getattr(dat, "track", None), "sectors", None) or getattr(dat, "sectors", None)):
                    self.scan_track_object = dat
                    sector_data = {s.idam.r: bytes(s.dam.data) for s in sectors if hasattr(s, "idam") and hasattr(s, "dam") and hasattr(s.dam, "data")}
                    if sector_data:
                        self.logger.debug(f"Read sectors: {list(sector_data.keys())}")
                        self.track_data[(cylinder, head)] = sector_data
                        self.fmt_cls = fmt_cls
                        if format_tuple[0] == "ibm.scan":
                            self._update_physical_format(dat, len(sector_data))
                            self._create_and_set_custom_diskdef()
                        if format_tuple[0] != "custom":
                            self.last_successful_format = format_tuple
                        success = True
            except Exception as e:
                self.logger.warning(f"Track read error with format {format_tuple}: {e}")

        util.with_drive_selected(read_track_wrapper, self.usb, self.drive_obj)
        if success:
            self.logger.debug(f"Track C:{cylinder} H:{head} read successfully")
            return self.track_data.get((cylinder, head))
        return None

    def _read_track(self, cylinder: int, head: int) -> bool:
        """
        Reads a full track from the disk, trying multiple formats if necessary.

        Args:
            cylinder: The cylinder number.
            head: The head number.

        Returns:
            True if the track was read successfully, False otherwise.
        """
        self.logger.debug(f"Attempting to read track C:{cylinder} H:{head}")
        self.initialize()
        self.track_data[(cylinder, head)] = {}
        for fmt in self._get_formats_to_try():
            if self._read_track_with_format(cylinder, head, fmt):
                return True
        self.logger.warning(f"No suitable format found for track C:{cylinder} H:{head}")
        return False

    def _convert_to_flux(self, cylinder: int, head: int) -> List[int]:
        """
        Converts sector data for a track into a flux stream for writing.

        Args:
            cylinder: The cylinder number of the track to convert.
            head: The head number of the track to convert.

        Returns:
            A list of integers representing the flux stream.

        Raises:
            ValueError: If no format is defined or the format is invalid.
        """
        self.logger.debug(f"Converting track C:{cylinder} H:{head} to flux")
        if not self.fmt_cls:
            if self.physical_format:
                self._create_and_set_custom_diskdef()
                if not self.fmt_cls:
                    raise ValueError("Failed to create disk definition for writing")
            else:
                raise ValueError("No format defined for writing")

        if not hasattr(self.fmt_cls, "track_map"):
            raise TypeError(f"Invalid DiskDef object: {type(self.fmt_cls)}")

        track_def = self.fmt_cls.track_map.get((cylinder, head))
        if not track_def:
            raise ValueError(f"No track definition for C:{cylinder} H:{head}")

        track = track_def.mk_track(cylinder, head)
        track_id = (cylinder, head)
        for s in track.sectors:
            if hasattr(s, "idam") and hasattr(s.idam, "r"):
                sector_num = s.idam.r
                if track_id in self.track_data and sector_num in self.track_data[track_id]:
                    s.dam.data = bytearray(self.track_data[track_id][sector_num])
                elif track_id in self.dirty_sectors and sector_num in self.dirty_sectors[track_id]:
                    data = self.dirty_sectors[track_id][sector_num]
                    # Pad or truncate data to match expected sector size
                    s.dam.data = bytearray(data[:len(s.dam.data)] if len(data) > len(s.dam.data) else data + bytes(len(s.dam.data) - len(data)))
                s.crc = s.idam.crc = s.dam.crc = 0

        master_track = track.master_track()
        self.drive_ticks_per_rev = self.drive_ticks_per_rev or (
            (60.0 / self.physical_format.rpm) * self.usb.sample_freq
            if self.physical_format and self.physical_format.rpm else 0.2 * self.usb.sample_freq
        )
        master_track.time_per_rev = self.drive_ticks_per_rev / self.usb.sample_freq
        wflux = master_track.flux_for_writeout(cue_at_index=True)

        factor = self.drive_ticks_per_rev / wflux.ticks_to_index
        flux_list = []
        rem = 0.0
        for x in wflux.list:
            y = x * factor + rem
            val = round(y)
            rem = y - val
            flux_list.append(val)
        return flux_list

    def _update_after_write(self, cylinder: int, head: int) -> None:
        """
        Updates internal caches and dirty flags after a successful write.

        Args:
            cylinder: The cylinder number that was written.
            head: The head number that was written.
        """
        track_id = (cylinder, head)
        if track_id in self.dirty_sectors:
            sectors_per_track = self.physical_format.get_sectors_per_track(cylinder, head)
            # If the entire track was dirty, we can just copy the dirty data
            if len(self.dirty_sectors[track_id]) == sectors_per_track:
                self.track_data[track_id] = self.dirty_sectors[track_id].copy()
            # Otherwise, update the existing track cache with the new dirty sectors
            elif track_id in self.track_data:
                self.track_data[track_id].update(self.dirty_sectors[track_id])
            del self.dirty_sectors[track_id]
        self.dirty_tracks.discard(track_id)
        self.logger.debug(f"Track C:{cylinder} H:{head} updated post-write")
