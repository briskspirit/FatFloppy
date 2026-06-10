import contextlib
import copy
import logging
import types
from typing import Any, ClassVar, Optional

from ..drivers.base_driver import DiskIODriver
from ..physical_format import PhysicalFormat, TrackFormat
from ..utils.logging_config import get_logger

try:
    from greaseweazle.codec import codec
    from greaseweazle.codec.ibm import ibm
    from greaseweazle.tools import read, util

    GREASEWEAZLE_AVAILABLE = True
except ImportError:
    GREASEWEAZLE_AVAILABLE = False
    util = None
    read = None
    ibm = None
    codec = None


DEFAULT_REVS = 3
DEFAULT_RETRIES = 2
DEFAULT_SEEK_RETRIES = 0
FALLBACK_TICKS_MULTIPLIER = 0.2
DEFAULT_BPS = 512
DEFAULT_CYLINDERS = 80
DEFAULT_HEADS = 2
DEFAULT_RPM = 300
DEFAULT_GAP3 = 84
DEFAULT_INTERLEAVE = 1

MAX_CYLINDERS_3_5 = 84
MAX_CYLINDERS_5_25 = 84
MAX_CYLINDERS_8 = 80

DRIVE_LETTER_A = "A"
DRIVE_LETTER_B = "B"
VALID_DRIVE_LETTERS = [DRIVE_LETTER_A, DRIVE_LETTER_B]

DRIVE_SIZE_3_5 = "3.5"
DRIVE_SIZE_5_25 = "5.25"
DRIVE_SIZE_8 = "8"
VALID_DRIVE_SIZES = [DRIVE_SIZE_3_5, DRIVE_SIZE_5_25, DRIVE_SIZE_8]

ENCODING_MFM = "MFM"
ENCODING_FM = "FM"
FORMAT_IBM_MFM = "ibm.mfm"
FORMAT_IBM_FM = "ibm.fm"
FORMAT_IBM_SCAN = "ibm.scan"
FORMAT_CUSTOM = "custom"

IBM_MFM_MODE = "IBM MFM"
MFM_CLOCK_DIVISOR = 2000
FM_CLOCK_DIVISOR = 1000
DEFAULT_RATE_MFM = 500

RPM_MEASUREMENT_CYLINDER = 2


logger = get_logger("GreaseweazleDriver")


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
        logger_instance.error(
            "Cannot create diskdef: Greaseweazle library not available."
        )
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
            if tf.encoding == ENCODING_MFM:
                format_name = FORMAT_IBM_MFM
            elif tf.encoding == ENCODING_FM:
                format_name = FORMAT_IBM_FM
            else:
                logger_instance.warning(
                    f"Unsupported encoding '{tf.encoding}', defaulting to '{FORMAT_IBM_MFM}'"
                )
                format_name = FORMAT_IBM_MFM

            track_def = ibm.IBMTrack_FixedDef(format_name)
            track_def.add_param("secs", str(tf.sectors_per_track))
            track_def.add_param("bps", str(tf.bytes_per_sector))
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
        logger_instance.info(
            f"Disk definition created: Cyls={disk_def.cyls}, Heads={disk_def.heads}"
        )
        return disk_def
    except Exception as e:
        logger_instance.error(f"Failed to create disk definition: {e}", exc_info=True)
        return None


