# Changelog

All notable changes to FatFloppy will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

### Technical
- Custom PyInstaller hooks to fix PyQt6 initialization issues
- Symlink dereferencing in DMG to ensure consistent installed size
- Entitlements for USB device access (Greaseweazle)
- Optimized PyInstaller build excludes unused Qt frameworks (57% size reduction)

### Known Issues
- No progress indicators for long operations

[0.1.0]: https://github.com/your-username/FatFloppy/releases/tag/v0.1.0
