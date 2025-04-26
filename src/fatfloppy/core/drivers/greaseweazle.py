# src/fatfloppy/core/drivers/greaseweazle.py
import copy
import types
import logging
from typing import List, Optional, Tuple, Dict

from ..drivers.base_driver import DiskIODriver
from ..physical_format import PhysicalFormat, TrackFormat
from ..utils.logging_config import get_logger

# Greaseweazle imports - keep them here as they are specific to GreaseweazleDriver
try:
    from greaseweazle.tools import util
    from greaseweazle.tools import read
    from greaseweazle.codec import codec
    from greaseweazle.codec.ibm import ibm
    GREASEWEAZLE_AVAILABLE = True
except ImportError:
    GREASEWEAZLE_AVAILABLE = False
    util = None # Define placeholders if GW is not available
    read = None
    ibm = None
    codec = None
    types = None


logger = get_logger()


def create_greaseweazle_diskdef(
    physical_format: PhysicalFormat,
    logger: logging.Logger,
) -> Optional[codec.DiskDef]:
    if not physical_format:
        logger.warning("Cannot create custom diskdef: physical format not provided")
        return None

    logger.debug("Creating custom disk definition")

    try:
        disk_def = codec.DiskDef()
        disk_def.cyls = physical_format.cylinders
        disk_def.heads = physical_format.heads

        # Create track definitions for each TrackFormat
        for tf in physical_format.track_formats:
            # Determine format name based on encoding
            if tf.encoding == "MFM":
                format_name = "ibm.mfm"
            elif tf.encoding == "FM":
                format_name = "ibm.fm"
            else:
                logger.warning(f"Unsupported encoding '{tf.encoding}' for track format, defaulting to ibm.mfm")
                format_name = "ibm.mfm"

            # Create a track definition for this TrackFormat
            track_def = ibm.IBMTrack_FixedDef(format_name)
            track_def.add_param("secs", str(tf.sectors_per_track))
            track_def.add_param("bps", str(physical_format.bytes_per_sector))
            track_def.add_param("gap3", str(tf.gap3))
            track_def.add_param("rate", str(tf.rate))
            track_def.finalise()

            # Assign this track definition to the specified cylinder and head ranges
            for c in range(tf.track_start, tf.track_end + 1):
                for h in range(tf.head_start, tf.head_end + 1):
                    disk_def.track_map[(c, h)] = track_def

        disk_def.finalise()

        logger.info(
            f"Custom disk definition created: Cyls={physical_format.cylinders}, Heads={physical_format.heads}, "
            f"with {len(physical_format.track_formats)} track formats"
        )
        return disk_def
    except Exception as e:
        logger.error(f"Failed to create custom disk definition: {e}", exc_info=True)
        return None

