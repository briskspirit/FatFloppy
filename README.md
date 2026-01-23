# FatFloppy

FatFloppy is a PyQt6-based graphical utility for browsing and managing vintage floppy disk images and physical disks via Greaseweazle hardware. It supports FAT12, CP/M, and HDOS filesystems across multiple disk image formats (IMG, IMD, H17, MITS DSK). This is an **alpha version**, with core features working but more to come.

[![License: Unlicense](https://img.shields.io/badge/License-Unlicense-yellow.svg)](https://unlicense.org)

## Key Features

- **Multiple Filesystems**: FAT12, CP/M, and HDOS support
- **Multiple Formats**: IMG, IMD, H17, and MITS DSK image formats
- **Physical Disk Access**: Greaseweazle hardware integration
- **File Operations**: Read, write, delete files, and create directories
- **Drag and Drop**: Extract files to your OS or add files to the disk
- **Disk Map**: Visualize sector usage with head-switching for double-sided disks
- **Format Detection**: Auto-detects common floppy formats or allows custom parameters

## Installation

### Windows

Download the latest installer from [Releases](https://github.com/your-username/FatFloppy/releases):

1. Run `FatFloppy-{version}-Windows-Setup.exe`
2. Follow the installation wizard
3. Launch from Start Menu or desktop shortcut

**Requirements**: Windows 10 or later (64-bit)

### macOS

Download the latest DMG from [Releases](https://github.com/your-username/FatFloppy/releases):

1. Open the DMG and drag FatFloppy.app to Applications folder
2. Right-click the app and select "Open" (first launch only)
3. Click "Open" in the security dialog

**Note**: The app is unsigned. macOS will show a security warning on first launch.

### Linux

Download the latest AppImage from [Releases](https://github.com/your-username/FatFloppy/releases):

1. Make the AppImage executable:
   ```bash
   chmod +x FatFloppy-{version}-x86_64.AppImage
   ```
2. Run it:
   ```bash
   ./FatFloppy-{version}-x86_64.AppImage
   ```

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
git clone https://github.com/your-username/FatFloppy.git
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

- No disk formatting or new image creation yet
- No progress indicators for long operations
- Limited testing on non-standard or corrupted disks

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
