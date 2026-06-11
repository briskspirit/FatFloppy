# Changelog

All notable changes to FatFloppy will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Apollo DOMAIN wbak backup floppy support (read-only):
  - Apollo floppy image driver for the 77x2x8x1024 physical-volume container
    (`.img`, exactly 1,261,568 bytes with the `APOLLO` signature; AEGIS-native
    disks are recognized as Apollo containers but not yet browsable)
  - wbak "tape-on-floppy" stream parser covering all four layers of the format
    (segment framing, ANSI X3.27 labels, backup blocks, object records), with
    recovery of the wbak writer's pathologies on real media (stale sectors,
    absorbed/lost middle segments, drop-on-miss) -- damaged files read back
    with holes zero-filled and are flagged, files cut by end-of-volume are
    flagged partial
  - read-only filesystem presenting each backup tree as a directory hierarchy
    with the genuine Apollo timestamps
- Commodore CBM DOS support:
  - D64/D71/D81 image driver (1541/1571/1581 variable-zone geometry derived
    from the exact file size), including the trailing error-byte D64/D71
    variants (preserved verbatim on write) and 42-track D64 images (read-only)
  - CBM DOS filesystem with full read/write: PETSCII file names, c1541-style
    type suffixes (`,p` `,s` `,u` `,r:<len>`), scratch-and-replace rewrites,
    and REL files with side sectors (plus super side sectors on the 1581)
  - 1581 (D81) partitions browsable and creatable as sub-directories
    (`NAME,<sectors>`)
  - VALIDATE-style filesystem check reconciling the BAM against the directory
  - Format profiles, detection, and formatting of new blank D64/D71/D81 images
- 80-track double-sided H37 HDOS format profile (`hdos_5.25_400k_dssd`), so
  raw 400KB 80x2x10x256 HDOS dumps are detected and writable.
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
  (set `FATFLOPPY_TEST_IMAGES`); the CBM and detection changes were verified
  against it with zero regressions in per-file content hashes.

### Fixed
- GUI crash when importing files onto a read-only filesystem: the synchronous
  import path let the filesystem's error escape a Qt slot (fatal in PyQt);
  failures are now collected and shown in a dialog.
- Extensive safety/correctness hardening across physical-disk access,
  archival-image parsing (IMD/H17), write ordering, hostile-input handling,
  and the filesystem/driver/core/GUI layers (audit remediation, Groups 1-10).
- MITS Altair minifloppy writes/creates reframing data tracks as system tracks
  and corrupting the directory.
- CP/M writes overwriting existing files on disks with all-zero directory slots.
- HDOS claiming disks whose geometry no HDOS controller can produce: detection
  is now gated to the H17/H37/H47 shapes (40/80 tracks x 10, 77 x 26, 256-byte
  sectors), so variable-zone disks (e.g. D64) are never misdetected as HDOS.
- CP/M DPB inference claiming non-CP/M disks: the heuristic sweep now requires
  plausible directory filenames before accepting an inferred layout.
- Space/usage display showing every sector free on CBM disks (the filesystem
  did not expose its allocation unit size to the GUI).
- GUI applying FAT 8.3 name mangling to every filesystem on import, so CBM
  names were truncated and `,s`-style type suffixes destroyed.
- Exports of names containing path separators losing everything before the
  separator (CBM `COPY/ALL` exported as `ALL`); host-illegal characters are
  now sanitized instead.
- CP/M extension-less files being undeletable and duplicating directory
  entries on overwrite (the listed name and the path parser disagreed).
- Root-level import paths with special characters being mis-normalized.

### Changed
- `open_disk` now defaults to content-based `auto` driver detection.
- CP/M and HDOS reject invalid or over-length filenames on write instead of
  silently truncating, and CP/M user numbers are bounded to 0-15 on write
  (lookups stay lenient so existing on-disk oddities remain addressable).
- FAT12 rejects the punctuation MS-DOS forbids in 8.3 short names
  (`, ; = [ ] +`) on write, and import suggestions replace those characters
  with `_`. Directory entries already carrying them (only writable by
  non-DOS tools) are now skipped on listing like other invalid entries;
  verified with zero impact across the real-image corpus.
- Import and export naming is delegated to per-filesystem policies: each
  filesystem suggests valid, de-duplicated import names (FAT/CP/M/HDOS 8.3
  with `~NN`; CBM 16-character PETSCII with `-NN`) and sanitizes export
  names for the host.

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
