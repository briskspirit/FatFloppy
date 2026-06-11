"""End-to-end CBM detection through DiskController."""

from pathlib import Path

import pytest

from fatfloppy.core.controller import DiskController

RESOURCES = Path(__file__).parent.parent / "resources" / "CBM"
CASES = [
    ("vic1541_bam.d64", "1541", "cbm_1541_d64"),
    ("c128_tutorial.d64", "1541", "cbm_1541_d64"),
    ("endless_forms.d64", "1541", "cbm_1541_d64"),
    ("1571_demo.d71", "1571", "cbm_1571_d71"),
    ("1581_demo.d81", "1581", "cbm_1581_d81"),
]


@pytest.mark.parametrize("image,variant,profile", CASES)
def test_controller_detects_cbm(image, variant, profile):
    path = RESOURCES / image
    if not path.exists():
        pytest.skip(f"resource {image} not present")
    controller = DiskController()
    assert controller.open_disk(str(path), disk_type="auto")
    fs = controller.filesystem
    assert fs is not None, "expected a filesystem to be detected"
    config = fs.get_specific_config()
    assert config.variant == variant
    format_name, _fs_config, _physical_format = controller.detect_format()
    assert format_name == profile
    files = controller.list_directory("/")
    assert files
    controller.close_disk()


def test_cpm_plus_d64_detects_as_cpm():
    # Real C128 CP/M Plus disk in a D64 container: CP/M must win, CBM must not.
    path = RESOURCES / "cpm_plus_30.d64"
    if not path.exists():
        pytest.skip("resource not present")
    controller = DiskController()
    assert controller.open_disk(str(path), disk_type="auto")
    fs = controller.filesystem
    assert fs is not None
    assert fs.filesystem_type == "CPM"
    controller.close_disk()


def _crack_d64_with_cpm_bait() -> bytearray:
    """Synthetic clone of the corpus' Archon.d64 failure mode: a valid CBM
    header link/DOS byte and directory, a control-code disk name (crack-era
    directory art: '\\r{clr}LOAD"EA",8,1\\r\\r'), a zeroed BAM, and game-data
    tracks that a CP/M DPB sweep can mistake for a directory holding one
    printable-garbage entry."""
    img = bytearray(174848)  # 35-track D64
    spt = [21] * 17 + [19] * 7 + [18] * 6 + [17] * 5  # tracks 1..35

    def track_off(t):
        return sum(spt[: t - 1]) * 256

    # Tracks 1-5: 0xE5-filled "game data" with a CP/M bait entry at the
    # start of each (covers every swept CP/M 'off' value at skew 1).
    img[track_off(1) : track_off(6)] = b"\xe5" * (track_off(6) - track_off(1))
    bait = (
        bytes([0x0A])  # user 10
        + b"^s######"  # printable garbage: leading symbol + lowercase
        + b"v##"
        + bytes([0, 0, 0, 0x10])
        + bytes(range(1, 17))
    )
    for t in range(1, 6):
        img[track_off(t) : track_off(t) + 32] = bait
    # 18/0: valid dir link + DOS byte, zeroed BAM, control-code disk name.
    h = track_off(18)
    img[h : h + 3] = bytes([18, 1, 0x41])
    img[h + 0x90 : h + 0xA0] = b'\x0d\x93LOAD"EA",8,1\x0d\x0d'
    # 18/1: one valid closed PRG entry "EA" pointing at 17/0.
    d = h + 256
    img[d : d + 2] = b"\x00\xff"
    img[d + 2] = 0x82
    img[d + 3], img[d + 4] = 17, 0
    img[d + 5 : d + 0x15] = b"EA".ljust(16, b"\xa0")
    img[d + 0x1E] = 1
    # 17/0: the file's single sector (2 payload bytes).
    f = track_off(17)
    img[f : f + 4] = bytes([0, 3, 0x01, 0x08])
    return img


def test_crack_disk_with_cpm_bait_detects_as_cbm(tmp_path):
    # Corpus regression (Archon.d64): the heuristic CP/M DPB inference must
    # not outbid a structurally valid CBM directory on a crack disk.
    path = tmp_path / "crack.d64"
    path.write_bytes(_crack_d64_with_cpm_bait())
    controller = DiskController()
    assert controller.open_disk(str(path), disk_type="auto")
    fs = controller.filesystem
    assert fs is not None
    assert fs.filesystem_type == "CBMDOS", (
        f"crack disk misdetected as {fs.filesystem_type}"
    )
    names = [i["name"] for i in controller.list_directory("/")]
    assert names == ["EA"]
    assert controller.read_file("/EA") == b"\x01\x08"
    controller.close_disk()


def test_profiles_registered():
    controller = DiskController()
    names = [name for name, _desc in controller.list_formats()]
    assert any("d64" in n for n in names)
    assert any("d71" in n for n in names)
    assert any("d81" in n for n in names)
