import struct

from abc import ABC, abstractmethod
from types import SimpleNamespace

from floppy_formats import FLOPPY_FORMATS
from greaseweazle.codec import codec
from greaseweazle.codec.ibm import ibm
from greaseweazle.tools import read, util

# TODO: FloppyBPB also needs geometry if setting it failed from BPB as GUI relies on it

class DiskManager(ABC):
    def __init__(self):
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
        if not self.sectors_per_track or self.sectors_per_track <= 0:
            self.sectors_per_track = 18
        if not self.num_heads or self.num_heads <= 0:
            self.num_heads = 2
        if not self.num_cylinders or self.num_cylinders <= 0:
            self.num_cylinders = 80
        if not self.sector_size or self.sector_size <= 0:
            self.sector_size = 512

        if not self.total_sectors or self.total_sectors <= 0:
            self.total_sectors = self.sectors_per_track * self.num_heads * self.num_cylinders

    def read_bpb_geometry(self):
        """Temporary internal BPB decoding to set geometry."""
        try:
            boot_sector = self.read_bytes(0, 512)
            bytes_per_sector = struct.unpack_from('<H', boot_sector, 0x00B)[0]
            sectors_per_track = struct.unpack_from('<H', boot_sector, 0x018)[0]
            num_heads = struct.unpack_from('<H', boot_sector, 0x01A)[0]
            total_sectors = struct.unpack_from('<H', boot_sector, 0x013)[0]
            if total_sectors == 0:
                total_sectors = struct.unpack_from('<I', boot_sector, 0x020)[0]

            # Basic validation
            if bytes_per_sector in [128, 256, 512, 1024] and \
               sectors_per_track > 0 and num_heads > 0 and total_sectors > 0:
                self.sector_size = bytes_per_sector
                self.sectors_per_track = sectors_per_track
                self.num_heads = num_heads
                self.total_sectors = total_sectors
                self.num_cylinders = self.total_sectors // (self.sectors_per_track * self.num_heads)
                print(f"DISKMANAGER; BPB geometry: {self.sectors_per_track} sectors/track, {self.num_heads} heads, {self.num_cylinders} cylinders, {self.sector_size} bytes/sector")
                return True
            else:
                print("BPB is invalid; geometry not set from BPB")
                return False
        except Exception as e:
            print(f"Warning: Error reading BPB geometry: {e}")
            return False

    def calculate_track_and_offset(self, byte_offset):
        self.ensure_geometry()
        bytes_per_track = self.sectors_per_track * self.sector_size
        track_num = byte_offset // bytes_per_track
        cyl = track_num // self.num_heads
        head = track_num % self.num_heads
        offset_in_track = byte_offset % bytes_per_track
        return cyl, head, offset_in_track