class GreaseweazleDriver(DiskIODriver):
    """
    Disk I/O driver for Greaseweazle hardware.

    This driver interfaces with physical floppy drives via a Greaseweazle device,
    providing low-level track read/write operations translated to sector-based I/O.
    """

    driver_type: ClassVar[str] = "physical"
    driver_file_extensions: ClassVar[list[str]] = []
    driver_category: ClassVar[str] = "physical"
    driver_description: ClassVar[str] = "Greaseweazle physical drive interface"
    driver_priority: ClassVar[int] = 50

    def __init__(
        self,
        device_name: Optional[str] = None,
        drive: str = DRIVE_LETTER_A,
        drive_size: str = DRIVE_SIZE_3_5,
    ):
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

        self.device_name: Optional[str] = device_name
        self.drive: str = drive
        self.drive_size: str = drive_size
        self.physical_format: Optional[PhysicalFormat] = None
        self.verify_writes: bool = True
        self.uses_physical_heads: bool = True

        self.initialized: bool = False
        self.dirty_sectors: dict[tuple[int, int], dict[int, bytes]] = {}
        self.dirty_tracks: set[tuple[int, int]] = set()
        self.track_data: dict[tuple[int, int], dict[int, bytes]] = {}
        self.sector_cache: dict[tuple[int, int, int], bytes] = {}

        self.usb: Optional[Any] = None
        self.drive_obj: Optional[Any] = None
        self.fmt_cls: Optional[codec.DiskDef] = None
        self.drive_ticks_per_rev: Optional[float] = None
        self.last_successful_format: Optional[tuple[str, Optional[int]]] = None
        self.using_custom_diskdef: bool = False
        self.scan_track_object: Optional[Any] = None

    def close(self) -> None:
        """Closes the USB connection to the Greaseweazle device."""
        if self.usb is not None:
            try:
                if hasattr(self.usb, "ser") and self.usb.ser:
                    self.usb.ser.close()
                    self.logger.debug("Closed Greaseweazle USB connection")
            except Exception as e:
                self.logger.warning(f"Error closing USB connection: {e}")
            finally:
                self.usb = None
                self.initialized = False

    @property
    def allows_geometry_override(self) -> bool:
        """
        Geometry can be set externally to match known formats.

        Returns:
            True as geometry override is allowed.
        """
        return True

    @property
    def dirty(self) -> bool:
        """
        Indicates whether there are pending writes to flush.

        Returns:
            True if there are dirty tracks.
        """
        return bool(self.dirty_tracks)

    @property
    def has_embedded_geometry(self) -> bool:
        """
        Physical disks can be scanned to detect geometry.

        Returns:
            True as geometry can be detected.
        """
        return True

    @property
    def requires_initialization(self) -> bool:
        """
        Greaseweazle needs initialization to measure RPM.

        Returns:
            True as initialization is required.
        """
        return True

    @property
    def supports_in_place_formatting(self) -> bool:
        """
        Physical disks can be formatted.

        Returns:
            True as formatting is supported.
        """
        return True

    @property
    def supports_new_image_creation(self) -> bool:
        """
        Greaseweazle doesn't create image files, it accesses hardware.

        Returns:
            False as new image creation is not applicable.
        """
        return False

    def flush(self) -> None:
        """
        Writes all buffered (dirty) sectors to the physical disk.

        Each dirty track is written as a whole. A track is only written once
        every one of its sectors is either freshly supplied by the caller or
        successfully read back from the media, so a transient read failure can
        never cause non-dirty sectors to be overwritten with codec filler.

        Raises:
            IOError: If any track could not be safely written (failed pre-read,
                failed write, or, when verify_writes is set, a read-back
                mismatch). Failed tracks are left dirty so they can be retried.
        """
        if not self.initialized or not self.dirty_tracks:
            self.logger.debug("Nothing to flush: not initialized or no dirty tracks")
            return

        self.logger.debug(f"Flushing {len(self.dirty_tracks)} dirty tracks")
        if not self.fmt_cls and self.physical_format:
            self._create_and_set_custom_diskdef()

        failures: list[str] = []
        for cylinder, head in sorted(self.dirty_tracks):
            try:
                self._flush_track(cylinder, head)
            except Exception as e:
                self.logger.error(
                    f"Flush failed for track C:{cylinder} H:{head}: {e}",
                    exc_info=True,
                )
                failures.append(f"C:{cylinder} H:{head} ({e})")

        if failures:
            raise OSError("Failed to flush track(s): " + "; ".join(failures))

        self.logger.info("Flush operation completed")

    def _flush_track(self, cylinder: int, head: int) -> None:
        """
        Safely writes a single dirty track to the physical disk.

        Args:
            cylinder: The cylinder number to flush.
            head: The head number to flush.

        Raises:
            IOError: If the track cannot be safely or completely written.
        """
        track_id = (cylinder, head)
        spt = self.physical_format.get_sectors_per_track(cylinder, head)
        track_format = self.physical_format.get_track_format(cylinder, head)
        expected_ids = {track_format.id_start + i for i in range(spt)}

        dirty = dict(self.dirty_sectors.get(track_id, {}))

        # Preserve untouched sectors: read the track back first unless every
        # sector is being rewritten. Refuse to write filler for missing ones.
        if not expected_ids.issubset(dirty):
            self._read_track(cylinder, head)
            existing = self.track_data.get(track_id, {})
        else:
            existing = {}

        intended = dict(existing)
        intended.update(dirty)

        missing = sorted(expected_ids - set(intended))
        if missing:
            raise OSError(
                f"refusing to write track with unrecovered sector(s) {missing}; "
                "pre-flush read incomplete"
            )

        # Stage the full intended image so _convert_to_flux encodes real data.
        self.track_data[track_id] = intended

        result = [False]

        def write_track_wrapper(cyl=cylinder, hd=head, res=result):
            flux_list = self._convert_to_flux(cyl, hd)
            self.usb.seek(cyl, hd)
            self.usb.write_track(
                flux_list=flux_list, cue_at_index=True, terminate_at_index=True
            )
            res[0] = True

        util.with_drive_selected(
            write_track_wrapper, self.usb, self.drive_obj, motor=True
        )
        if not result[0]:
            raise OSError("track write did not complete")

        if self.verify_writes:
            self._verify_track(cylinder, head, intended)

        # Commit: the on-disk track now matches the intended image.
        self.track_data[track_id] = intended
        self.dirty_sectors.pop(track_id, None)
        self.dirty_tracks.discard(track_id)
        for sector in range(spt):
            self.sector_cache.pop((cylinder, head, sector), None)

    def _verify_track(
        self, cylinder: int, head: int, intended: dict[int, bytes]
    ) -> None:
        """
        Reads a freshly written track back and compares it to the intended data.

        Args:
            cylinder: The cylinder number to verify.
            head: The head number to verify.
            intended: Mapping of physical sector id to the bytes that were written.

        Raises:
            IOError: If the track cannot be re-read or any sector differs.
        """
        track_id = (cylinder, head)
        if not self._read_track(cylinder, head):
            raise OSError("verify failed: could not read track back")
        readback = self.track_data.get(track_id, {})
        for sector_id, data in intended.items():
            actual = readback.get(sector_id)
            if actual is None or bytes(actual) != bytes(data):
                raise OSError(
                    f"verify mismatch on sector id {sector_id} (C:{cylinder} H:{head})"
                )

    def get_format_requirements(self) -> dict:
        """
        Returns format requirements for Greaseweazle driver.

        Returns:
            Dictionary describing what format information is needed.
        """
        return {
            "needs_format_for_open": False,
            "needs_format_for_io": False,
            "can_derive_format": True,
            "preferred_detection_method": "auto",
        }

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
                flux = self.usb.read_track(RPM_MEASUREMENT_CYLINDER)
                self.drive_ticks_per_rev = flux.ticks_per_rev

            util.with_drive_selected(measure_rpm, self.usb, self.drive_obj)
            self.logger.debug(f"RPM measured: ticks_per_rev={self.drive_ticks_per_rev}")
        except Exception as e:
            self.logger.warning(f"RPM measurement failed: {e}")
            self.drive_ticks_per_rev = FALLBACK_TICKS_MULTIPLIER * self.usb.sample_freq
            self.logger.debug(f"Defaulting to ticks_per_rev={self.drive_ticks_per_rev}")

        self.initialized = True
        self.logger.info(f"Driver initialized for drive {self.drive}")

    def prepare_for_format_application(
        self, format_info: dict
    ) -> tuple[bool, Optional[str]]:
        """
        Validates format compatibility for Greaseweazle driver.

        Args:
            format_info: Dictionary containing format parameters.

        Returns:
            Tuple of (is_ready, error_message).
        """
        has_format = any(
            k in format_info for k in ["format_name", "physical_format", "cylinders"]
        )

        if not has_format:
            return True, None

        from ..filesystem_registry import FilesystemRegistry

        if "physical_format" in format_info:
            pf = format_info["physical_format"]
        elif "format_name" in format_info:
            format_name = format_info["format_name"]
            all_formats = FilesystemRegistry.get_all_formats()
            profile = all_formats.get(format_name)
            if not profile or not profile.physical_format:
                return False, f"Unknown or invalid format name: {format_name}"
            pf = profile.physical_format
        elif "cylinders" in format_info:
            try:
                cylinders = format_info.get("cylinders", DEFAULT_CYLINDERS)
                heads = format_info.get("heads", DEFAULT_HEADS)
                sectors_per_track = format_info.get("sectors_per_track", 18)
                bytes_per_sector = format_info.get("bytes_per_sector", DEFAULT_BPS)
                encoding = format_info.get("encoding", ENCODING_MFM)
                rate = format_info.get("rate", DEFAULT_RATE_MFM)

                track_format = TrackFormat(
                    track_start=0,
                    track_end=cylinders - 1,
                    head_start=0,
                    head_end=heads - 1,
                    sectors_per_track=sectors_per_track,
                    encoding=encoding,
                    rate=rate,
                    interleave=DEFAULT_INTERLEAVE,
                    bytes_per_sector=bytes_per_sector,
                )
                pf = PhysicalFormat(
                    cylinders=cylinders,
                    heads=heads,
                    rpm=DEFAULT_RPM,
                    heads_inverted=False,
                    bytes_per_sector=bytes_per_sector,
                    track_formats=[track_format],
                )
            except Exception as e:
                return False, f"Invalid geometry parameters: {e}"
        else:
            return False, "No recognizable format information provided"

        if self.drive_size == DRIVE_SIZE_3_5 and pf.cylinders > MAX_CYLINDERS_3_5:
            return (
                False,
                f'Format has {pf.cylinders} cylinders, 3.5" drives support max {MAX_CYLINDERS_3_5}',
            )
        elif self.drive_size == DRIVE_SIZE_5_25 and pf.cylinders > MAX_CYLINDERS_5_25:
            return (
                False,
                f'Format has {pf.cylinders} cylinders, 5.25" drives support max {MAX_CYLINDERS_5_25}',
            )
        elif self.drive_size == DRIVE_SIZE_8 and pf.cylinders > MAX_CYLINDERS_8:
            return (
                False,
                f'Format has {pf.cylinders} cylinders, 8" drives support max {MAX_CYLINDERS_8}',
            )

        return True, None

    def _physical_sector_id(self, cylinder: int, head: int, logical_sector: int) -> int:
        """
        Maps a 0-based logical sector index to its on-disk IDAM id.

        The project contract delivers 0-based logical sector indices to drivers
        (see Disk.read_sector). The Greaseweazle codec stamps sector ids
        sequentially from id_start and handles interleave as a physical
        placement concern, so the logical->id map is the linear id_start + index.

        Args:
            cylinder: The cylinder number.
            head: The head number.
            logical_sector: The 0-based logical sector index.

        Returns:
            The physical sector id (IDAM 'r') for that logical sector.
        """
        id_start = 1
        if self.physical_format:
            with contextlib.suppress(ValueError):
                id_start = self.physical_format.get_track_format(
                    cylinder, head
                ).id_start
        return id_start + logical_sector

    def read_sector(self, cylinder: int, head: int, sector: int) -> bytes:
        """
        Reads a single sector from the physical disk.

        Args:
            cylinder: The cylinder number.
            head: The head number.
            sector: The 0-based logical sector index.

        Returns:
            The sector data as a bytes object. Returns a zero-filled buffer on failure.
        """
        self.logger.debug(f"Reading sector C:{cylinder} H:{head} LS:{sector}")
        self.initialize()
        physical_id = self._physical_sector_id(cylinder, head, sector)
        track_id = (cylinder, head)

        # Read-your-writes: a buffered (not-yet-flushed) write wins over the
        # stale on-disk contents.
        pending = self.dirty_sectors.get(track_id)
        if pending is not None and physical_id in pending:
            self.logger.debug(f"Sector LS:{sector} served from pending write")
            return pending[physical_id]

        sector_key = (cylinder, head, sector)
        if sector_key in self.sector_cache:
            self.logger.debug(f"Sector {sector_key} found in cache")
            return self.sector_cache[sector_key]

        if track_id not in self.track_data:
            try:
                self._read_track(cylinder, head)
            except Exception as e:
                self.logger.error(f"Failed to read track C:{cylinder} H:{head}: {e}")
                self.track_data[track_id] = {}

        if track_id in self.track_data and physical_id in self.track_data[track_id]:
            data = self.track_data[track_id][physical_id]
            self.sector_cache[sector_key] = data
            self.logger.debug(f"Sector {sector_key} cached")
            return data

        bytes_per_sector = (
            self.physical_format.get_bytes_per_sector(cylinder, head)
            if self.physical_format
            else DEFAULT_BPS
        )
        self.logger.warning(
            f"Returning default zero-filled data for unreadable sector {sector_key}"
        )
        return b"\x00" * bytes_per_sector

    def set_physical_format(self, physical_format: PhysicalFormat) -> None:
        """
        Sets the physical disk format and recreates the Greaseweazle diskdef.

        Args:
            physical_format: The physical format to set.

        Raises:
            TypeError: If physical_format is not a PhysicalFormat object.
        """
        self.logger.debug("Setting physical format for Greaseweazle driver")
        if not isinstance(physical_format, PhysicalFormat):
            raise TypeError("physical_format must be a PhysicalFormat object")

        self.physical_format = copy.deepcopy(physical_format)

        self.fmt_cls = None
        self.using_custom_diskdef = False
        self.last_successful_format = None

        # Decoded sector data and pending writes were interpreted under the
        # previous geometry; discard them so they are not served or flushed
        # under the new format.
        if self.dirty_tracks:
            self.logger.warning(
                "Discarding %d dirty track(s) on physical format change",
                len(self.dirty_tracks),
            )
        self.track_data.clear()
        self.sector_cache.clear()
        self.dirty_sectors.clear()
        self.dirty_tracks.clear()

        self._create_and_set_custom_diskdef()

        self.logger.info(
            f"Physical format set and diskdef recreated: "
            f"Cyls={physical_format.cylinders}, Heads={physical_format.heads}"
        )

    def validate_for_opening(
        self, _source: str, **kwargs
    ) -> tuple[bool, Optional[str]]:
        """
        Validates whether the Greaseweazle can be opened.

        Args:
            _source: Device name (can be None for auto-detection), unused.
            **kwargs: Must contain 'drive_letter' and 'drive_size'.

        Returns:
            Tuple of (is_valid, error_message).
        """
        if not GREASEWEAZLE_AVAILABLE:
            return False, "Greaseweazle library not installed"

        drive_letter = kwargs.get("drive_letter", self.drive)
        drive_size = kwargs.get("drive_size", self.drive_size)

        if drive_letter not in VALID_DRIVE_LETTERS:
            return (
                False,
                f"Invalid drive letter: {drive_letter}. Must be {' or '.join(VALID_DRIVE_LETTERS)}",
            )

        if drive_size not in VALID_DRIVE_SIZES:
            return (
                False,
                f"Invalid drive size: {drive_size}. Must be {', '.join(VALID_DRIVE_SIZES)}",
            )

        return True, None

    def validate_state_for_opening(self) -> tuple[bool, Optional[str]]:
        """
        Validates Greaseweazle driver state after opening.

        For physical drives, physical_format is not required at open time
        since it can be detected during I/O operations.

        Returns:
            Tuple of (is_valid, error_message).
        """
        if not GREASEWEAZLE_AVAILABLE:
            return False, "Greaseweazle library not available"

        return True, None

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
        self.logger.debug(
            f"Writing sector C:{cylinder} H:{head} LS:{sector}, {len(data)} bytes"
        )
        self.initialize()
        physical_id = self._physical_sector_id(cylinder, head, sector)
        track_id = (cylinder, head)
        self.dirty_sectors.setdefault(track_id, {})[physical_id] = data
        self.dirty_tracks.add(track_id)
        sector_key = (cylinder, head, sector)
        if sector_key in self.sector_cache:
            del self.sector_cache[sector_key]
            self.logger.debug(f"Cleared cache for sector {sector_key}")

    def _convert_to_flux(self, cylinder: int, head: int) -> list[int]:
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
                if (
                    track_id in self.dirty_sectors
                    and sector_num in self.dirty_sectors[track_id]
                ):
                    data = self.dirty_sectors[track_id][sector_num]
                    s.dam.data = bytearray(
                        data[: len(s.dam.data)]
                        if len(data) > len(s.dam.data)
                        else data + bytes(len(s.dam.data) - len(data))
                    )
                elif (
                    track_id in self.track_data
                    and sector_num in self.track_data[track_id]
                ):
                    s.dam.data = bytearray(self.track_data[track_id][sector_num])
                s.crc = s.idam.crc = s.dam.crc = 0

        master_track = track.master_track()
        self.drive_ticks_per_rev = self.drive_ticks_per_rev or (
            (60.0 / self.physical_format.rpm) * self.usb.sample_freq
            if self.physical_format and self.physical_format.rpm
            else FALLBACK_TICKS_MULTIPLIER * self.usb.sample_freq
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

    def _get_formats_to_try(self) -> list[tuple[str, Optional[int]]]:
        """
        Determines the sequence of formats to attempt when reading a track.

        Returns:
            A list of format tuples (format_name, rate) to try.
        """
        formats = []
        if self.fmt_cls and self.using_custom_diskdef:
            formats.append((FORMAT_CUSTOM, None))
        if self.last_successful_format:
            formats.append(self.last_successful_format)
        if not self.fmt_cls or not self.using_custom_diskdef:
            formats.append((FORMAT_IBM_SCAN, None))
        if not self.last_successful_format and self.physical_format:
            formats.append((FORMAT_IBM_MFM, DEFAULT_RATE_MFM))
        self.logger.debug(f"Formats to try: {formats}")
        return formats

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

    def _read_track_with_format(
        self, cylinder: int, head: int, format_tuple: tuple[str, Optional[int]]
    ) -> Optional[dict[int, bytes]]:
        """
        Attempts to read a track using a single, specified Greaseweazle format.

        Args:
            cylinder: The cylinder number.
            head: The head number.
            format_tuple: A tuple of (format_name, rate) to use for the read attempt.

        Returns:
            A dictionary of {sector_number: sector_data} on success, otherwise None.
        """
        self.logger.debug(
            f"Reading track C:{cylinder} H:{head} with format {format_tuple}"
        )
        format_name, _ = format_tuple
        fmt_cls = self.fmt_cls if format_name == FORMAT_CUSTOM else None
        if not fmt_cls:
            try:
                fmt_cls = codec.get_diskdef(format_name)
            except Exception as e:
                self.logger.error(
                    f"Failed to get disk definition for {format_name}: {e}"
                )
                return None

        args = types.SimpleNamespace(
            revs=DEFAULT_REVS,
            raw=False,
            fmt_cls=fmt_cls,
            tracks=util.TrackSet(f"c={cylinder}:h={head}"),
            retries=DEFAULT_RETRIES,
            seek_retries=DEFAULT_SEEK_RETRIES,
            reverse=False,
            adjust_speed=None,
            fake_index=None,
            hard_sectors=False,
            drive=self.drive_obj,
            ticks=0,
            drive_ticks_per_rev=self.drive_ticks_per_rev,
        )
        success = False

        def read_track_wrapper():
            nonlocal success
            try:
                track_iter = util.TrackSet.TrackIter(args.tracks)
                next(track_iter)
                _, dat = read.read_with_retry(self.usb, args, track_iter)
                if dat and (
                    sectors := getattr(getattr(dat, "track", None), "sectors", None)
                    or getattr(dat, "sectors", None)
                ):
                    self.scan_track_object = dat
                    sector_data = {
                        s.idam.r: bytes(s.dam.data)
                        for s in sectors
                        if hasattr(s, "idam")
                        and hasattr(s, "dam")
                        and hasattr(s.dam, "data")
                    }
                    if sector_data:
                        self.logger.debug(f"Read sectors: {list(sector_data.keys())}")
                        self.track_data[(cylinder, head)] = sector_data
                        self.fmt_cls = fmt_cls
                        if format_tuple[0] == FORMAT_IBM_SCAN:
                            self._update_physical_format(dat, len(sector_data))
                            self._create_and_set_custom_diskdef()
                        if format_tuple[0] != FORMAT_CUSTOM:
                            self.last_successful_format = format_tuple
                        success = True
            except Exception as e:
                self.logger.warning(f"Track read error with format {format_tuple}: {e}")

        util.with_drive_selected(read_track_wrapper, self.usb, self.drive_obj)
        if success:
            self.logger.debug(f"Track C:{cylinder} H:{head} read successfully")
            return self.track_data.get((cylinder, head))
        return None

    def _update_physical_format(self, dat: Any, num_sectors: int) -> None:
        """
        Updates the driver's physical_format based on a successful 'ibm.scan'.

        Only the track-level fields the scan actually measured (encoding, rate,
        sector count) are updated; the overall disk geometry (cylinders, heads,
        rpm, sector size) is preserved from the existing format or the drive-size
        defaults so a single-track scan cannot inflate the geometry.

        Args:
            dat: The data object returned from a successful Greaseweazle read.
            num_sectors: The number of sectors found on the track.
        """
        if (
            not hasattr(dat, "track")
            or not hasattr(dat.track, "mode")
            or not hasattr(dat.track, "clock")
        ):
            self.logger.debug("Insufficient data to update physical format")
            return

        mode = dat.track.mode
        encoding = ENCODING_MFM if str(mode) == IBM_MFM_MODE else ENCODING_FM
        rate = int(
            1.0
            / (
                dat.track.clock
                * (MFM_CLOCK_DIVISOR if encoding == ENCODING_MFM else FM_CLOCK_DIVISOR)
            )
        )
        self.logger.debug(
            f"Updating format: encoding={encoding}, rate={rate}, sectors={num_sectors}"
        )

        existing = self.physical_format
        cylinders = existing.cylinders if existing else DEFAULT_CYLINDERS
        heads = existing.heads if existing else DEFAULT_HEADS
        rpm = existing.rpm if existing else DEFAULT_RPM
        heads_inverted = existing.heads_inverted if existing else False
        bytes_per_sector = existing.bytes_per_sector if existing else DEFAULT_BPS

        track_format = TrackFormat(
            track_start=0,
            track_end=cylinders - 1,
            head_start=0,
            head_end=heads - 1,
            sectors_per_track=num_sectors,
            encoding=encoding,
            rate=rate,
            gap3_bytes=DEFAULT_GAP3,
            interleave=DEFAULT_INTERLEAVE,
        )
        self.physical_format = PhysicalFormat(
            cylinders=cylinders,
            heads=heads,
            rpm=rpm,
            heads_inverted=heads_inverted,
            bytes_per_sector=bytes_per_sector,
            track_formats=[track_format],
        )
