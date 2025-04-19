# FatFloppy

FatFloppy is a graphical utility for browsing and managing FAT12 floppy disks on modern systems, supporting both physical disks (via Greaseweazle hardware) and raw disk images (`.img`, `.ima`). It’s a unique tool for retro computing enthusiasts, offering live file browsing for old floppies—something rare in today’s OSes. This is an **alpha version**, with core features working but more to come (e.g., formatting, progress indicators).

[![License: Unlicense](https://img.shields.io/badge/License-Unlicense-yellow.svg)](https://unlicense.org)

## Key Features

- **Browse Floppies**: View directories and files on FAT12 disks (physical or images).
- **File Operations**: Read, write, delete files, and create directories.
- **Drag and Drop**: Extract files to your OS or add files to the disk.
- **Disk Map**: Visualize sector usage (boot, FAT, root, data) with head-switching for double-sided disks.
- **Format Detection**: Auto-detects common floppy formats (e.g., 1.44MB, 720KB) or allows custom parameters.
- **Retro Focus**: Built for 3.5", 5.25", and 8" disks, with Greaseweazle integration for physical access.

## Requirements

- **Python**: 3.6+
- **Greaseweazle Hardware** (optional): For physical disks. See [Greaseweazle GitHub](https://github.com/keirf/Greaseweazle).
- **Libraries**: `greaseweazle`, `PyQt6` (install via `pip install -r requirements.txt`).

## Installation

```bash
git clone https://github.com/your-username/FatFloppy.git
cd FatFloppy
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install .
```

## Usage

```bash
fatfloppy
```

- **Open Disk**: Use `File > Open Disk Image File` for `.img/.ima` files or `File > Open Physical Floppy` for Greaseweazle-connected drives.
- **Navigate**: Use the directory tree and file list to browse.
- **Manage Files**: Extract, add, delete, or create folders via toolbar buttons or drag-and-drop.
- **View Disk Map**: See sector usage, toggle heads if double-sided.

## Known Limitations

No disk formatting or new image creation yet.
No progress indicators for long operations (may show “beach ball” on macOS).
Limited testing on non-standard or corrupted disks.
CLI is incomplete and not recommended for regular use.

## Contributing

This is a hobby project, and contributions are welcome! Please:

- Report bugs or suggest features via issues.
- Submit pull requests for fixes or enhancements.
- Test on macOS, Windows, or Linux to help ensure compatibility.

## License

MIT License (see ).

## Acknowledgements

This project heavily relies on the excellent [Greaseweazle](https://github.com/keirf/Greaseweazle) hardware and software by Keir Fraser.
PyQt6 for the GUI framework.
