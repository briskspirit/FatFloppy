# tests/software/test_05_controller_physical_mocked_pytest.py
import pytest
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock, ANY, PropertyMock

sys.path.insert(0, str(Path(__file__).parent.parent.parent / 'src'))

from fatfloppy.core.controller import DiskController
from fatfloppy.core.drivers import GreaseweazleDriver
from fatfloppy.core.filesystem import FATFilesystem, FATBootSector
from fatfloppy.core.disk import DiskGeometry
from fatfloppy.core.formats import BootSectorData
from fatfloppy.core.format_definitions import FLOPPY_FORMATS

try:
    from greaseweazle import usb as real_gw_usb
    from greaseweazle.flux import Flux as RealFlux, WriteoutFlux as RealWriteoutFlux
    from greaseweazle.codec.ibm import IBMTrack_FixedDef as RealIBMTrack_FixedDef, IBMTrack as RealIBMTrack, DiskDef as RealDiskDef
    from greaseweazle.track import MasterTrack as RealMasterTrack, RawTrack as RealRawTrack
    REAL_GW_AVAILABLE = True
except ImportError:
    print("Warning: Real Greaseweazle library not found. Mocking based on provided source.")
    REAL_GW_AVAILABLE = False
    class DummyCmdError(Exception): pass
    class DummyAck: Okay=0; BadCommand=1; NoIndex=2; NoTrk0=3; Wrprot=4
    class DummyCmd: GetInfo=0; Seek=2; Head=3; ReadFlux=7; WriteFlux=8
    real_gw_usb = MagicMock(); real_gw_usb.CmdError = DummyCmdError
    real_gw_usb.Ack = DummyAck; real_gw_usb.Cmd = DummyCmd; real_gw_usb.Unit = MagicMock
    RealFlux = MagicMock; RealWriteoutFlux = MagicMock
    RealIBMTrack_FixedDef = MagicMock; RealIBMTrack = MagicMock; RealDiskDef = MagicMock
    RealMasterTrack = MagicMock; RealRawTrack = MagicMock

FMT_144 = FLOPPY_FORMATS['ibm_3.5_1.44m']
FMT_360 = FLOPPY_FORMATS['ibm_5.25_360k']
FMT_720 = FLOPPY_FORMATS['ibm_3.5_720k']

def create_mock_flux(sample_freq=16000000.0, ticks_per_rev=3200000.0, revs=2):
    mock_flux = MagicMock(spec=RealFlux if REAL_GW_AVAILABLE else None)
    mock_flux.sample_freq = float(sample_freq) if sample_freq else 16000000.0
    mock_flux.index_list = [float(ticks_per_rev)] * revs
    avg_interval = 4e-6 * mock_flux.sample_freq # ~64 ticks at 16MHz for MFM 500kbps
    num_fluxes = int((float(ticks_per_rev) * revs) / avg_interval) if avg_interval > 0 else 0
    # Ensure list is not empty
    mock_flux.list = [float(avg_interval + (-1)**i * avg_interval * 0.1) for i in range(num_fluxes)] or [100.0]
    current_sum = sum(mock_flux.list)
    target_sum = float(ticks_per_rev) * revs
    if current_sum > 0 and target_sum > 0:
         scale = target_sum / current_sum
         mock_flux.list = [x * scale for x in mock_flux.list]
    elif target_sum > 0:
         mock_flux.list = [float(target_sum / (num_fluxes or 1))] * (num_fluxes or 1)

    type(mock_flux).ticks_per_rev = PropertyMock(return_value=float(ticks_per_rev))
    type(mock_flux).time_per_rev = PropertyMock(return_value=float(ticks_per_rev / mock_flux.sample_freq if mock_flux.sample_freq else 0.2))
    mock_flux.summary_string.return_value = f"Mock Flux ({len(mock_flux.list)} samples)"
    mock_flux.flux.return_value = mock_flux
    return mock_flux

