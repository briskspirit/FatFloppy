# Changelog

All notable changes to FatFloppy will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-09-13

### Added
- DEC RT-11 filesystem support (RX01/RX02/RX50 floppy volumes; raw images,
  IMD, and TD0 containers):
  - full read/write with 6.3 RAD50 file names, real RT-11 date words
    (genuine dates on listing, today's date stamped on write), and
    contiguous first-fit allocation with directory segment splitting;
    deleted entries are left in place rather than coalesced, matching real
    RT-11 (only SQUEEZE merges free space)
  - both archival conventions for the same floppy — raw physical sector
    order (the DEC handler interleave) and plain logical block order — are
    auto-resolved by scoring the directory structure under each candidate
    view, on read and write alike, even when the file sizes are identical
  - creating and formatting new blank RT-11 images (INIT-style per-device
    directory-segment defaults)
  - VALIDATE-style filesystem check walking the directory segment chain
    (overlapping runs, device overruns, cyclic or broken chains)
  - verified file-content-exact against an independent extractor across a
    234-file local corpus of real DEC distribution media (V03B through
    V5.4B), with zero regressions elsewhere in the real-image corpus
- Teledisk TD0 container driver (read-only): both normal and
  "advanced"-compressed TD0 archives open transparently; any filesystem
  auto-detected inside (FAT12, CP/M, and others) is browsable without any
  manual format selection. Byte-exact sector data verified against an
  independent Greaseweazle-decoded IMD twin across a 19-image public-domain
  corpus (zero mismatches).
- Apollo AEGIS native floppy support (read-only):
  - browsing and extraction of SR9-era AEGIS boot/utility floppies: the
    volume's real directory tree (PV/LV labels, VTOC, file maps) with the
    genuine Apollo timestamps and object types; managed objects
    (text/record/hdru) read back with their 32-byte storage headers
    stripped, the same view AEGIS itself presents
  - filesystem check reconciling block ownership (labels, BAT, VTOC, index
    blocks, directory and file pages) against the volume's BAT bitmap
    (clean volumes reconcile perfectly)
  - entries with missing VTOCEs or out-of-range file maps are flagged
    damaged and read back with holes zero-filled instead of failing the
    volume
  - SR10-or-later volumes (a different VTOCE layout) are recognized but
    deliberately never claimed, so they cannot be misread
- Apollo DOMAIN wbak backup floppy support (read-only):
  - Apollo floppy image driver for the 77x2x8x1024 physical-volume container
    (`.img`, exactly 1,261,568 bytes with the `APOLLO` signature)
  - wbak "tape-on-floppy" stream parser covering all four layers of the format
    (segment framing, ANSI X3.27 labels, backup blocks, object records), with
    recovery of the wbak writer's pathologies on real media (stale sectors,
    absorbed/lost middle segments, drop-on-miss) -- damaged files read back
    with holes zero-filled and are flagged, files cut by end-of-volume are
    flagged partial
  - read-only filesystem presenting each backup tree as a directory hierarchy
    with the genuine Apollo timestamps
  - cross-volume reassembly of split backup sets: extracting a file cut at
    end-of-volume prompts to open the next volume image and stitches the
    pieces together (wrong-volume picks are detected and re-prompted;
    N-volume chains prompt once per missing volume; declining, dragging the
    file out to the host, or extracting from physical media all yield the
    available prefix without prompting)
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

- The file viewer now loads BOTH the text and hex views on every open; the
  text-vs-binary guess only decides which tab is raised, so a wrong guess
  just means switching tabs. Binary files get a read-only text view that
  can never be saved back.
- Apollo wbak objects whose NAME record was clipped on the medium are now
  recovered when provable (the surviving name remnant is matched against
  the adjacent file record's UID); unprovable cases keep the faithful `?`
  placeholder. Recovers names the original reference extractor cannot.
- Files cut at end-of-volume (PARTIAL) or damaged on the medium (DMG) are
  now color-coded in the file browser with explanatory tooltips.
- wbak entries whose size field was destroyed on the medium (overwritten by
  text debris) now report their actual recoverable size instead of the
  garbage value; the raw declared size stays available in the entry details.
- The wbak "insert next volume" prompt now names exactly what it needs —
  backup tree, sequence, section, and set uid — using the same wording as
  the wrong-volume rejection message.

### Fixed
- Raw RX01 disk images are no longer misdetected as FAT12: directory-entry
  scoring now requires plausible 8.3 entries (DEC's EBCDIC label track no
  longer counts), and uniform-fill FAT candidates are rejected during no-BPB
  boot-sector synthesis.
- Auto-detection no longer claims a filesystem below that filesystem's own
  validity threshold (the global claim floor masked self-invalid matches and
  could shadow a valid lower-scoring candidate).
- Plain text files (e.g. archived directory listings) are no longer claimed
  as CP/M volumes by DPB inference; trailing NUL padding from block-padded
  transfers is tolerated, and the scan is skipped on physical media.
- The file browser refreshes automatically after attaching the next volume
  of a split Apollo wbak backup set.
- Apollo AEGIS volumes that are recognized but structurally damaged now
  report the damage in the disk information panel instead of browsing as
  silently empty.
- Replacing an RT-11 file now allocates the new copy before freeing the old
  one (authentic .ENTER ordering), so an interrupted write can no longer
  leave the directory pointing at partially overwritten data; replacing a
  file on a nearly full volume may now require deleting it first, exactly
  like real RT-11.
- Detection over-claiming: HDOS and inferred-DPB CP/M no longer claim disks of
  other formats (detection is now gated on plausible geometry and directory
  contents), and the new drivers cannot shadow existing ones — all verified
  with zero content-level regressions across the real-image corpus.
- Filename handling throughout: import/export naming bugs across the GUI and
  filesystems (FAT 8.3 mangling applied to non-FAT disks, export names losing
  path-separator prefixes, CP/M extension-less files being undeletable,
  mis-normalized import paths).
- GUI robustness: crashes and wrong displays around read-only filesystems,
  dialogs on worker threads, and the space/usage panel.
- Write-path correctness on MITS Altair minifloppies and CP/M disks with
  all-zero directory slots, plus broad safety/correctness hardening across
  physical-disk access, image parsing, write ordering, and hostile-input
  handling (audit remediation, Groups 1-10).

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
  filesystem suggests valid, de-duplicated import names (FAT/CP/M 8.3 with
  `~NN`; HDOS letter-first 8.3 with digit suffixes; CBM 16-character PETSCII
  with `-NN`; RT-11 6.3 RAD50 with digit suffixes) and sanitizes export
  names for the host.
- HDOS rejects filenames outside the real HDOS charset on write (letter
  first, then letters and digits, for both name and extension); lookups stay
  lenient so existing on-disk oddities remain addressable.

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

[0.2.0]: https://github.com/briskspirit/FatFloppy/releases/tag/v0.2.0
[0.1.1]: https://github.com/briskspirit/FatFloppy/releases/tag/v0.1.1
[0.1.0]: https://github.com/briskspirit/FatFloppy/releases/tag/v0.1.0
