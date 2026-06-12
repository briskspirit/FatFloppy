# FatFloppy

FatFloppy is a PyQt6-based graphical utility for browsing and managing vintage floppy disk images and physical disks via Greaseweazle hardware. It supports FAT12, CP/M, HDOS, Commodore CBM DOS, and Apollo DOMAIN filesystems (wbak backups and native AEGIS volumes) across multiple disk image formats (IMG, IMD, H17, MITS DSK, D64/D71/D81, Apollo floppy). This is an **alpha version**, with core features working but more to come.

[![License: Unlicense](https://img.shields.io/badge/License-Unlicense-yellow.svg)](https://unlicense.org)

## Key Features

- **Multiple Filesystems**: FAT12, CP/M, HDOS, CBM DOS, and Apollo DOMAIN
  wbak and AEGIS (read-only) support, including many non-standard vintage
  layouts (no-BPB DOS, DEC Rainbow, 86-DOS, hard-sectored Heath H17 / MITS
  Altair CP/M, and more)
- **Multiple Formats**: IMG, IMD, H17, MITS DSK, Commodore D64/D71/D81, and
  Apollo DOMAIN floppy image formats (including error-byte D64/D71 variants;
  42-track D64 images open read-only)
- **Physical Disk Access**: Greaseweazle hardware integration
- **File Operations**: Read, write, delete files, and create directories
- **Create & Format**: Make new blank images and format them to a chosen profile
- **Drag and Drop**: Extract files to your OS or add files to the disk
- **Disk Map**: Visualize sector usage with head-switching for double-sided disks
- **Format Detection**: Content-based auto-detection picks the right driver and
  format from the file's contents (not just its extension), or allows custom
  parameters

## What's Supported

| Filesystem | Read | Write | Image formats | Notes |
|---|:-:|:-:|---|---|
| **FAT12** (DOS 1.x–3.x) | ✓ | ✓ | IMG, IMD | incl. no-BPB DOS 1.x, DEC Rainbow RX50, 86-DOS |
| **CP/M 2.2** | ✓ | ✓ | IMG, IMD, H17, MITS DSK | many OEM layouts; unknown DPBs inferred from the disk |
| **HDOS** (Heath/Zenith) | ✓ | ✓ | IMG (H8D), H17 | H17/H37/H47 controller geometries |
| **CBM DOS** (Commodore 1541/1571/1581) | ✓ | ✓ | D64, D71, D81 | REL files, 1581 partitions; error-byte variants preserved; 42-track D64 read-only |
| **Apollo DOMAIN wbak** backups | ✓ | — | Apollo IMG | split backup sets reassembled across volumes ("insert next floppy") |
| **Apollo AEGIS** native (SR9) | ✓ | — | Apollo IMG | boot/utility floppies; SR10 recognized but not claimed |

Physical disks: FAT12, CP/M, and HDOS media (FM/MFM) can be read and written
directly through Greaseweazle hardware. Commodore 5.25" GCR media and Apollo
floppies are currently image-only. New blank images can be created and
formatted for the writable filesystems' profiles.

## Installation

### Windows

Download the latest installer from [Releases](https://github.com/briskspirit/FatFloppy/releases):

1. Run `FatFloppy-{version}-Windows-Setup.exe`
2. Follow the installation wizard
3. Launch from Start Menu or desktop shortcut

**Requirements**: Windows 10 or later (64-bit)

### macOS

Download the latest DMG from [Releases](https://github.com/briskspirit/FatFloppy/releases):

1. Open the DMG and drag FatFloppy.app to Applications folder
2. Right-click the app and select "Open" (first launch only)
3. Click "Open" in the security dialog

**Note**: The app is unsigned. macOS will show a security warning on first launch.

### Linux

Download the latest AppImage from [Releases](https://github.com/briskspirit/FatFloppy/releases):

1. Make the AppImage executable:
   ```bash
   chmod +x FatFloppy-{version}-x86_64.AppImage
   ```
2. Run it:
   ```bash
   ./FatFloppy-{version}-x86_64.AppImage
   ```

**Double-click to run**: On Ubuntu 24.04+, you may need to enable executable text files to run on double-click:
- Open Files (Nautilus) → Preferences → Behavior
- Under "Executable Text Files", select "Run them" or "Ask what to do"

**FUSE requirement**: If you get a FUSE error, either install FUSE2 or run:
```bash
./FatFloppy-{version}-x86_64.AppImage --appimage-extract-and-run
```

**Greaseweazle USB access**: To use physical floppy drives with Greaseweazle, install the udev rule:
```bash
# Download the rule (or extract from AppImage at usr/share/doc/fatfloppy/)
wget https://raw.githubusercontent.com/keirf/greaseweazle/master/scripts/49-greaseweazle.rules

# Install it
sudo cp 49-greaseweazle.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger

# Physically unplug and reconnect your Greaseweazle
```

### From Source

**Requirements:**
- Python 3.9+
- PyQt6
- Greaseweazle (for physical disk access)

**Install:**
```bash
git clone https://github.com/briskspirit/FatFloppy.git
cd FatFloppy
pip install -e ".[dev]"
```

## Usage

```bash
fatfloppy
```

- **Open Disk**: Use `File > Open Disk Image File` for `.img/.ima` files or `File > Open Physical Floppy` for Greaseweazle-connected drives.
- **Navigate**: Use the directory tree and file list to browse.
- **Manage Files**: Extract, add, delete, or create folders via toolbar buttons or drag-and-drop.
- **View Disk Map**: See sector usage, toggle heads if double-sided.

### CBM DOS (D64/D71/D81) Notes

- **File types**: file names take c1541-style type suffixes — `,p` (PRG, the
  default when no suffix is given), `,s` (SEQ), `,u` (USR), and `,r:<len>`
  (REL with a record length of 1-254). A comma that does not parse as a type
  code stays part of the file name.
- **Writes**: rewriting an existing name uses scratch-and-replace semantics
  (like CBM DOS `@0:`), and REL files get proper side sectors (plus super
  side sectors on the 1581).
- **Partitions (D81 only)**: creating a folder named `NAME,<sectors>` makes a
  1581 partition formatted as a browsable sub-directory (`NAME` alone
  defaults to 120 sectors; the size must be at least 120 sectors and a
  multiple of 40, i.e. whole tracks).

### Apollo DOMAIN wbak Notes

- **Read-only**: Apollo floppies are archival backup media; FatFloppy opens
  them read-only.
- **Container**: `.img` files of exactly 1,261,568 bytes (77×2×8×1024)
  carrying the `APOLLO` physical-volume signature.
- **Browsing**: each `wbak` backup tree on the disk mounts as a directory
  hierarchy with the genuine Apollo timestamps.
- **Damage handling**: files damaged on the medium are flagged `DMG` and read
  back with their holes zero-filled; a file cut by the end of the volume is
  flagged `PARTIAL` and reads back as the available prefix.
- **Split backup sets**: extracting a file cut at end-of-volume prompts to
  open the next volume image of the backup set and stitches the pieces
  together; picking an image from the wrong set (or the wrong volume of the
  right set) is detected and re-prompted, and chains of any length are
  followed volume by volume (N-volume sets prompt once per missing volume).
  Declining extracts the available prefix as before; drag-out to the host and
  extraction from physical media (Greaseweazle) always extract the available
  prefix without prompting (a modal dialog cannot interrupt a drag or a
  worker-thread read).
- **AEGIS disks**: AEGIS-filesystem (native) Apollo floppies are detected
  separately and browse as native volumes — see the next section.

### Apollo AEGIS Notes

- **Read-only**: native AEGIS volumes open read-only, like the wbak media.
- **Browsing**: SR9-era AEGIS boot/utility floppies mount as the volume's
  real directory tree with the genuine Apollo timestamps and object types
  (`TEXT`, `OBJ`, `SYSBOOT`, ...); managed objects (text/record/hdru) read
  back with their 32-byte storage headers stripped, the same view AEGIS
  itself presents.
- **Verified structures**: detection and the filesystem check walk the real
  on-disk structures — PV/LV labels, the VTOC and its index blocks, and
  every file map — and reconcile the resulting block ownership against the
  volume's BAT bitmap (clean volumes reconcile perfectly).
- **Damage handling**: entries whose VTOCE is missing or whose file map
  points off-volume are flagged `DMG` and read back with holes zero-filled
  instead of failing the volume.
- **SR10 disks**: SR10-or-later volumes (a different VTOCE layout) are
  recognized as AEGIS but deliberately not claimed, so they are never
  misread; they open as raw Apollo containers only.

### Filenames on Import/Export

Importing a host file auto-generates a name valid for the target filesystem:
FAT, CP/M, and HDOS get 8.3 names with `~NN` de-duplication; CBM keeps up to
16 PETSCII characters with `-NN` de-duplication, and commas are neutralized
so type suffixes like `,s` stay deliberate rather than accidental. Names you
type yourself are validated by the filesystem and rejected with a clear error
instead of being silently truncated. On export, characters illegal on the
host are sanitized (e.g. CBM `COPY/ALL` becomes `COPY_ALL`) and collisions
within the same batch are uniquified.

## Building from Source

See [packaging/macos/](packaging/macos/) for macOS, [packaging/windows/](packaging/windows/) for Windows, and [packaging/linux/](packaging/linux/) for Linux build instructions.

```bash
# macOS
pip install -e ".[dev]"
make dmg  # Creates DMG installer

# Windows PowerShell
pip install -e ".[dev]"
pip install pyinstaller pillow
.\packaging\windows\build_windows.ps1  # Creates installer

# Linux
pip install -e ".[dev]"
pip install pyinstaller
make appimage  # Creates AppImage (requires linuxdeploy and linuxdeploy-plugin-qt)
```

## Known Limitations

- No progress indicators for long operations
- Some rare/proprietary disk-image containers are not yet recognized

## Contributing

This is a hobby project, and contributions are welcome! Please:

- Report bugs or suggest features via issues.
- Submit pull requests for fixes or enhancements.
- Test on macOS, Windows, or Linux to help ensure compatibility.

## License

[Unlicense License](https://unlicense.org).

## Acknowledgements

- [Greaseweazle](https://github.com/keirf/Greaseweazle) hardware and software by Keir Fraser
- [PyQt6](https://www.riverbankcomputing.com/software/pyqt/) for the GUI framework
- [JetBrains Mono](https://github.com/JetBrains/JetBrainsMono) font by JetBrains
