# drivers.py

from dataclasses import dataclass
from typing import List, Dict, Optional, Callable, Union, Any
import struct

from greaseweazle.tools import util
from greaseweazle import usb as USB
from greaseweazle.codec import codec  # Correct import


@dataclass
class PhysicalFormat:
    encoding: str  # FM/MFM
    rate: int      # Data rate (kbps)
    rpm: int       # Rotations per minute
    gap3: int      # Gap3 size
    skew: int = 0  # Sector skew
    interleave: int = 1
    # Additional fields needed for disk geometry
    sectors_per_track: int = 18
    heads: int = 2
    sector_size: int = 512

class DiskIODriver:
    def read_sector(self, cylinder: int, head: int, sector: int) -> bytes:
        raise NotImplementedError("Subclasses must implement read_sector")

    def write_sector(self, cylinder: int, head: int, sector: int, data: bytes) -> None:
        raise NotImplementedError("Subclasses must implement write_sector")

    def flush(self) -> None:
        raise NotImplementedError("Subclasses must implement flush")

    def set_physical_format(self, physical_format: PhysicalFormat) -> None:
        raise NotImplementedError("Subclasses must implement set_physical_format")

class GreaseweazleDriver(DiskIODriver):
    def __init__(self, device_name=None, drive='A'):
        self.device_name = device_name
        self.drive = drive
        self.usb = None
        self.drive_obj = None
        self.physical_format = None
        self.dirty_sectors = {}
        self.dirty_tracks = set()
        self.track_data = {}
        self.initialized = False
        self.fmt_cls = None

    def initialize(self):
        if self.initialized:
            return

        self.usb = util.usb_open(self.device_name)
        self.drive_obj = util.Drive()(self.drive)

        # Set default format for initial reads
        if self.physical_format and not self.fmt_cls:
            if self.physical_format.encoding == "MFM":
                format_name = "ibm.mfm"
            else:
                format_name = "ibm.fm"
            try:
                # Using the correct import
                self.fmt_cls = codec.get_diskdef(format_name)
            except Exception as e:
                print(f"Warning: Failed to get disk definition: {e}")

        self.initialized = True

    def read_sector(self, cylinder: int, head: int, sector: int) -> bytes:
        self.initialize()
        track_id = (cylinder, head)

        if track_id not in self.track_data:
            try:
                self._read_track(cylinder, head)
            except Exception as e:
                print(f"Error reading track {cylinder}.{head}: {e}")
                # Create empty track data to prevent future retries
                self.track_data[track_id] = {}

        if track_id in self.track_data and sector in self.track_data[track_id]:
            return self.track_data[track_id][sector]

        sector_size = 512
        if self.physical_format:
            sector_size = self.physical_format.sector_size
        return b'\x00' * sector_size

    def write_sector(self, cylinder: int, head: int, sector: int, data: bytes) -> None:
        self.initialize()
        track_id = (cylinder, head)

        if track_id not in self.dirty_sectors:
            self.dirty_sectors[track_id] = {}

        self.dirty_sectors[track_id][sector] = data
        self.dirty_tracks.add(track_id)

    def flush(self) -> None:
        if not self.initialized or not self.dirty_tracks:
            return

        def write_tracks():
            for track_id in sorted(self.dirty_tracks):
                cylinder, head = track_id
                self.usb.seek(cylinder, head)
                flux_list = self._convert_to_flux(cylinder, head)
                self.usb.write_track(
                    flux_list=flux_list,
                    cue_at_index=True,
                    terminate_at_index=True
                )

        util.with_drive_selected(write_tracks, self.usb, self.drive_obj)
        self.dirty_tracks.clear()
        self.dirty_sectors.clear()

    def set_physical_format(self, physical_format: PhysicalFormat) -> None:
        self.physical_format = physical_format

        # Update fmt_cls based on the new physical format
        if self.initialized:
            if physical_format.encoding == "MFM":
                format_name = "ibm.mfm"
            else:
                format_name = "ibm.fm"
            try:
                self.fmt_cls = codec.get_diskdef(format_name)
            except Exception as e:
                print(f"Warning: Failed to get disk definition: {e}")

    def _read_track(self, cylinder: int, head: int) -> None:
        from greaseweazle.tools import read
        import types

        args = types.SimpleNamespace(
            revs=2, raw=True,  # Changed to raw=True to capture flux even if no sectors found
            fmt_cls=self.fmt_cls,
            tracks=util.TrackSet(f'c={cylinder}:h={head}'),
            retries=3, seek_retries=0, reverse=False,
            adjust_speed=None, fake_index=None, hard_sectors=False,
            drive=self.drive_obj, ticks=0, drive_ticks_per_rev=None
        )

        # If fmt_cls isn't set yet, try to set it
        if not args.fmt_cls:
            try:
                if self.physical_format and self.physical_format.encoding == "MFM":
                    format_name = "ibm.mfm"
                else:
                    format_name = "ibm.fm"
                args.fmt_cls = codec.get_diskdef(format_name)
            except Exception as e:
                print(f"Warning: Failed to get disk definition for track read: {e}")

        def read_track_wrapper():
            track_iterator = util.TrackSet.TrackIter(args.tracks)
            next(track_iterator)
            flux, dat = read.read_with_retry(self.usb, args, track_iterator)

            # Initialize empty track data
            self.track_data[(cylinder, head)] = {}

            # Try to extract sector information if available
            if dat is not None:
                sectors = None
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
                    self.track_data[(cylinder, head)] = sector_data
                    print(f"Found {len(sector_data)} sectors on track {cylinder}.{head}")
                else:
                    print(f"No sectors found on track {cylinder}.{head}, but flux data was read")

            # Even if no sectors are found, we've read the track's flux
            # Return true to avoid exceptions
            return True

        try:
            util.with_drive_selected(read_track_wrapper, self.usb, self.drive_obj)
        except Exception as e:
            print(f"Error in read_track_wrapper: {e}")
            # Create empty track data to prevent future retries
            self.track_data[(cylinder, head)] = {}

    def _convert_to_flux(self, cylinder: int, head: int) -> List[int]:
        from greaseweazle.track import MasterTrack

        if not self.fmt_cls:
            if self.physical_format.encoding == "MFM":
                format_name = "ibm.mfm"
            else:
                format_name = "ibm.fm"
            try:
                self.fmt_cls = codec.get_diskdef(format_name)
            except Exception as e:
                raise ValueError(f"Failed to get disk definition: {e}")

        track_def = None
        for key, value in self.fmt_cls.track_map.items():
            track_def = value
            break

        if not track_def:
            raise ValueError(f"No track definition found")

        track = track_def.mk_track(cylinder, head)
        track_id = (cylinder, head)

        for s in track.sectors:
            if hasattr(s, 'idam') and hasattr(s.idam, 'r'):
                sector_num = s.idam.r
                if track_id in self.dirty_sectors and sector_num in self.dirty_sectors[track_id]:
                    s.dam.data = bytearray(self.dirty_sectors[track_id][sector_num])
                    s.crc = s.idam.crc = s.dam.crc = 0

        master_track = track.master_track()

        # Get drive parameters
        def measure_rpm():
            flux = self.usb.read_track(2)
            self.drive_ticks_per_rev = flux.ticks_per_rev

        util.with_drive_selected(measure_rpm, self.usb, self.drive_obj)

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