class GreaseweazleDriver(DiskIODriver):
    def __init__(self, device_name=None, drive="A", drive_size="3.5"):
        super().__init__()
        if not GREASEWEAZLE_AVAILABLE:
            raise ImportError("Greaseweazle library not found. Physical drive access unavailable.")
        self.device_name = device_name
        self.drive = drive
        self.drive_size = drive_size
        self.physical_format = None
        self.dirty_sectors = {}
        self.dirty_tracks = set()
        self.track_data = {}
        self.sector_cache = {}
        self.initialized = False
        self.fmt_cls = None
        self.drive_ticks_per_rev = None
        self.last_successful_format = None
        self.using_custom_diskdef = False
        self.scan_track_object = None
        self.verify_writes = True
        self.uses_physical_heads = True  # Added to indicate physical head usage

    def initialize(self):
        if not GREASEWEAZLE_AVAILABLE:
             self.logger.error("Cannot initialize GreaseweazleDriver: library not available.")
             return # Or raise error
        if self.initialized:
            return
        self.usb = util.usb_open(self.device_name)
        self.drive_obj = util.Drive()(self.drive)
        try:
            def measure_rpm():
                flux = self.usb.read_track(2)
                self.drive_ticks_per_rev = flux.ticks_per_rev
            util.with_drive_selected(measure_rpm, self.usb, self.drive_obj)
        except Exception as e:
            # TODO: If failed to measure RPM - we should stop initializing and quit/close disk, as this tells us that either drive isn't working or something is wrong with floppy disk itself. Or it's not there?) CRYTICAL!
            # normally Greaseweazle CLI in this case will return something like "No index found"
            self.logger.warning(f"Failed to measure RPM: {e}")
            self.drive_ticks_per_rev = 0.2 * self.usb.sample_freq
        self.initialized = True

    def read_sector(self, cylinder: int, head: int, sector: int) -> bytes:
        self.initialize()
        sector_key = (cylinder, head, sector)
        if sector_key in self.sector_cache:
            return self.sector_cache[sector_key]
        track_id = (cylinder, head)
        if track_id not in self.track_data:
            try:
                self._read_track(cylinder, head)
            except Exception as e:
                self.logger.error(f"Error reading track C:{cylinder} H:{head}: {e}")
                self.track_data[track_id] = {}
        if track_id in self.track_data and sector in self.track_data[track_id]:
            self.sector_cache[sector_key] = self.track_data[track_id][sector]
            return self.track_data[track_id][sector]
        bytes_per_sector = 512 if not self.physical_format else self.physical_format.bytes_per_sector
        return b"\x00" * bytes_per_sector

    def write_sector(self, cylinder: int, head: int, sector: int, data: bytes) -> None:
        self.initialize()
        track_id = (cylinder, head)
        if track_id not in self.dirty_sectors:
            self.dirty_sectors[track_id] = {}
        self.dirty_sectors[track_id][sector] = data
        self.dirty_tracks.add(track_id)
        sector_key = (cylinder, head, sector)
        if sector_key in self.sector_cache:
            del self.sector_cache[sector_key]

    def flush(self) -> None:
        if not self.initialized or not self.dirty_tracks:
            return
        if not self.fmt_cls and self.physical_format:
            try:
                self._create_and_set_custom_diskdef()
            except Exception as e:
                self.logger.error(f"Failed to create disk definition: {e}")
                return
        tracks_to_read = [
            track_id for track_id in self.dirty_tracks
            if len(self.dirty_sectors.get(track_id, {})) < self.physical_format.get_sectors_per_track(track_id[0], track_id[1])
        ]
        for track_id in tracks_to_read:
            cylinder, head = track_id
            try:
                self._read_track(cylinder, head)
            except Exception as e:
                self.logger.error(f"Failed to read track C:{cylinder} H:{head}: {e}")
        tracks_to_write = sorted(list(self.dirty_tracks))
        successfully_written = []
        for track_id in tracks_to_write:
            cylinder, head = track_id
            result = [False]
            def write_track_wrapper():
                try:
                    flux_list = self._convert_to_flux(cylinder, head)
                    self.usb.seek(cylinder, head)
                    self.usb.write_track(flux_list=flux_list, cue_at_index=True, terminate_at_index=True)
                    result[0] = True
                except Exception as e:
                    self.logger.error(f"Error writing track C:{cylinder} H:{head}: {e}", exc_info=True)
            try:
                util.with_drive_selected(write_track_wrapper, self.usb, self.drive_obj, motor=True)
                if result[0]:
                    successfully_written.append(track_id)
                else:
                    self.logger.warning(f"Failed to write track C:{cylinder} H:{head}")
                    if track_id in self.track_data:
                        del self.track_data[track_id]
            except Exception as e:
                self.logger.error(f"Drive selection error for track C:{cylinder} H:{head}: {e}", exc_info=True)
        for track_id in successfully_written:
            cylinder, head = track_id
            if track_id in self.dirty_sectors:
                if len(self.dirty_sectors[track_id]) == self.physical_format.get_sectors_per_track(cylinder, head):
                    self.track_data[track_id] = self.dirty_sectors[track_id].copy()
                elif track_id in self.track_data:
                    for sector, data in self.dirty_sectors[track_id].items():
                        self.track_data[track_id][sector] = data
                del self.dirty_sectors[track_id]
            if track_id in self.dirty_tracks:
                self.dirty_tracks.remove(track_id)

    def set_physical_format(self, physical_format: PhysicalFormat) -> None:
        if not isinstance(physical_format, PhysicalFormat):
            raise TypeError("physical_format must be a PhysicalFormat object")
        self.physical_format = copy.deepcopy(physical_format)
        self.fmt_cls = None
        self.using_custom_diskdef = False
        self.last_successful_format = None

    def _create_and_set_custom_diskdef(self):
        if not self.physical_format:
            self.fmt_cls = None
            self.using_custom_diskdef = False
            return
        disk_def = create_greaseweazle_diskdef(self.physical_format, self.logger)
        if disk_def:
            self.fmt_cls = disk_def
            self.using_custom_diskdef = True
        else:
            self.fmt_cls = None
            self.using_custom_diskdef = False

    def _get_formats_to_try(self) -> List[Tuple[str, Optional[int]]]:
        formats_to_try = []
        if self.fmt_cls and self.using_custom_diskdef:
            formats_to_try.append(("custom", None))
        if self.last_successful_format:
            formats_to_try.append(self.last_successful_format)
        if not self.fmt_cls or not self.using_custom_diskdef:
            formats_to_try.append(("ibm.scan", None))
        if not self.last_successful_format and self.physical_format:
            # Default to a common rate since rate is now track-specific
            formats_to_try.append(("ibm.mfm", 500))  # Default to MFM 500 kbps
        return formats_to_try

    def _update_physical_format(self, dat, num_sectors: int) -> None:
        if not hasattr(dat, "track") or not hasattr(dat.track, "mode"):
            return
        mode = dat.track.mode
        encoding = "MFM" if str(mode) == "IBM MFM" else "FM"
        if not hasattr(dat.track, "clock"):
            return
        rate = int(1.0 / (dat.track.clock * (2000 if encoding == "MFM" else 1000)))
        track_format = TrackFormat(
            track_start=0,
            track_end=79,  # Default cylinders - 1
            head_start=0,
            head_end=1,    # Default heads - 1
            sectors_per_track=num_sectors,
            encoding=encoding,
            rate=rate,
            gap3=84,
            interleave=1
        )
        self.physical_format = PhysicalFormat(
            cylinders=80,
            heads=2,
            rpm=300,
            heads_inverted=False,
            bytes_per_sector=512,
            track_formats=[track_format]
        )

    def _read_track_with_format(self, cylinder: int, head: int, format_tuple: Tuple[str, Optional[int]]) -> Optional[Dict[int, bytes]]:
        from greaseweazle.codec import codec
        import types

        format_name, rate = format_tuple
        fmt_cls_to_try = self.fmt_cls if format_name == "custom" else None
        if not fmt_cls_to_try:
            try:
                fmt_cls_to_try = codec.get_diskdef(format_name)
            except Exception as e:
                self.logger.error(f"Failed to get disk definition for {format_name}: {e}")
                return None

        args = types.SimpleNamespace(
            revs=3,
            raw=False,
            fmt_cls=fmt_cls_to_try,
            tracks=util.TrackSet(f"c={cylinder}:h={head}"),
            retries=2,
            seek_retries=0,
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
            track_iterator = util.TrackSet.TrackIter(args.tracks)
            next(track_iterator)
            flux, dat = read.read_with_retry(self.usb, args, track_iterator)
            if dat is None:
                return
            self.scan_track_object = dat
            sectors = getattr(getattr(dat, "track", None), "sectors", None) or getattr(dat, "sectors", None)
            if not sectors:
                return
            sector_data = {
                s.idam.r: bytes(s.dam.data)
                for s in sectors
                if hasattr(s, "idam") and hasattr(s, "dam") and hasattr(s.dam, "data")
            }
            if not sector_data:
                return
            self.track_data[(cylinder, head)] = sector_data
            self.fmt_cls = fmt_cls_to_try
            if format_tuple[0] == "ibm.scan":
                self._update_physical_format(dat, len(sector_data))
                self._create_and_set_custom_diskdef()
            if format_tuple[0] != "custom":
                self.last_successful_format = format_tuple
            success = True

        try:
            util.with_drive_selected(read_track_wrapper, self.usb, self.drive_obj)
            if success:
                return self.track_data[(cylinder, head)]
        except Exception as e:
            self.logger.error(f"Error reading track with format {format_tuple[0]}: {e}")
        return None

    def _read_track(self, cylinder: int, head: int) -> bool:
        self.initialize()
        self.track_data[(cylinder, head)] = {}
        formats_to_try = self._get_formats_to_try()
        for format_tuple in formats_to_try:
            sector_data = self._read_track_with_format(cylinder, head, format_tuple)
            if sector_data:
                return True
        return False

    def _convert_to_flux(self, cylinder: int, head: int) -> List[int]:
        if not self.fmt_cls:
            if self.physical_format:
                self._create_and_set_custom_diskdef()
                if not self.fmt_cls:
                    raise ValueError("Failed to create necessary disk definition for writing.")
            else:
                raise ValueError("No physical format defined and no existing format class, cannot write.")
        if not hasattr(self.fmt_cls, "track_map"):
            raise TypeError(f"Internal error: self.fmt_cls is not a valid DiskDef object (type: {type(self.fmt_cls)}).")
        track_coords = (cylinder, head)
        track_def = self.fmt_cls.track_map.get(track_coords)
        if not track_def:
            raise ValueError(f"No track definition found for C:{cylinder} H:{head}")
        track = track_def.mk_track(cylinder, head)
        track_id = (cylinder, head)
        if track_id in self.track_data:
            for s in track.sectors:
                if hasattr(s, "idam") and hasattr(s.idam, "r"):
                    sector_num = s.idam.r
                    if sector_num in self.track_data[track_id]:
                        s.dam.data = bytearray(self.track_data[track_id][sector_num])
                        s.crc = s.idam.crc = s.dam.crc = 0
        if track_id in self.dirty_sectors:
            for s in track.sectors:
                if hasattr(s, "idam") and hasattr(s.idam, "r"):
                    sector_num = s.idam.r
                    if sector_num in self.dirty_sectors[track_id]:
                        sector_data = self.dirty_sectors[track_id][sector_num]
                        expected_size = len(s.dam.data)
                        if len(sector_data) != expected_size:
                            sector_data = (
                                sector_data + bytes(expected_size - len(sector_data))
                                if len(sector_data) < expected_size
                                else sector_data[:expected_size]
                            )
                        s.dam.data = bytearray(sector_data)
                        s.crc = s.idam.crc = s.dam.crc = 0
        master_track = track.master_track()
        if not self.drive_ticks_per_rev:
            self.drive_ticks_per_rev = (
                (60.0 / self.physical_format.rpm) * self.usb.sample_freq
                if self.physical_format and self.physical_format.rpm
                else 0.2 * self.usb.sample_freq
            )
        master_track.time_per_rev = self.drive_ticks_per_rev / self.usb.sample_freq
        wflux = master_track.flux_for_writeout(cue_at_index=True)
        factor = self.drive_ticks_per_rev / wflux.ticks_to_index
        rem = 0.0
        wflux_list = []
        for x in wflux.list:
            y = x * factor + rem
            val = round(y)
            rem = y - val
            wflux_list.append(val)
        return wflux_list

    def _write_track(self, cylinder: int, head: int) -> bool:
        track_id = (cylinder, head)
        if track_id not in self.dirty_sectors:
            return True
        try:
            flux_list = self._convert_to_flux(cylinder, head)
            self.usb.seek(cylinder, head)
            self.usb.write_track(flux_list=flux_list, cue_at_index=True, terminate_at_index=True)
            if self.verify_writes:
                if track_id in self.track_data:
                    del self.track_data[track_id]
                if track_id in self.dirty_tracks:
                    self.dirty_tracks.remove(track_id)
                if track_id in self.dirty_sectors:
                    del self.dirty_sectors[track_id]
                return self._read_track(cylinder, head)
            return True
        except Exception as e:
            self.logger.error(f"Error writing track C:{cylinder} H:{head}: {e}", exc_info=True)
            return False
