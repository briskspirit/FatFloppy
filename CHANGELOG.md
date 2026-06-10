# Changelog

All notable changes to FatFloppy will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Detection and reading of many additional vintage CP/M and DOS layouts:
  - Heath/Zenith H17 hard-sectored CP/M (4:1 software sector skew)
  - CP/M disks with no shipped DPB profile (e.g. Zenith Z-100, Kaypro II, IMSAI)
  - CP/M disks whose unused directory slots are 0x00-filled instead of 0xE5
  - No-BPB FAT12 (DOS 1.x and early OEM disks identified by media descriptor)
  - DEC Rainbow (RX50) MS-DOS FAT12 with its 2:1 interleave and reserved tracks
  - 86-DOS (Seattle Computer Products) no-BPB FAT12 with two reserved tracks
  - MITS Altair 8" CP/M with non-standard DPBs, and 5.25" minifloppy CP/M
- Content-based driver auto-detection: the right driver is chosen by validating
  the file's contents (in priority order, raw IMG last) rather than trusting the
  file extension, so images open even with a wrong or ambiguous extension.
- Creating and formatting new images for the added format profiles (DEC Rainbow,
  MITS Altair 8"/mini, 5.25" CP/M).
- "Detected Format" panel in the Disk Information dock showing the container
  driver and the matched format profile (or that the layout was inferred).
- Opt-in regression sweep over a real disk-image corpus
  (set `FATFLOPPY_TEST_IMAGES`).

### Fixed
- Extensive safety/correctness hardening across physical-disk access,
  archival-image parsing (IMD/H17), write ordering, hostile-input handling,
  and the filesystem/driver/core/GUI layers (audit remediation, Groups 1-10).
- MITS Altair minifloppy writes/creates reframing data tracks as system tracks
  and corrupting the directory.
- CP/M writes overwriting existing files on disks with all-zero directory slots.

### Changed
- `open_disk` now defaults to content-based `auto` driver detection.

## [0.1.1] - 2026-06-10

### Added
- Windows installer and portable ZIP builds
- Linux AppImage builds (x86_64)

### Fixed
- File operations on CP/M and HDOS filesystems failing on Windows due to path separator issues
- Toolbar overflow menu appearing on Windows instead of showing buttons directly
- Platform-specific font sizing for consistent appearance across macOS, Windows, and Linux

## [0.1.0] - 2026-01-22

### Added
- Initial release
- FAT12 filesystem support with complete file operations
- CP/M filesystem support with complete file operations
- HDOS filesystem support with complete file operations
- IMG (raw image) format support
- IMD (ImageDisk) format support
- H17 (Heath) format support
- MITS DSK format support
- Greaseweazle physical disk support
- PyQt6-based GUI with file browser
- Disk sector map visualization
- Drag-and-drop file operations
- Automatic format detection
- Text and hex file viewers

### Known Issues
- No progress indicators for long operations

[0.1.1]: https://github.com/briskspirit/FatFloppy/releases/tag/v0.1.1
[0.1.0]: https://github.com/briskspirit/FatFloppy/releases/tag/v0.1.0
