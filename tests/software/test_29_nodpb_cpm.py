"""
No-DPB CP/M detection (disks with no matching DPB profile).

CP/M has no on-disk signature or BPB; detection needs a Disk Parameter Block.
Disks whose geometry isn't covered by a shipped profile (Zenith Z-100, Kaypro II,
...) previously read as "no filesystem". These tests assert FatFloppy now infers
the layout and reads them - using real public-domain images - while never
mis-detecting a FAT12 disk as CP/M.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from fatfloppy.core.controller import DiskController  # noqa: E402
from fatfloppy.core.filesystems.cpm_fs import CPMFilesystem  # noqa: E402
from fatfloppy.core.filesystems.fat12_fs import FATFilesystem  # noqa: E402

CPM_RES = Path(__file__).parent.parent / "resources" / "cpm_extra"

# (filename, an expected file that must list and read back)
CASES = [
    ("kaypro2_cpm22.imd", "PIP.COM"),
    ("cpm85_z100.imd", "ASM.COM"),
]


@pytest.mark.parametrize("filename,expected_file", CASES, ids=[c[0] for c in CASES])
def test_nodpb_cpm_detects_and_reads(filename, expected_file):
    path = CPM_RES / filename
    if not path.exists():
        pytest.skip(f"resource missing: {path}")

    controller = DiskController()
    assert controller.open_disk(str(path), disk_type="IMD"), (
        f"failed to open {filename}"
    )
    assert isinstance(controller.filesystem, CPMFilesystem), (
        f"{filename} not detected as CP/M (got {type(controller.filesystem).__name__})"
    )

    listing = [item["name"] for item in controller.list_directory("/")]
    assert expected_file in listing, (
        f"{expected_file} not in {filename} listing: {listing[:12]}"
    )

    data = controller.read_file("/" + expected_file)
    assert data, f"could not read {expected_file} from {filename}"
    controller.close_disk()


def _bait_entry() -> bytes:
    """A 32-byte directory slot that is printable garbage, not a real CP/M
    entry: user 10, lowercase/symbol filename, valid-looking block pointers.
    Mirrors the single slot in the corpus' Archon.d64 (a C64 game disk) that
    the DPB sweep latched onto."""
    return (
        bytes([0x0A])  # user 10
        + b"^s######"  # name: leading symbol + lowercase
        + b"v##"  # ext: lowercase
        + bytes([0, 0, 0, 0x10])  # ex/s1/s2/rc
        + bytes(range(1, 17))  # plausible small block pointers
    )


def test_nodpb_scan_rejects_single_garbage_entry(tmp_path):
    """The DPB sweep tries hundreds of (off, bsh, drm, skew) layouts; on a
    non-CP/M disk a single printable-but-implausible entry amid 0xE5 fill
    (common in game data - corpus: Archon.d64) must not register as CP/M."""
    from fatfloppy.core.disk import Disk
    from fatfloppy.core.drivers import IMGImageDriver
    from fatfloppy.core.filesystem_registry import FilesystemRegistry

    img = bytearray(b"\xe5" * (40 * 2 * 9 * 512))
    track_bytes = 9 * 512
    for t in range(5):  # cover every swept 'off' with skew 1
        img[t * track_bytes : t * track_bytes + 32] = _bait_entry()
    path = tmp_path / "bait.img"
    path.write_bytes(img)

    fmt = FilesystemRegistry.get_all_formats()["ibm_5.25_360k"]
    drv = IMGImageDriver(str(path))
    drv.set_physical_format(fmt.physical_format)
    disk = Disk(drv)
    disk.set_geometry(fmt.physical_format)
    fs = CPMFilesystem(disk)
    assert fs.get_validity_score() < CPMFilesystem.validity_threshold


def test_nodpb_scan_still_accepts_plausible_entry(tmp_path):
    """Counterpart guard: the same sweep must keep accepting a layout whose
    single entry looks like a real CP/M filename (uppercase, leading
    alphanumeric) - the plausibility gate must not over-tighten."""
    from fatfloppy.core.disk import Disk
    from fatfloppy.core.drivers import IMGImageDriver
    from fatfloppy.core.filesystem_registry import FilesystemRegistry

    entry = (
        bytes([0x00])
        + b"HELLO   "
        + b"COM"
        + bytes([0, 0, 0, 0x10])
        + bytes(range(1, 17))
    )
    img = bytearray(b"\xe5" * (40 * 2 * 9 * 512))
    track_bytes = 9 * 512
    for t in range(5):
        img[t * track_bytes : t * track_bytes + 32] = entry
    path = tmp_path / "plausible.img"
    path.write_bytes(img)

    fmt = FilesystemRegistry.get_all_formats()["ibm_5.25_360k"]
    drv = IMGImageDriver(str(path))
    drv.set_physical_format(fmt.physical_format)
    disk = Disk(drv)
    disk.set_geometry(fmt.physical_format)
    fs = CPMFilesystem(disk)
    assert fs.get_validity_score() >= CPMFilesystem.validity_threshold
    assert any(f.name == "HELLO.COM" for f in fs.list_directory("/"))


def _listing_text(size: int) -> bytes:
    """Realistic DIR-listing text: uppercase filenames, CRLF lines, ending in
    a CP/M ^Z text-EOF marker (0x1A counts as text - CP/M-era text files end
    with it). Newline bytes (0x0A/0x0D) are valid CP/M user numbers and the
    uppercase words after them parse as plausible filenames, which is exactly
    how the DPB sweep latched onto such files."""
    lines = []
    for i in range(4000):
        sz = (i * 7) % 200 + 1
        letter = chr(65 + i % 26)
        lines.append(f"RT11{letter}{letter}.SYS   {sz:3d}P 04-May-87")
    return ("\r\n".join(lines)).encode("ascii")[: size - 1] + b"\x1a"


def test_nodpb_scan_rejects_pure_text_image(tmp_path):
    """A plain ASCII text file (e.g. a DIR listing saved next to disk images)
    must not be claimed as a CP/M volume by DPB inference: before the
    pure-text gate this exact image scored 77 with phantom entries."""
    path = tmp_path / "listing.txt"
    path.write_bytes(_listing_text(100 * 1024))

    controller = DiskController()
    assert controller.open_disk(str(path))
    assert not isinstance(controller.filesystem, CPMFilesystem), (
        "pure-text file was claimed as CP/M by DPB inference"
    )
    controller.close_disk()


def test_nodpb_scan_rejects_real_rt11_listing():
    """Real-world case: RT-11 distribution listing .TXT files (100% printable
    text) misdetected as CP/M with phantom entries before the pure-text gate.
    (p732i.dir.txt, another real victim, carries 77 trailing 0x00 padding
    bytes, so the deliberately-exact gate does not fire for it - see
    test_pure_text_gate_requires_every_byte_text.)"""
    path = (
        Path(__file__).parent.parent.parent
        / "local_images"
        / "RT11"
        / "RT11-V05.01.d"
        / "BA-P727B-BC.TXT"
    )
    if not path.exists():
        pytest.skip(f"local image missing: {path}")

    controller = DiskController()
    assert controller.open_disk(str(path))
    assert not isinstance(controller.filesystem, CPMFilesystem), (
        "RT-11 listing text file was claimed as CP/M by DPB inference"
    )
    controller.close_disk()


@pytest.mark.parametrize("non_text_byte", [0x00, 0xE5], ids=["0x00", "0xE5"])
def test_pure_text_gate_requires_every_byte_text(tmp_path, non_text_byte):
    """The pure-text gate is exact: a single non-text byte (even a 0x00 or
    0xE5 that could be directory fill) disables it, so it can never block a
    real disk image. Inference may still reject such an image for other
    reasons - this pins the gate itself."""
    from fatfloppy.core.disk import Disk
    from fatfloppy.core.drivers import IMGImageDriver
    from fatfloppy.core.filesystem_registry import FilesystemRegistry

    img = bytearray(_listing_text(40 * 2 * 9 * 512))
    fmt = FilesystemRegistry.get_all_formats()["ibm_5.25_360k"]

    def _gate(data: bytes) -> bool:
        path = tmp_path / f"gate_{non_text_byte:02x}_{len(data)}.img"
        path.write_bytes(data)
        drv = IMGImageDriver(str(path))
        drv.set_physical_format(fmt.physical_format)
        disk = Disk(drv)
        disk.set_geometry(fmt.physical_format)
        return CPMFilesystem(disk)._image_is_pure_text()

    assert _gate(bytes(img)) is True, "all-text image must trip the gate"
    img[len(img) // 2] = non_text_byte
    assert _gate(bytes(img)) is False, (
        f"one 0x{non_text_byte:02X} byte must disable the pure-text gate"
    )


@pytest.mark.parametrize(
    "filename,disk_type",
    [
        ("nobpb_fat12/pcdos100_160k.imd", "IMD"),
        ("nobpb_fat12/msdos125_compaq_320k.img", "IMG"),
        ("empty_formatted_144m.img", "IMG"),
    ],
)
def test_nodpb_cpm_scan_does_not_false_detect_fat(filename, disk_type):
    """The CP/M layout scan must never claim a FAT12 disk as CP/M."""
    path = Path(__file__).parent.parent / "resources" / filename
    if not path.exists():
        pytest.skip(f"resource missing: {path}")
    controller = DiskController()
    controller.open_disk(str(path), disk_type=disk_type)
    assert isinstance(controller.filesystem, FATFilesystem), (
        f"{filename} (FAT12) was mis-detected as {type(controller.filesystem).__name__}"
    )
    controller.close_disk()
