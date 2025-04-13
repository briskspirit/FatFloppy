# drivers.py

from dataclasses import dataclass
from typing import List, Dict, Optional, Callable, Union, Any
import struct

from greaseweazle.tools import util
from greaseweazle import usb as USB
from greaseweazle.codec import codec
from greaseweazle.tools import read


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
        self.track_data = {}       # Track sector cache
        self.sector_cache = {}     # Direct sector cache for faster lookups
        self.initialized = False
        self.fmt_cls = None
        self.drive_ticks_per_rev = None

    def initialize(self):
        if self.initialized:
            return

        self.usb = util.usb_open(self.device_name)
        self.drive_obj = util.Drive()(self.drive)

        # Measure drive RPM
        try:
            def measure_rpm():
                flux = self.usb.read_track(2)
                self.drive_ticks_per_rev = flux.ticks_per_rev
                print(f"Measured drive RPM: {60 / (self.drive_ticks_per_rev / self.usb.sample_freq):.1f}")

            util.with_drive_selected(measure_rpm, self.usb, self.drive_obj)
        except Exception as e:
            print(f"Warning: Failed to measure RPM: {e}")
            # Use a reasonable default
            self.drive_ticks_per_rev = 0.2 * self.usb.sample_freq

        self.initialized = True

    def read_sector(self, cylinder: int, head: int, sector: int) -> bytes:
            self.initialize()

            # Check direct sector cache first for fastest access
            sector_key = (cylinder, head, sector)
            if sector_key in self.sector_cache:
                return self.sector_cache[sector_key]

            # Then check track cache
            track_id = (cylinder, head)
            if track_id not in self.track_data:
                try:
                    self._read_track(cylinder, head)
                except Exception as e:
                    print(f"Error reading track {cylinder}.{head}: {e}")
                    # Create empty track data to prevent future retries
                    self.track_data[track_id] = {}

            # After track read, check if sector is available
            if track_id in self.track_data and sector in self.track_data[track_id]:
                # Cache the sector directly for faster future access
                self.sector_cache[sector_key] = self.track_data[track_id][sector]
                return self.track_data[track_id][sector]

            # Sector not found - return empty sector
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

        # Make sure fmt_cls is set before writing
        if not self.fmt_cls and self.physical_format:
            try:
                if self.physical_format.encoding == "MFM":
                    format_name = "ibm.mfm"
                else:
                    format_name = "ibm.fm"
                self.fmt_cls = codec.get_diskdef(format_name)
            except Exception as e:
                print(f"Warning: Failed to get disk definition for writing: {e}")
                return

        def write_tracks():
            for track_id in sorted(self.dirty_tracks):
                cylinder, head = track_id
                self.usb.seek(cylinder, head)
                try:
                    flux_list = self._convert_to_flux(cylinder, head)
                    self.usb.write_track(
                        flux_list=flux_list,
                        cue_at_index=True,
                        terminate_at_index=True
                    )
                    print(f"Successfully wrote track {cylinder}.{head}")
                except Exception as e:
                    print(f"Error writing track {cylinder}.{head}: {e}")

        util.with_drive_selected(write_tracks, self.usb, self.drive_obj)
        self.dirty_tracks.clear()
        self.dirty_sectors.clear()

    def set_physical_format(self, physical_format: PhysicalFormat) -> None:
        self.physical_format = physical_format

        # Update fmt_cls based on the new physical format
        if self.initialized:
            try:
                if physical_format.encoding == "MFM":
                    format_name = "ibm.mfm"
                else:
                    format_name = "ibm.fm"
                self.fmt_cls = codec.get_diskdef(format_name)
            except Exception as e:
                print(f"Warning: Failed to get disk definition: {e}")

    def _read_track(self, cylinder: int, head: int) -> None:
        import types

        # List of formats to try
        formats_to_try = []

        # Add primary format if set
        if self.physical_format:
            if self.physical_format.encoding == "MFM":
                formats_to_try.append(("ibm.mfm", self.physical_format.rate))
            else:
                formats_to_try.append(("ibm.fm", self.physical_format.rate))

        # Add default formats to try if we don't find anything with primary format
        if not formats_to_try or formats_to_try[0][0] != "ibm.scan":
            formats_to_try.append(("ibm.scan", None))  # ibm.scan automatically tries various formats

        # Add other common formats
        additional_formats = [
            ("ibm.mfm", 500),  # HD 1.44MB
            ("ibm.mfm", 250),  # DD 720KB
            ("ibm.fm", 250),   # FM formats
        ]

        # Add additional formats if not already included
        for fmt in additional_formats:
            if fmt not in formats_to_try:
                formats_to_try.append(fmt)

        # Initialize empty track data to ensure there's something
        self.track_data[(cylinder, head)] = {}

        # Try each format until we find one that works
        for format_name, rate in formats_to_try:
            print(f"Trying to read track {cylinder}.{head} with format {format_name}" +
                  (f" at {rate}kbps" if rate else ""))

            try:
                # Get the format definition
                fmt_cls = codec.get_diskdef(format_name)

                # Create the arguments
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

                # Read the track
                def read_track_wrapper():
                    track_iterator = util.TrackSet.TrackIter(args.tracks)
                    next(track_iterator)
                    flux, dat = read.read_with_retry(self.usb, args, track_iterator)

                    sectors = None
                    if dat is not None:
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
                            print(f"Found {len(sector_data)} sectors on track {cylinder}.{head} using {format_name}")
                            # Use this data and exit the loop
                            self.track_data[(cylinder, head)] = sector_data
                            self.fmt_cls = fmt_cls
                            if self.physical_format is None and rate is not None:
                                # Update physical format if not set
                                encoding = "MFM" if format_name == "ibm.mfm" else "FM"
                                sectors_per_track = len(sector_data)
                                self.physical_format = PhysicalFormat(
                                    encoding=encoding,
                                    rate=rate,
                                    rpm=300,
                                    gap3=84,
                                    sectors_per_track=sectors_per_track,
                                    heads=2,
                                    sector_size=512
                                )
                            return True

                    return False

                success = False
                try:
                    util.with_drive_selected(read_track_wrapper, self.usb, self.drive_obj)
                    if self.track_data[(cylinder, head)]:
                        success = True
                        break
                except Exception as e:
                    print(f"Error trying format {format_name}: {e}")
                    continue

                if success:
                    break

            except Exception as e:
                print(f"Failed to try format {format_name}: {e}")
                continue

        # If we still didn't find any sectors, log it
        if not self.track_data[(cylinder, head)]:
            print(f"No sectors found on track {cylinder}.{head} after trying all formats")

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

        if track_id in self.dirty_sectors:
            for s in track.sectors:
                if hasattr(s, 'idam') and hasattr(s.idam, 'r'):
                    sector_num = s.idam.r
                    if sector_num in self.dirty_sectors[track_id]:
                        s.dam.data = bytearray(self.dirty_sectors[track_id][sector_num])
                        s.crc = s.idam.crc = s.dam.crc = 0

        master_track = track.master_track()

        # Ensure we have drive_ticks_per_rev
        if not self.drive_ticks_per_rev:
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