class RawImageDriver(DiskIODriver):
    def __init__(self, file_path, image_data=None):
        self.file_path = file_path
        self.physical_format = None
        self.dirty = False
        self.geometry_set = False

        if image_data is not None:
            self.image_data = bytearray(image_data)
            self.dirty = True
        else:
            with open(file_path, 'rb') as f:
                self.image_data = bytearray(f.read())

    def read_sector(self, cylinder: int, head: int, sector: int) -> bytes:
        # For the boot sector (and a few others), support reading without geometry
        if not self.geometry_set and cylinder == 0 and head == 0 and sector <= 3:
            # Use standard 512-byte sectors as a fallback
            sector_size = 512 if not self.physical_format else self.physical_format.sector_size
            offset = (sector - 1) * sector_size
            if offset + sector_size <= len(self.image_data):
                return bytes(self.image_data[offset:offset + sector_size])
            return b'\x00' * sector_size

        if not self.physical_format:
            raise ValueError("Physical format not set")

        offset = self._calculate_sector_offset(cylinder, head, sector)
        if offset + self.physical_format.sector_size <= len(self.image_data):
            return bytes(self.image_data[offset:offset + self.physical_format.sector_size])
        return b'\x00' * self.physical_format.sector_size

    def write_sector(self, cylinder: int, head: int, sector: int, data: bytes) -> None:
        if not self.physical_format:
            raise ValueError("Physical format not set")

        offset = self._calculate_sector_offset(cylinder, head, sector)
        self.image_data[offset:offset + len(data)] = data
        self.dirty = True

    def flush(self) -> None:
        if self.dirty:
            with open(self.file_path, 'wb') as f:
                f.write(self.image_data)
            self.dirty = False

    def set_physical_format(self, physical_format: PhysicalFormat) -> None:
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
        if offset + length <= len(self.image_data):
            return bytes(self.image_data[offset:offset + length])
        return b'\x00' * length