class FloppyDiskManager(DiskManager):
    def __init__(self, device_name=None, drive='A', format_name='ibm.scan', format_params=None, tracks=None):
        super().__init__()
        self.tracks = tracks if tracks else util.TrackSet('c=0-79:h=0-1')
        self.usb = util.usb_open(device_name)
        self.drive_obj = util.Drive()(drive)
        self.format_name = format_name

        if format_params:
            self.fmt_cls = self.create_custom_diskdef(format_params)
        else:
            self.fmt_cls = codec.get_diskdef(format_name)

        self.track_data = {}  # Holds read track data
        self.dirty_sectors = {}  # Holds dirty sectors separately
        self.dirty_tracks = set()  # Tracks with dirty sectors
        self.init_geometry_from_format()

    @property
    def is_dirty(self):
        return len(self.dirty_tracks) > 0

    def init_geometry_from_format(self):
        try:
            if self.format_name == 'ibm.scan':
                self.read_and_detect_format()
                if not self.read_bpb_geometry():
                    self.detect_geometry_from_disk()
            else:
                self.init_geometry_from_fmt_cls()
                if not self.has_complete_geometry():
                    # TODO: check if this is even needed when we set geometry from hard set format
                    self.read_bpb_geometry()
        except Exception as e:
            print(f"Warning: Could not initialize geometry from format: {e}")
        self.ensure_geometry()

    def has_complete_geometry(self):
        return (self.sectors_per_track and self.num_heads and
                self.num_cylinders and self.sector_size)

    def read_and_detect_format(self):
        try:
            args = SimpleNamespace(
                revs=2, raw=False, fmt_cls=self.fmt_cls,
                tracks=util.TrackSet('c=0:h=0'),
                retries=3, seek_retries=0, reverse=False,
                adjust_speed=None, fake_index=None, hard_sectors=False,
                drive=self.drive_obj, ticks=0, drive_ticks_per_rev=None
            )

            def read_track_zero():
                for t in args.tracks:
                    _, dat = read.read_with_retry(self.usb, args, t)
                    if dat is not None:
                        track = None
                        if hasattr(dat, 'track'):
                            track = dat.track
                        else:
                            track = dat
                        if hasattr(track, 'sectors') and track.sectors:
                            sectors = track.sectors
                            if sectors:
                                self.sector_size = len(sectors[0].dam.data) if hasattr(sectors[0].dam, 'data') else 512
                                self.sectors_per_track = len(sectors)
                                self.num_heads = 2
                                self.num_cylinders = 80
                                print(f"Detected from track 0: {self.sectors_per_track} sectors, {self.sector_size} bytes per sector")
                        if hasattr(track, 'mode'):
                            if track.mode.name == 'MFM':
                                print("Detected MFM encoding")
                                if not self.sectors_per_track:
                                    if self.sector_size == 512:
                                        self.sectors_per_track = 18
                                        self.num_cylinders = 80
                                        self.num_heads = 2
                            elif track.mode.name == 'FM':
                                print("Detected FM encoding")
                                if not self.sectors_per_track:
                                    self.sectors_per_track = 9
                                    self.num_cylinders = 40
                                    self.num_heads = 2

            util.with_drive_selected(read_track_zero, self.usb, self.drive_obj)
        except Exception as e:
            print(f"Warning: Error detecting format from track 0: {e}")

    def detect_geometry_from_disk(self):
        print("Attempting to detect disk geometry from disk...")
        try:
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
                    print(f"Error reading track for head {head}: {e}")
                    pass

            if max_sectors > 0:
                self.sectors_per_track = max_sectors
                print(f"Detected {max_sectors} sectors per track")

            if not self.num_cylinders:
                max_cylinder = 40

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
        if not self.fmt_cls or not hasattr(self.fmt_cls, 'tracks'):
            return

        try:
            self.num_cylinders = self.fmt_cls.cyls
            self.num_heads = self.fmt_cls.heads

            for track_coord, track_def in self.fmt_cls.track_map.items():
                if hasattr(track_def, 'secs'):
                    self.sectors_per_track = track_def.secs
                if hasattr(track_def, 'sz') and track_def.sz:
                    n = track_def.sz[0] if isinstance(track_def.sz, list) else track_def.sz
                    self.sector_size = 128 << n
                break

            if self.sectors_per_track and self.num_heads and self.num_cylinders:
                self.total_sectors = self.sectors_per_track * self.num_heads * self.num_cylinders
        except Exception as e:
            print(f"Warning: Error initializing geometry from format: {e}")

    def create_custom_diskdef(self, params):
        disk_def = codec.DiskDef()
        disk_def.cyls = params['cyls']
        disk_def.heads = params['heads']

        track_def = ibm.IBMTrack_FixedDef(params['format_name'])

        for key, val in params['track_params'].items():
            track_def.add_param(key, str(val))

        track_def.finalise()

        for c in range(disk_def.cyls):
            for h in range(disk_def.heads):
                disk_def.track_map[(c, h)] = track_def

        disk_def.finalise()

        self.num_cylinders = params['cyls']
        self.num_heads = params['heads']
        if 'sector_size' in params:
            self.sector_size = params['sector_size']
        elif 'track_params' in params and 'bps' in params['track_params']:
            self.sector_size = params['track_params']['bps']

        if 'track_params' in params and 'secs' in params['track_params']:
            self.sectors_per_track = params['track_params']['secs']

        self.ensure_geometry()

        return disk_def

    def set_geometry(self, sectors_per_track, num_heads, num_cylinders, sector_size):
        self.sectors_per_track = sectors_per_track
        self.num_heads = num_heads
        self.num_cylinders = num_cylinders
        self.sector_size = sector_size
        self.total_sectors = sectors_per_track * num_heads * num_cylinders

    def read_track(self, cyl, head):
        track_id = (cyl, head)
        if track_id in self.track_data:
            print(f"Track {cyl}.{head} already in memory")
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
                next(track_iterator)
                flux, dat = read.read_with_retry(self.usb, args, track_iterator)

                sectors = None
                if hasattr(dat, 'track') and hasattr(dat.track, 'sectors'):
                    sectors = dat.track.sectors
                elif hasattr(dat, 'sectors'):
                    sectors = dat.sectors

                if dat is not None and sectors:
                    sector_data = {s.idam.r: bytes(s.dam.data) for s in sectors if hasattr(s, 'dam') and hasattr(s.dam, 'data')}
                    self.track_data[track_id] = sector_data

                    if not self.sectors_per_track or self.sectors_per_track < len(sectors):
                        self.sectors_per_track = len(sectors)
                        if self.num_heads and self.num_cylinders:
                            self.total_sectors = self.sectors_per_track * self.num_heads * self.num_cylinders

                    if not self.sector_size and sectors:
                        self.sector_size = len(sectors[0].dam.data) if hasattr(sectors[0], 'dam') and hasattr(sectors[0].dam, 'data') else 512
                else:
                    raise ValueError(f"Failed to read track {cyl}.{head}")

            util.with_drive_selected(read_track_wrapper, self.usb, self.drive_obj)

        return self.track_data[track_id]

    def write_track(self, cyl, head, data):
        track_id = (cyl, head)
        self.track_data[track_id] = data
        self.dirty_tracks.add(track_id)

    def flush(self, progress_callback=None):
        if not self.dirty_tracks:
            print("No dirty tracks to write.")
            if progress_callback:
                progress_callback(1.0)  # Jump to 100% if nothing to flush
            return

        if self.format_name == 'ibm.scan':
            print("Warning: Writing with ibm.scan codec may be unreliable.")

        if not hasattr(self, 'drive_ticks_per_rev'):
            def measure_rpm():
                flux = self.usb.read_track(2)
                self.drive_ticks_per_rev = flux.ticks_per_rev
                print(f"FLUSH Measured drive RPM: {60 / (self.drive_ticks_per_rev / self.usb.sample_freq):.1f}")

            try:
                util.with_drive_selected(measure_rpm, self.usb, self.drive_obj)
            except Exception as e:
                print("Failed to measure RPM:", e)
                raise

        total_tracks = len(self.dirty_tracks)
        tracks_processed = 0

        # Read phase: Only read tracks if not all sectors are dirty
        for track_id in sorted(self.dirty_tracks):
            cyl, head = track_id
            try:
                if len(self.dirty_sectors[track_id]) == self.sectors_per_track:
                    # All sectors are dirty, use dirty_sectors directly
                    full_track_data = self.dirty_sectors[track_id]
                else:
                    # Not all sectors are dirty, read the track and merge
                    print(f"FLUSH_PREREAD Reading track {cyl}.{head}")
                    try:
                        full_track_data = self.read_track(cyl, head)
                    except Exception as e:
                        print(f"FLUSH_PREREAD Error reading track {cyl}.{head}: {e}")
                        # If we can't read the track, initialize with empty sectors
                        full_track_data = {sector: bytearray(self.sector_size) for sector in range(1, self.sectors_per_track + 1)}

                    # Merge dirty sectors into the full track data
                    for sector in self.dirty_sectors[track_id]:
                        full_track_data[sector] = self.dirty_sectors[track_id][sector]

                self.track_data[track_id] = full_track_data

                if progress_callback:
                    progress = (tracks_processed / total_tracks) * 0.5  # 0% to 50% for reads
                    print(f"FLUSH_PREREAD  Read progress: {progress:.2f}")
                    progress_callback(progress)
            except Exception as e:
                print(f"FLUSH_PREREAD Error processing track {cyl}.{head}: {e}")

            tracks_processed += 1

        # Write phase: Write all dirty tracks
        tracks_written = 0
        def write_tracks():
            nonlocal tracks_written
            for track_id in sorted(self.dirty_tracks):
                cyl, head = track_id
                print(f"Writing track {cyl}.{head}")
                self.usb.seek(cyl, head)
                flux_list = self.convert_to_flux(cyl, head, self.drive_ticks_per_rev)
                self.usb.write_track(
                    flux_list=flux_list,
                    cue_at_index=True,
                    terminate_at_index=True
                )
                tracks_written += 1
                if progress_callback:
                    progress = 0.5 + (tracks_written / total_tracks) * 0.5  # 50% to 100% for writes
                    print(f"WRITE_TRACKS Write progress: {progress:.2f}")
                    progress_callback(progress)

        try:
            util.with_drive_selected(write_tracks, self.usb, self.drive_obj)
            self.dirty_tracks.clear()
            self.dirty_sectors.clear()  # Clear dirty sectors after successful flush
            print("FLUSH Flush operation completed successfully.")
        except Exception as e:
            print(f"Error writing to disk: {e}")
            raise
        finally:
            if progress_callback:
                progress_callback(1.0)  # Ensure it reaches 100%

    def convert_to_flux(self, cyl, head, drive_ticks_per_rev):
        track_def = self.fmt_cls.track_map.get((cyl, head))
        if track_def is None:
            for key, value in self.fmt_cls.track_map.items():
                track_def = value
                break
            if track_def is None:
                raise ValueError(f"No track definition found for cylinder {cyl}, head {head}")

        track = None

        if isinstance(track_def, ibm.IBMTrack_ScanDef):
            fixed_def = ibm.IBMTrack_FixedDef('ibm.mfm')
            fixed_def.secs = self.sectors_per_track or 18
            fixed_def.sz = [2]
            fixed_def.finalise()
            track = fixed_def.mk_track(cyl, head)
        else:
            track = track_def.mk_track(cyl, head)

        track_id = (cyl, head)
        if track_id not in self.track_data:
            raise ValueError(f"Track data not found for cylinder {cyl}, head {head}")

        track_data = self.track_data[track_id]

        for s in track.sectors:
            if hasattr(s, 'idam') and hasattr(s.idam, 'r') and s.idam.r in track_data:
                s.dam.data = bytearray(track_data[s.idam.r])
                s.crc = s.idam.crc = s.dam.crc = 0

        master_track = track.master_track()
        master_track.time_per_rev = drive_ticks_per_rev / self.usb.sample_freq
        wflux = master_track.flux_for_writeout(cue_at_index=True)

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
        self.ensure_geometry()

        bytes_per_track = self.sectors_per_track * self.sector_size
        start_track_num = offset // bytes_per_track
        end_track_num = (offset + length - 1) // bytes_per_track
        tracks_to_read = set()
        for track_num in range(start_track_num, end_track_num + 1):
            cyl = track_num // self.num_heads
            head = track_num % self.num_heads
            tracks_to_read.add((cyl, head))

        # Load tracks not in memory
        tracks_to_load = [track_id for track_id in tracks_to_read if track_id not in self.track_data]
        total_tracks_to_load = len(tracks_to_load)
        tracks_loaded = 0

        for track_id in tracks_to_load:
            try:
                self.read_track(*track_id)
                tracks_loaded += 1
                if progress_callback and total_tracks_to_load > 0:
                    progress_callback(tracks_loaded / total_tracks_to_load)
            except Exception as e:
                print(f"Error reading track {track_id}: {e}")

        # Assemble data, preferring dirty_sectors over track_data
        data = b''
        current_offset = offset
        remaining_length = length
        while remaining_length > 0:
            track_num = current_offset // bytes_per_track
            cyl = track_num // self.num_heads
            head = track_num % self.num_heads
            offset_in_track = current_offset % bytes_per_track
            sector_num = offset_in_track // self.sector_size + 1
            offset_in_sector = offset_in_track % self.sector_size

            track_id = (cyl, head)
            if track_id in self.dirty_sectors and sector_num in self.dirty_sectors[track_id]:
                sector_data = self.dirty_sectors[track_id][sector_num]
            elif track_id in self.track_data and sector_num in self.track_data[track_id]:
                sector_data = self.track_data[track_id][sector_num]
            else:
                sector_data = b'\x00' * self.sector_size

            chunk_size = min(remaining_length, self.sector_size - offset_in_sector)
            data += sector_data[offset_in_sector:offset_in_sector + chunk_size]
            remaining_length -= chunk_size
            current_offset += chunk_size

        return data

    def write_bytes(self, offset, data, progress_callback=None):
        self.ensure_geometry()

        bytes_per_track = self.sectors_per_track * self.sector_size
        track_num = offset // bytes_per_track
        cyl = track_num // self.num_heads
        head = track_num % self.num_heads
        offset_in_track = offset % bytes_per_track
        sector_num = offset_in_track // self.sector_size + 1
        offset_in_sector = offset_in_track % self.sector_size

        print(f"Writing {len(data)} bytes to track {cyl}.{head}, sector {sector_num}, offset {offset_in_sector}")

        data = bytearray(data)  # Ensure input is a bytearray
        while data:
            track_id = (cyl, head)
            # # Ensure track data is read from disk if not already in memory
            # if track_id not in self.track_data:
            #     try:
            #         self.read_track(cyl, head)
            #     except Exception as e:
            #         print(f"Error reading track {track_id} during write_bytes: {e}")
            #         # Initialize empty track data if we can't read it
            #         self.track_data[track_id] = {}
            if track_id not in self.dirty_sectors:
                self.dirty_sectors[track_id] = {}

            if sector_num not in self.dirty_sectors[track_id]:
                # Copy the current sector data from track_data if it exists, otherwise create empty sector
                if track_id in self.track_data and sector_num in self.track_data[track_id]:
                    self.dirty_sectors[track_id][sector_num] = bytearray(self.track_data[track_id][sector_num])
                else:
                    self.dirty_sectors[track_id][sector_num] = bytearray(self.sector_size)

            sector_data = self.dirty_sectors[track_id][sector_num]
            write_length = min(len(data), self.sector_size - offset_in_sector)
            sector_data[offset_in_sector:offset_in_sector + write_length] = data[:write_length]
            self.dirty_sectors[track_id][sector_num] = sector_data
            self.dirty_tracks.add(track_id)

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
        super().__init__()
        self.file_path = file_path
        if image_data is not None:
            self.image_data = bytearray(image_data)
            self.dirty = True
        else:
            with open(file_path, 'rb') as f:
                self.image_data = bytearray(f.read())
            self.dirty = False

        self._infer_geometry()

    @property
    def is_dirty(self):
        return self.dirty

    def _infer_geometry(self):
        if self.read_bpb_geometry():
            return

        # TODO: do we want to rely on the image size to infer geometry?
        # Check against known floppy formats by media descriptor and other data?
        image_size = len(self.image_data)
        for fmt in FLOPPY_FORMATS:
            expected_size = fmt['total_sectors'] * fmt['sector_size']
            if image_size == expected_size:
                self.sectors_per_track = fmt['sectors']
                self.num_heads = fmt['heads']
                self.num_cylinders = fmt['tracks']
                self.sector_size = fmt['sector_size']
                self.total_sectors = fmt['total_sectors']
                print(f"IMAGEDISKMANAGER; Inferred geometry from FLOPPY_FORMATS: {self.sectors_per_track} sectors/track, {self.num_heads} heads, {self.num_cylinders} cylinders, {self.sector_size} bytes/sector")
                return

        # TODO: should set for some old safe format prior to DOS 2.0 BPB introduction?
        # And try to get media descriptor from FAT12 first table?
        print("IMAGEDISKMANAGER; Warning: No matching format in FLOPPY_FORMATS; using default geometry")
        self.sectors_per_track = 18
        self.num_heads = 2
        self.sector_size = 512
        self.num_cylinders = image_size // (self.sectors_per_track * self.num_heads * self.sector_size)
        self.total_sectors = self.sectors_per_track * self.num_heads * self.num_cylinders

    def read_bytes(self, offset, length, progress_callback=None):
        data = self.image_data[offset:offset + length]
        if progress_callback:
            progress_callback(1.0)
        return data

    def write_bytes(self, offset, data, progress_callback=None):
        self.image_data[offset:offset + len(data)] = data
        self.dirty = True
        if progress_callback:
            progress_callback(1.0)

    def flush(self, progress_callback=None):
        if self.dirty:
            total_bytes = len(self.image_data)
            chunk_size = self.sector_size
            with open(self.file_path, 'wb') as f:
                for i in range(0, total_bytes, chunk_size):
                    chunk = self.image_data[i:i + chunk_size]
                    f.write(chunk)
                    if progress_callback:
                        progress_callback(min(i + len(chunk), total_bytes) / total_bytes)
            self.dirty = False


class MemoryDiskManager(DiskManager):
    def __init__(self, image_data):
        super().__init__()
        self.image_data = image_data

    @property
    def is_dirty(self):
        return self.dirty

    def read_bytes(self, offset, length):
        return self.image_data[offset:offset + length]

    def write_bytes(self, offset, data):
        self.image_data[offset:offset + len(data)] = data

    def flush(self):
        pass