def create_mock_track_data(cyl, head, fmt_def, provide_boot_sector=False):
    mock_dat = MagicMock(spec=RealRawTrack if REAL_GW_AVAILABLE else None)
    mock_dat.track = MagicMock(spec=RealIBMTrack if REAL_GW_AVAILABLE else None)
    num_sectors = fmt_def.geometry.sectors_per_track
    sector_size = fmt_def.geometry.sector_size
    mock_sectors = []
    for i in range(num_sectors):
        sec_nr = i + 1
        sec = MagicMock()
        if sector_size == 128: sector_n_value = 0
        elif sector_size == 256: sector_n_value = 1
        elif sector_size == 512: sector_n_value = 2
        elif sector_size == 1024: sector_n_value = 3
        else: sector_n_value = 2
        sec.idam = MagicMock(c=cyl, h=head, r=sec_nr, n=sector_n_value, crc=0)
        sec.crc = 0
        sec.dam = MagicMock(data=b'', crc=0)
        if provide_boot_sector and cyl == 0 and head == 0 and sec_nr == 1:
             bsd = fmt_def.boot_sector
             if not bsd:
                 bsd = BootSectorData(
                     sectors_per_track=fmt_def.geometry.sectors_per_track, num_heads=fmt_def.geometry.heads,
                     total_sectors=fmt_def.geometry.total_sectors, media_descriptor=fmt_def.media_descriptor,
                     bytes_per_sector=fmt_def.geometry.sector_size, sectors_per_cluster=1, root_entries=224, sectors_per_fat=9)
             sec.dam.data = bytearray(bsd.to_bytes())
        else:
             byte_pattern = bytes([cyl % 256, head % 256, sec_nr % 256])
             sec.dam.data = bytearray((byte_pattern * (sector_size // 3 + 1))[:sector_size])
        mock_sectors.append(sec)
    mock_dat.track.sectors = mock_sectors
    mock_dat.sectors = mock_sectors # Direct access
    mock_dat.nr_missing = MagicMock(return_value=0)
    type(mock_dat.track).mode = PropertyMock(return_value="IBM MFM")
    type(mock_dat.track).clock = PropertyMock(return_value=2e-6)
    return mock_dat

def create_mock_ibm_track_def_instance():
    mock_track_def = MagicMock(spec=RealIBMTrack_FixedDef if REAL_GW_AVAILABLE else None)
    mock_track_def._params = {}
    def mock_add_param(key, value): mock_track_def._params[key] = value
    mock_track_def.add_param = mock_add_param
    mock_track_def.finalise = MagicMock()
    def mock_mk_track(cyl, head):
        track = MagicMock(spec=RealIBMTrack if REAL_GW_AVAILABLE else None)
        track.sectors = []
        mock_master = MagicMock(spec=RealMasterTrack if REAL_GW_AVAILABLE else None)
        mock_master.time_per_rev = 0.2
        mock_writeout = MagicMock(spec=RealWriteoutFlux if REAL_GW_AVAILABLE else None)
        mock_writeout.list = [100.0, 200.0, 150.0]
        mock_writeout.ticks_to_index = sum(mock_writeout.list) / 2 if mock_writeout.list else 32000000.0 * 0.2
        mock_master.flux_for_writeout.return_value = mock_writeout
        track.master_track.return_value = mock_master
        return track
    mock_track_def.mk_track = mock_mk_track
    return mock_track_def

def create_mock_disk_def_instance():
    mock_disk_def = MagicMock(spec=RealDiskDef if REAL_GW_AVAILABLE else None)
    mock_disk_def.track_map = {}
    mock_disk_def.cyls = 80
    mock_disk_def.heads = 2
    mock_disk_def.finalise = MagicMock()
    return mock_disk_def

@pytest.fixture(scope="function")
def mocked_controller(request):
    patches = []
    mocks = {}
    patch_targets = [
        ('fatfloppy.core.drivers.util', 'mock_util'),
        ('fatfloppy.core.drivers.codec', 'mock_codec'),
        ('fatfloppy.core.drivers.read', 'mock_read'),
        ('greaseweazle.codec.codec.DiskDef', 'MockDiskDef'),
        ('greaseweazle.codec.ibm.IBMTrack_FixedDef', 'MockIBMTrack_FixedDef'),
        ('fatfloppy.core.drivers.util.TrackSet', 'MockTrackSet'),
    ]

    try:
        for target, name in patch_targets:
            patcher = patch(target, create=True)
            patches.append(patcher)
            mocks[name] = patcher.start()

        mock_usb = MagicMock(spec=real_gw_usb.Unit if REAL_GW_AVAILABLE else None)
        mock_usb.sample_freq = 16000000.0
        mock_usb.hw_model = "MockGW"; mock_usb.hw_major = 0; mock_usb.hw_minor = 0
        mock_usb.max_cmd_len = 64
        mock_usb.seek.return_value = None; mock_usb.set_bus_type.return_value = None
        mock_usb.drive_select.return_value = None; mock_usb.drive_deselect.return_value = None
        mock_usb.drive_motor.return_value = None; mock_usb.get_pin.return_value = False
        mock_usb.set_pin.return_value = None; mock_usb.write_track.return_value = None
        mock_usb.read_track.return_value = create_mock_flux(sample_freq=mock_usb.sample_freq, ticks_per_rev=3200000.0)
        mocks['mock_util'].usb_open.return_value = mock_usb
        mock_drive_instance = MagicMock()
        mock_drive_instance.unit_id = 0; mock_drive_instance.bus = MagicMock(value='ibm-pc')
        mocks['mock_util'].Drive.return_value = MagicMock(return_value=mock_drive_instance)
        mocks['mock_util'].with_drive_selected.side_effect = lambda func, *args, **kwargs: func()

        def mock_trackset_init(track_str):
            mock_instance = MagicMock()
            mock_instance.track_str = track_str
            mock_instance.__str__ = lambda self_ignored: mock_instance.track_str
            def track_iter(): yield (int(track_str.split(':')[0][2:]), int(track_str.split(':')[1][2:]))
            mock_instance.__iter__ = track_iter
            class MockTrackIter:
                 def __init__(self, ts): self.ts = ts; self.iter = iter(self.ts)
                 def __next__(self): return next(self.iter)
                 def __iter__(self): return self
            type(mock_instance).TrackIter = MockTrackIter
            return mock_instance
        mocks['MockTrackSet'].side_effect = mock_trackset_init
        mocks['mock_codec'].get_diskdef.return_value = create_mock_disk_def_instance()
        mocks['MockDiskDef'].side_effect = create_mock_disk_def_instance
        mocks['MockIBMTrack_FixedDef'].side_effect = create_mock_ibm_track_def_instance
        mocks['mock_read'].read_with_retry.return_value = (
            create_mock_flux(sample_freq=mock_usb.sample_freq),
            create_mock_track_data(0, 0, FMT_144, provide_boot_sector=True)
        )
        mocks_bundle = {
            'mocks': mocks,
            'mock_usb': mock_usb,
            'mock_drive_instance': mock_drive_instance
        }
        controller = DiskController()
        yield controller, mocks_bundle

    finally:
        print(f"--- [Fixture Teardown] Stopping GW mocks for test: {request.node.name} ---")
        while patches:
            patcher = patches.pop()
            try: patcher.stop()
            except RuntimeError as e:
                 if "never started" not in str(e) and "already stopped" not in str(e):
                    print(f"Warning: Error stopping patch {patcher}: {e}")
        if 'controller' in locals() and locals()['controller'].disk:
            try: locals()['controller'].close_disk()
            except Exception as e: print(f"Ignoring error closing controller in teardown: {e}")

def test_01_open_physical_drive_A_35_auto_detect_mocked(mocked_controller):
    controller, mocks_bundle = mocked_controller
    mocks = mocks_bundle['mocks']
    mock_usb = mocks_bundle['mock_usb']
    mock_util = mocks['mock_util']
    mock_read = mocks['mock_read']

    drive_letter = 'A'
    drive_size = "3.5"
    expected_format = FMT_144

    mock_usb.read_track.return_value = create_mock_flux(ticks_per_rev=3200000.0, sample_freq=mock_usb.sample_freq)
    def mock_read_retry_auto_detect(usb, args, track_iter):
        track_str = str(args.tracks); c,h = -1,-1
        try: parts=track_str.split(':'); c=int(parts[0][2:]); h=int(parts[1][2:])
        except: pass
        if c == 0 and h == 0:
            return (create_mock_flux(sample_freq=mock_usb.sample_freq), create_mock_track_data(0, 0, expected_format, provide_boot_sector=True))
        else:
             return (create_mock_flux(sample_freq=mock_usb.sample_freq), create_mock_track_data(c, h, expected_format))
    mock_read.read_with_retry.side_effect = mock_read_retry_auto_detect
    mock_fs = MagicMock(spec=FATFilesystem); mock_fs.is_valid.return_value = True; mock_fs.fat_type="FAT12"
    bsd = expected_format.boot_sector; mock_fat_bs = MagicMock(spec=FATBootSector)
    for attr, value in bsd.__dict__.items(): setattr(mock_fat_bs, attr, value)
    mock_fat_bs.is_valid.return_value = True; mock_fat_bs.calculate_fat_type.return_value = "FAT12"
    mock_fs.boot_sector = mock_fat_bs

    with patch.object(DiskController, 'detect_filesystem', return_value="FAT12") as mock_detect_fs:
        def side_effect_assign_fs(*args, **kwargs):
            controller.filesystem = mock_fs
            if controller.disk and controller.disk.geometry:
                if controller.disk.geometry.sectors_per_track != mock_fat_bs.sectors_per_track or \
                   controller.disk.geometry.heads != mock_fat_bs.num_heads:
                    updated_geom = DiskGeometry(cylinders=controller.disk.geometry.cylinders, heads=mock_fat_bs.num_heads, sectors_per_track=mock_fat_bs.sectors_per_track, sector_size=controller.disk.geometry.sector_size)
                    controller.set_geometry(updated_geom)
            return "FAT12"
        mock_detect_fs.side_effect = side_effect_assign_fs
        success = controller.open_disk(source=None, disk_type="physical", drive_letter=drive_letter, drive_size=drive_size)
        assert success is True, "controller.open_disk should return True"
        assert isinstance(controller.driver, GreaseweazleDriver), "Driver type mismatch"
        mock_util.usb_open.assert_called_with(None)
        mock_util.Drive.assert_called()
        assert mock_usb.read_track.called, "mock_usb.read_track should be called for RPM"
        mock_detect_fs.assert_called(), "detect_filesystem should be called"
        assert controller.driver.initialized is True, "Driver should be initialized"
        assert controller.disk is not None, "Disk object should be created"
        assert controller.disk.geometry is not None, "Disk geometry should be set"
        assert controller.filesystem is not None, "Filesystem object should be created"
        geom = controller.disk.geometry
        assert geom.sectors_per_track == expected_format.geometry.sectors_per_track
        assert geom.heads == expected_format.geometry.heads
        pf = controller.driver.physical_format
        assert pf is not None, "Physical format should be set"
        assert pf.sectors_per_track == expected_format.geometry.sectors_per_track
        assert pf.heads == expected_format.geometry.heads

def test_02_open_physical_with_explicit_format_mocked(mocked_controller):
    controller, mocks_bundle = mocked_controller
    mocks = mocks_bundle['mocks']
    mock_usb = mocks_bundle['mock_usb']
    mock_util = mocks['mock_util']
    mock_drive_instance = mocks_bundle['mock_drive_instance']
    drive_letter = 'B'; drive_size = "5.25"; expected_format = FMT_360
    source_device = "COM3";
    format_info = {
        "cylinders": expected_format.geometry.cylinders, "heads": expected_format.geometry.heads,
        "sectors_per_track": expected_format.geometry.sectors_per_track, "sector_size": expected_format.geometry.sector_size,
        "encoding": expected_format.physical_format.encoding, "rate": expected_format.physical_format.rate,
        "rpm": expected_format.physical_format.rpm, "gap3": expected_format.physical_format.gap3,
        "cskew": expected_format.physical_format.cskew, "interleave": expected_format.physical_format.interleave
    }
    mock_drive_instance.unit_id = 1
    mock_util.Drive.return_value = MagicMock(return_value=mock_drive_instance)
    mock_fs = MagicMock(spec=FATFilesystem); mock_fs.is_valid.return_value = True; mock_fs.fat_type="FAT12"
    bsd = expected_format.boot_sector or BootSectorData()
    mock_fat_bs = MagicMock(spec=FATBootSector)
    for attr, value in bsd.__dict__.items(): setattr(mock_fat_bs, attr, value)
    mock_fat_bs.is_valid.return_value = True; mock_fat_bs.calculate_fat_type.return_value = "FAT12"
    mock_fat_bs.sectors_per_track=expected_format.geometry.sectors_per_track
    mock_fat_bs.num_heads=expected_format.geometry.heads
    mock_fs.boot_sector = mock_fat_bs

    with patch.object(DiskController, 'detect_filesystem', return_value="FAT12") as mock_detect_fs:
         def set_fs_side_effect(*args, **kwargs):
             if controller.disk: controller.filesystem = mock_fs
             return "FAT12"
         mock_detect_fs.side_effect = set_fs_side_effect
         mock_usb.read_track.return_value = create_mock_flux(ticks_per_rev=3333333.0, sample_freq=mock_usb.sample_freq)
         success = controller.open_disk(
             source=source_device, disk_type="physical",
             drive_letter=drive_letter, drive_size=drive_size,
             format_info=format_info
         )
         assert success is True, "controller.open_disk should return True for explicit format"
         assert isinstance(controller.driver, GreaseweazleDriver)
         mock_util.Drive.assert_called()
         drive_call_args = mock_util.Drive().call_args
         assert drive_call_args[0][0] == drive_letter
         mock_util.usb_open.assert_called_once_with(source_device)
         assert controller.driver.initialized is True, "Driver should be initialized after open"
         mock_detect_fs.assert_called_once()
         assert controller.filesystem is not None
         assert controller.disk is not None, "Disk object missing"
         assert controller.disk.geometry is not None, "Geometry missing"
         assert controller.disk.geometry.cylinders == expected_format.geometry.cylinders
         assert controller.disk.geometry.sectors_per_track == expected_format.geometry.sectors_per_track
         assert controller.driver.physical_format is not None, "Physical format missing"
         assert controller.driver.physical_format.rate == expected_format.physical_format.rate
         assert controller.driver.using_custom_diskdef is True, "Custom diskdef should be set"

def open_disk_for_rw_tests(controller, mocks_bundle, test_format=FMT_144):
    mocks = mocks_bundle['mocks']
    mock_usb = mocks_bundle['mock_usb']
    mock_util = mocks['mock_util']
    format_info_dict = {
        "cylinders": test_format.geometry.cylinders, "heads": test_format.geometry.heads,
        "sectors_per_track": test_format.geometry.sectors_per_track, "sector_size": test_format.geometry.sector_size,
        "encoding": test_format.physical_format.encoding, "rate": test_format.physical_format.rate,
        "rpm": test_format.physical_format.rpm, "gap3": test_format.physical_format.gap3,
        "cskew": test_format.physical_format.cskew, "interleave": test_format.physical_format.interleave
    }
    mock_fs = MagicMock(spec=FATFilesystem); mock_fs.is_valid.return_value = True; mock_fs.fat_type="FAT12"
    bsd = test_format.boot_sector or BootSectorData()
    mock_fat_bs = MagicMock(spec=FATBootSector)
    for attr, value in bsd.__dict__.items(): setattr(mock_fat_bs, attr, value)
    mock_fat_bs.is_valid.return_value = True; mock_fat_bs.calculate_fat_type.return_value = "FAT12"
    mock_fat_bs.sectors_per_track=test_format.geometry.sectors_per_track
    mock_fat_bs.num_heads=test_format.geometry.heads
    mock_fat_bs.total_sectors=test_format.geometry.total_sectors
    mock_fs.boot_sector = mock_fat_bs

    with patch.object(DiskController, 'detect_filesystem', return_value="FAT12") as mock_detect_fs:
        def side_effect_assign_fs(*args, **kwargs):
            if controller.disk: controller.filesystem = mock_fs
            return "FAT12"
        mock_detect_fs.side_effect = side_effect_assign_fs
        mock_usb.read_track.return_value = create_mock_flux(sample_freq=mock_usb.sample_freq, ticks_per_rev=3200000.0)
        mock_util.with_drive_selected.side_effect = lambda func, usb, drive, motor=ANY: func()
        success = controller.open_disk(None, "physical", 'A', "3.5", format_info=format_info_dict)
        assert success is True, f"open_disk failed in helper for format {test_format.name}"
        assert controller.disk is not None, "Disk object not created in helper"
        assert controller.driver is not None, "Driver object not created in helper"
        assert controller.driver.initialized is True, "Driver not initialized in helper"
        assert controller.driver.using_custom_diskdef is True, "Custom diskdef not set in helper"

def test_03_physical_read_sector_success_mocked(mocked_controller):
    controller, mocks_bundle = mocked_controller
    mocks = mocks_bundle['mocks']
    mock_usb = mocks_bundle['mock_usb']
    mock_read = mocks['mock_read']
    cyl, head, sect = 10, 1, 5
    test_format = FMT_144
    open_disk_for_rw_tests(controller, mocks_bundle, test_format)
    def mock_read_retry_func(usb, args, track_iter):
        c,h = -1,-1; track_str = str(args.tracks)
        try: parts=track_str.split(':'); c=int(parts[0][2:]); h=int(parts[1][2:])
        except: print(f"WARN: Could not parse C/H from {track_str}")
        if c == cyl and h == head: return (create_mock_flux(sample_freq=mock_usb.sample_freq), create_mock_track_data(c, h, test_format))
        else: return (create_mock_flux(sample_freq=mock_usb.sample_freq), create_mock_track_data(c, h, test_format))
    mock_read.read_with_retry.side_effect = mock_read_retry_func # Configure on mock_read
    read_data = controller.disk.read_sector(cyl, head, sect)
    mock_read.read_with_retry.assert_called() # Check mock_read
    last_call_args = mock_read.read_with_retry.call_args[0]
    assert f'c={cyl}:h={head}' in str(last_call_args[1].tracks)
    assert len(read_data) == test_format.geometry.sector_size
    expected_byte_values = bytes([cyl % 256, head % 256, sect % 256])
    assert read_data.startswith(expected_byte_values)
    assert (cyl, head, sect) in controller.driver.sector_cache
    mock_read.read_with_retry.reset_mock()
    read_data_cached = controller.disk.read_sector(cyl, head, sect)
    mock_read.read_with_retry.assert_not_called()
    assert read_data_cached == read_data

def test_04_physical_read_sector_not_found_mocked(mocked_controller):
    controller, mocks_bundle = mocked_controller
    mocks = mocks_bundle['mocks']
    mock_usb = mocks_bundle['mock_usb']
    mock_read = mocks['mock_read']
    cyl, head, sect = 11, 0, 15
    expected_data = b'\x00' * 512
    test_format = FMT_144
    open_disk_for_rw_tests(controller, mocks_bundle, test_format)
    assert controller.disk.geometry.sectors_per_track >= sect, "Test invalid: sector out of range"
    def mock_read_retry_func(usb, args, track_iter):
         c,h = -1,-1; track_str = str(args.tracks)
         try: parts=track_str.split(':'); c=int(parts[0][2:]); h=int(parts[1][2:])
         except: pass
         if c == cyl and h == head:
             mock_track = create_mock_track_data(c, h, test_format)
             if hasattr(mock_track, 'track') and hasattr(mock_track.track, 'sectors'):
                 mock_track.track.sectors = [s for s in mock_track.track.sectors if not (hasattr(s, 'idam') and s.idam.r == sect)]
             if hasattr(mock_track, 'sectors'):
                 mock_track.sectors = [s for s in mock_track.sectors if not (hasattr(s, 'idam') and s.idam.r == sect)]
             return (create_mock_flux(sample_freq=mock_usb.sample_freq), mock_track)
         else: return (create_mock_flux(sample_freq=mock_usb.sample_freq), create_mock_track_data(c, h, test_format))
    mock_read.read_with_retry.side_effect = mock_read_retry_func
    read_data = controller.disk.read_sector(cyl, head, sect)
    mock_read.read_with_retry.assert_called()
    assert read_data == expected_data
    assert (cyl, head, sect) not in controller.driver.sector_cache
    assert (cyl, head) in controller.driver.track_data
    assert sect not in controller.driver.track_data[(cyl, head)]

def test_06_physical_write_sector_and_flush_success_mocked(mocked_controller):
    controller, mocks_bundle = mocked_controller
    mocks = mocks_bundle['mocks']
    mock_usb = mocks_bundle['mock_usb']
    mock_read = mocks['mock_read']
    mock_util = mocks['mock_util']
    cyl, head, sect = 20, 0, 9
    write_data = b'\xDB' * 512
    test_format = FMT_144
    open_disk_for_rw_tests(controller, mocks_bundle, test_format)
    controller.driver.verify_writes = False
    with patch.object(GreaseweazleDriver, '_convert_to_flux', autospec=True) as mock_convert:
        mock_convert.return_value = [100.0, 200.0, 150.0]
        mock_read.read_with_retry.reset_mock()
        mock_read.read_with_retry.return_value = (create_mock_flux(sample_freq=mock_usb.sample_freq), create_mock_track_data(cyl, head, test_format))
        controller.disk.write_sector(cyl, head, sect, write_data)
        controller.driver.flush()
        mock_convert.assert_called_once_with(controller.driver, cyl, head)
        mock_util.with_drive_selected.assert_called()
        mock_usb.seek.assert_called_with(cyl, head)
        mock_usb.write_track.assert_called_once_with(flux_list=mock_convert.return_value, cue_at_index=True, terminate_at_index=True)
        assert (cyl, head) not in controller.driver.dirty_sectors
        assert len(controller.driver.dirty_tracks) == 0
        read_data = controller.disk.read_sector(cyl, head, sect)
        assert read_data == write_data
        mock_read.read_with_retry.assert_called_once()  # Called once during flush

def test_07_physical_flush_write_error_mocked(mocked_controller):
    controller, mocks_bundle = mocked_controller
    mocks = mocks_bundle['mocks']
    mock_usb = mocks_bundle['mock_usb']
    mock_read = mocks['mock_read']
    mock_util = mocks['mock_util']
    cyl, head, sect = 21, 1, 2
    write_data = b'\xEE' * 512
    test_format = FMT_144
    open_disk_for_rw_tests(controller, mocks_bundle, test_format)
    controller.driver.verify_writes = False
    with patch.object(GreaseweazleDriver, '_convert_to_flux', autospec=True) as mock_convert:
        mock_convert.return_value = [111.0, 222.0, 111.0]
        error_to_raise = real_gw_usb.CmdError(cmd=b'', code=real_gw_usb.Ack.Wrprot) if REAL_GW_AVAILABLE else RuntimeError("Mock Write Error")
        mock_usb.write_track.side_effect = error_to_raise
        mock_read.read_with_retry.reset_mock()
        mock_read.read_with_retry.return_value = (create_mock_flux(sample_freq=mock_usb.sample_freq), create_mock_track_data(cyl, head, test_format))
        mock_util.with_drive_selected.side_effect = lambda func, usb, drive, motor=ANY: func()
        controller.disk.write_sector(cyl, head, sect, write_data)
        controller.driver.flush()
        mock_convert.assert_called_once_with(controller.driver, cyl, head)
        mock_util.with_drive_selected.assert_called()
        mock_usb.seek.assert_called_with(cyl, head)
        mock_usb.write_track.assert_called_once()
        assert (cyl, head) in controller.driver.dirty_sectors, "Track should remain dirty"
        assert (cyl, head) in controller.driver.dirty_tracks, "Track should remain dirty"

def test_09_physical_flush_partial_track_reads_first_mocked(mocked_controller):
    controller, mocks_bundle = mocked_controller
    mocks = mocks_bundle['mocks']
    mock_usb = mocks_bundle['mock_usb']
    mock_read = mocks['mock_read']
    mock_util = mocks['mock_util']
    cyl, head, sect = 25, 1, 4
    write_data = b'\xCC' * 512
    test_format = FMT_144
    open_disk_for_rw_tests(controller, mocks_bundle, test_format)
    controller.driver.verify_writes = False
    assert controller.driver.physical_format.sectors_per_track > 1
    with patch.object(GreaseweazleDriver, '_convert_to_flux', autospec=True) as mock_convert:
        mock_convert.return_value = [300.0, 100.0, 250.0]
        mock_read.read_with_retry.side_effect = MagicMock(return_value=(create_mock_flux(sample_freq=mock_usb.sample_freq), create_mock_track_data(cyl, head, test_format)))
        mock_util.with_drive_selected.side_effect = lambda func, usb, drive, motor=ANY: func()
        controller.disk.write_sector(cyl, head, sect, write_data)
        controller.driver.flush()
        mock_read.read_with_retry.assert_called_once()
        call_args = mock_read.read_with_retry.call_args[0]
        assert f'c={cyl}:h={head}' in str(call_args[1].tracks)
        mock_convert.assert_called_once_with(controller.driver, cyl, head)
        mock_util.with_drive_selected.assert_called()
        mock_usb.seek.assert_called_once_with(cyl, head)
        mock_usb.write_track.assert_called_once_with(flux_list=mock_convert.return_value, cue_at_index=True, terminate_at_index=True)
        assert (cyl, head) not in controller.driver.dirty_sectors
        assert len(controller.driver.dirty_tracks) == 0

def test_10_gw_cache_invalidation_mocked(mocked_controller):
    controller, mocks_bundle = mocked_controller
    mocks = mocks_bundle['mocks']
    mock_usb = mocks_bundle['mock_usb']
    mock_read = mocks['mock_read']
    mock_util = mocks['mock_util']
    cyl, head, sect = 22, 0, 1
    write_data = b'\xAA' * 512
    test_format = FMT_144
    open_disk_for_rw_tests(controller, mocks_bundle, test_format)
    controller.disk.read_sector(cyl, head, sect)
    assert (cyl, head) in controller.driver.track_data, "Cache not populated"
    assert mock_read.read_with_retry.call_count == 1  # First call for initial read
    controller.disk.write_sector(cyl, head, sect, write_data)
    controller.driver.verify_writes = False
    with patch.object(GreaseweazleDriver, '_convert_to_flux', autospec=True) as mock_convert:
        mock_convert.return_value = [1.0]
        mock_read.read_with_retry.return_value = (create_mock_flux(sample_freq=mock_usb.sample_freq), create_mock_track_data(cyl, head, test_format))
        controller.driver.flush()
        assert mock_read.read_with_retry.call_count == 2  # Second call during flush
    read_data = controller.disk.read_sector(cyl, head, sect)
    assert read_data == write_data, "Cache should contain the written data"
    assert mock_read.read_with_retry.call_count == 2  # No additional calls

def test_11_gw_write_verify_success_mocked(mocked_controller):
    controller, mocks_bundle = mocked_controller
    mocks = mocks_bundle['mocks']
    mock_usb = mocks_bundle['mock_usb']
    mock_read = mocks['mock_read']
    mock_util = mocks['mock_util']
    cyl, head, sect = 23, 1, 7
    write_data = b'\xBB' * 512
    test_format = FMT_144
    open_disk_for_rw_tests(controller, mocks_bundle, test_format)
    controller.driver.verify_writes = True
    with patch.object(GreaseweazleDriver, '_convert_to_flux', autospec=True) as mock_convert:
        mock_convert.return_value = [1.0]
        mock_read_retry_verify = MagicMock(return_value=(create_mock_flux(sample_freq=mock_usb.sample_freq), create_mock_track_data(cyl, head, test_format)))
        mock_read.read_with_retry.side_effect = mock_read_retry_verify
        mock_util.with_drive_selected.side_effect = lambda func, usb, drive, motor=ANY: func()
        controller.disk.write_sector(cyl, head, sect, write_data)
        controller.driver.flush()
        mock_usb.seek.assert_called_with(cyl, head)
        mock_usb.write_track.assert_called_once()
        mock_read.read_with_retry.assert_called_once()
        assert (cyl, head) not in controller.driver.dirty_sectors, "Should be cleared"
        assert (cyl, head) not in controller.driver.dirty_tracks
