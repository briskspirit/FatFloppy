# src/fatfloppy/core/drivers/greaseweazle.py
import copy
import types
import logging
from typing import List, Optional, Tuple, Dict

from ..drivers.base_driver import DiskIODriver
from ..physical_format import PhysicalFormat, TrackFormat
from ..utils.logging_config import get_logger

try:
    from greaseweazle.tools import util
    from greaseweazle.tools import read
    from greaseweazle.codec import codec
    from greaseweazle.codec.ibm import ibm
    GREASEWEAZLE_AVAILABLE = True
except ImportError:
    GREASEWEAZLE_AVAILABLE = False
    util = None
    read = None
    ibm = None
    codec = None
    types = None

logger = get_logger()

def create_greaseweazle_diskdef(physical_format: PhysicalFormat, logger_instance: logging.Logger) -> Optional[codec.DiskDef]:
    if not physical_format:
        logger_instance.warning("Cannot create diskdef: No physical format provided")
        return None

    logger_instance.debug("Creating Greaseweazle disk definition")
    try:
        disk_def = codec.DiskDef()
        disk_def.cyls = physical_format.cylinders
        disk_def.heads = physical_format.heads

        for tf in physical_format.track_formats:
            format_name = "ibm.mfm" if tf.encoding == "MFM" else "ibm.fm" if tf.encoding == "FM" else None
            if not format_name:
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
    def __init__(self, device_name=None, drive="A", drive_size="3.5"):
        super().__init__()
        self.logger.debug(f"Initializing GreaseweazleDriver for drive {drive}")
        if not GREASEWEAZLE_AVAILABLE:
            raise ImportError("Greaseweazle library not found")
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
        self.uses_physical_heads = True

    def initialize(self):
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
            # TODO: If failed to measure RPM - we should stop initializing and quit/close disk, as this tells us that either drive isn't working or something is wrong with floppy disk itself. Or it's not there?) CRYTICAL!
            self.logger.warning(f"RPM measurement failed: {e}")
            self.drive_ticks_per_rev = 0.2 * self.usb.sample_freq
            self.logger.debug(f"Defaulting to ticks_per_rev={self.drive_ticks_per_rev}")

        self.initialized = True
        self.logger.info(f"Driver initialized for drive {self.drive}")

    def read_sector(self, cylinder: int, head: int, sector: int) -> bytes:
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
        self.logger.debug(f"Returning default data for sector {sector_key}")
        return b"\x00" * bytes_per_sector

    def write_sector(self, cylinder: int, head: int, sector: int, data: bytes) -> None:
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

    def set_physical_format(self, physical_format: PhysicalFormat) -> None:
        self.logger.debug("Setting physical format")
        if not isinstance(physical_format, PhysicalFormat):
            raise TypeError("physical_format must be a PhysicalFormat object")
        self.physical_format = copy.deepcopy(physical_format)
        self.fmt_cls = None
        self.using_custom_diskdef = False
        self.last_successful_format = None
        self.logger.info(f"Physical format set: Cyls={physical_format.cylinders}, Heads={physical_format.heads}")

    def _create_and_set_custom_diskdef(self):
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

    def _update_physical_format(self, dat, num_sectors: int) -> None:
        if not hasattr(dat, "track") or not hasattr(dat.track, "mode") or not hasattr(dat.track, "clock"):
            self.logger.debug("Insufficient data to update physical format")
            return

        mode = dat.track.mode
        encoding = "MFM" if str(mode) == "IBM MFM" else "FM"
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

    def _read_track_with_format(self, cylinder: int, head: int, format_tuple: Tuple[str, Optional[int]]) -> Optional[Dict[int, bytes]]:
        self.logger.debug(f"Reading track C:{cylinder} H:{head} with format {format_tuple}")
        format_name, rate = format_tuple
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
                flux, dat = read.read_with_retry(self.usb, args, track_iter)
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
                self.logger.error(f"Track read error with format {format_tuple}: {e}")

        util.with_drive_selected(read_track_wrapper, self.usb, self.drive_obj)
        if success:
            self.logger.debug(f"Track C:{cylinder} H:{head} read successfully")
            return self.track_data[(cylinder, head)]
        return None

    def _read_track(self, cylinder: int, head: int) -> bool:
        self.logger.debug(f"Attempting to read track C:{cylinder} H:{head}")
        self.initialize()
        self.track_data[(cylinder, head)] = {}
        for fmt in self._get_formats_to_try():
            if data := self._read_track_with_format(cylinder, head, fmt):
                return True
        self.logger.warning(f"No suitable format found for track C:{cylinder} H:{head}")
        return False

    def _convert_to_flux(self, cylinder: int, head: int) -> List[int]:
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
                    s.dam.data = bytearray(data[:len(s.dam.data)] if len(data) > len(s.dam.data) else data + bytes(len(s.dam.data) - len(data)))
                s.crc = s.idam.crc = s.dam.crc = 0

        master_track = track.master_track()
        self.drive_ticks_per_rev = self.drive_ticks_per_rev or (
            (60.0 / self.physical_format.rpm) * self.usb.sample_freq if self.physical_format and self.physical_format.rpm else 0.2 * self.usb.sample_freq
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

    def _write_track(self, cylinder: int, head: int) -> bool:
        self.logger.debug(f"Writing track C:{cylinder} H:{head}")
        track_id = (cylinder, head)
        if track_id not in self.dirty_sectors:
            self.logger.debug("No dirty sectors to write")
            return True

        try:
            flux_list = self._convert_to_flux(cylinder, head)
            self.usb.seek(cylinder, head)
            self.usb.write_track(flux_list=flux_list, cue_at_index=True, terminate_at_index=True)
            if self.verify_writes:
                self.track_data.pop(track_id, None)
                self.dirty_tracks.discard(track_id)
                self.dirty_sectors.pop(track_id, None)
                return self._read_track(cylinder, head)
            return True
        except Exception as e:
            self.logger.error(f"Failed to write track C:{cylinder} H:{head}: {e}", exc_info=True)
            return False

    def _update_after_write(self, cylinder: int, head: int):
        track_id = (cylinder, head)
        if track_id in self.dirty_sectors:
            sectors_per_track = self.physical_format.get_sectors_per_track(cylinder, head)
            if len(self.dirty_sectors[track_id]) == sectors_per_track:
                self.track_data[track_id] = self.dirty_sectors[track_id].copy()
            elif track_id in self.track_data:
                self.track_data[track_id].update(self.dirty_sectors[track_id])
            del self.dirty_sectors[track_id]
        self.dirty_tracks.discard(track_id)
        self.logger.debug(f"Track C:{cylinder} H:{head} updated post-write")
