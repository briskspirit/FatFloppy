# Changelog

All notable changes to FatFloppy will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
