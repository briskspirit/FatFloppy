# FatFloppy

FatFloppy is a graphical utility designed to browse, read, write, and manage files on floppy disks (both physical disks accessed via Greaseweazle hardware and raw disk image files) using a modern operating system. It primarily targets FAT12 filesystems commonly found on DOS-formatted floppy disks.

It provides a user-friendly interface to interact with floppy disk contents, including a visual representation of the disk layout and sector usage.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
<!-- Add other badges here if applicable (Build Status, Version, etc.) -->

## Overview

Accessing floppy disks on modern computers can be challenging. Hardware like Greaseweazle provides the necessary low-level interface, but software is needed to interpret the filesystem structure and manage files. FatFloppy aims to bridge this gap by providing:

1.  **Low-level Access:** Leverages the Greaseweazle library to communicate with physical floppy drives connected via Greaseweazle hardware.
2.  **Image File Support:** Can directly open and manipulate raw sector-based disk image files (`.img`, `.ima`).
3.  **Filesystem Interpretation:** Implements FAT12 filesystem logic to parse boot sectors, FATs, directories, and files.
4.  **User Interface:** Offers a graphical interface (built with PyQt6) for intuitive navigation and file management.
5.  **Visualization:** Includes a disk map to visualize sector allocation (boot sector, FATs, root directory, data area).

## Key Features

*   **Browse Physical Floppies:** Connect a floppy drive via Greaseweazle and browse its contents.
*   **Work with Disk Images:** Open, view, and modify common raw disk image files (`.ima`, `.img`).
*   **FAT12 Filesystem Support:**
    *   List directories and files.
    *   Read file contents.
    *   Write files (including overwriting existing files).
    *   Create new directories.
    *   Delete files and empty directories.
    *   View file/directory attributes and timestamps.
*   **Format Detection:** Attempts to automatically detect common floppy disk formats based on boot sector parameters.
*   **Manual Format Specification:** Allows specifying physical drive parameters (drive type, size) and selecting predefined formats or defining custom geometry/physical parameters for physical disks.
*   **Greaseweazle Integration:**
    *   Reads and writes tracks using flux transitions.
    *   Measures drive RPM.
    *   Supports custom disk definitions for non-standard formats.
    *   Includes sector/track caching for performance.
*   **Visual Disk Map:** Displays a graphical representation of cylinders and sectors, color-coded by usage (Boot, FAT, Root Dir, Data Free/Used). Supports switching views between heads (sides) for double-sided disks.
*   **Drag and Drop:**
    *   Drag files *from* the application to your OS file explorer to extract them.
    *   Drag files *to* the application from your OS file explorer to add them to the current directory on the disk (prompts for 8.3 filename).
*   **Disk Information Display:** Shows detected physical geometry, filesystem parameters (from BPB), and free/used space.
*   **Cross-Platform Potential:** Built with Python and PyQt6, aiming for compatibility where Greaseweazle and dependencies are supported.

## Screenshots

*(Add screenshots of the main window, disk map, dialogs etc. here)*

Example:
`![Main Window](docs/images/screenshot_main.png)`
`![Disk Map](docs/images/screenshot_diskmap.png)`
`![Drive Selection](docs/images/screenshot_dialog.png)`

## Requirements

*   **Python:** Python 3.6 or higher.
*   **Greaseweazle Hardware (Optional):** Required for accessing physical floppy drives. See the [Greaseweazle GitHub](https://github.com/keirf/Greaseweazle) for hardware details.
*   **Python Libraries:**
    *   `greaseweazle` (Installs necessary dependencies like `pyserial`, `crcmod`, `bitarray`)
    *   `PyQt6`

## Installation

1.  **Clone the repository:**
    ```bash
    git clone https://github.com/your-username/FatFloppy.git
    cd FatFloppy
    ```

2.  **Install dependencies:**
    It's recommended to use a virtual environment:
    ```bash
    python -m venv venv
    source venv/bin/activate  # On Windows use `venv\Scripts\activate`
    ```
    Install using `pip` which will use `setup.py`:
    ```bash
    pip install .
    ```
    Alternatively, install requirements directly (ensure `greaseweazle` is installed first if needed):
    ```bash
    pip install -r requirements.txt
    pip install greaseweazle
    ```

## Usage

### Graphical User Interface (GUI)

Run the application from the command line after installation:

```bash
fatfloppy
```

Or, if running directly from the source directory (with dependencies installed):

```bash
python main.py
```

**Opening a Disk:**

*   **Disk Image:** Go to `File` -> `Open Disk Image File` and select your `.ima` or `.img` file.
*   **Physical Floppy:** Go to `File` -> `Open Physical Floppy`.
    *   Select the Drive Interface Type (usually "IBM PC (A/B)").
    *   Select the connected Drive (A or B, or 0-3 for Shugart).
    *   Select the physical Drive Size (3.5", 5.25", 8").
    *   (Optional) Select a known Format profile or choose "Custom..." to manually enter physical parameters (Encoding, Rate, RPM, Geometry, Gaps, etc.). Choosing "Auto-detect" is generally recommended unless you know the specific format or auto-detection fails.
    *   Click `OK`.

**Navigating:**

*   Use the **Directory Tree** pane on the left to select directories.
*   The **Files in Current Directory** pane on the right will show the contents of the selected directory.

**File Operations:**

*   **Extract:** Select a file in the file list and click the `Extract` button on the toolbar (or drag the file to your desktop/explorer).
*   **Add File:** Click the `Add File` button on the toolbar (or drag a file from your desktop/explorer onto the file list pane). You will be prompted to confirm/enter the 8.3 filename.
*   **Delete:** Select a file or empty directory in the file list and click the `Delete` button on the toolbar. You will be asked for confirmation.
*   **New Folder:** Click the `New Folder` button on the toolbar. You will be prompted for the 8.3 directory name.

**Disk Map:**

*   The **Disk Map** pane visualizes sector usage for the currently selected head (side).
*   Use the `Switch to Head X` button on the toolbar to toggle between sides on double-sided disks.
*   Colors indicate sector type (see legend on the map).

**Disk Information:**

*   The **Disk Information** pane displays details about the physical geometry and the detected filesystem parameters (like sectors per cluster, volume label, etc.) derived from the Boot Sector/BPB.

### Command Line Interface (CLI)

The CLI mode is currently very basic and mainly for testing.

```bash
# Open and show basic info for an image file
python main.py --image path/to/your/disk.img

# Open and show basic info for a physical drive (less useful without interaction)
# python main.py --device COM3 # Example device name
```

## Technical Details

*   **Architecture:** FatFloppy uses a layered approach:
    *   **GUI (`gui/`):** PyQt6 components for the user interface (Main Window, Dialogs, Disk Map, File Browser).
    *   **Controller (`core/controller.py`):** Orchestrates operations, manages the current disk state, and acts as the bridge between the UI and the core logic.
    *   **Disk (`core/disk.py`):** Represents the logical disk, handling geometry and sector-level read/write requests passed to the driver. Performs LBA/CHS conversion.
    *   **Drivers (`core/drivers.py`):** Abstract `DiskIODriver` and concrete implementations (`GreaseweazleDriver`, `RawImageDriver`) for interacting with the physical disk or image file. `GreaseweazleDriver` handles flux-level operations and caching.
    *   **Filesystem (`core/filesystem.py`):** Implements the `FATFilesystem` logic (currently FAT12 focused), including boot sector parsing, FAT table management (with caching), directory entry handling, and cluster chain processing.
    *   **Formats (`core/formats.py`, `core/format_definitions.py`):** Defines standard floppy formats (`FormatProfile`) linking logical geometry, physical parameters (`PhysicalFormat`), and boot sector data (`BootSectorData`). `FormatManager` assists in detection.
    *   **Utilities (`utils/`):** Logging configuration.
*   **Greaseweazle Usage:** The `GreaseweazleDriver` uses the `greaseweazle` library for:
    *   Opening USB connection to the device.
    *   Selecting the drive.
    *   Measuring drive RPM.
    *   Reading raw track flux data.
    *   Writing flux data to tracks.
    *   Decoding tracks using `greaseweazle.codec` (specifically trying `ibm.scan` for detection and standard IBM FM/MFM formats).
    *   Generating flux for writing based on sector data and format parameters.
*   **Disk Map Visualization:** The map renders sectors as segments of concentric rings (cylinders). Colors are determined by mapping the absolute sector number (calculated from Cylinder, Head, Sector) to its role based on FAT filesystem layout parameters (Boot Sector, FATs, Root Directory, Data Area). Data area sectors are further colored based on whether the corresponding cluster is marked as allocated in the FAT.

## Supported Formats

FatFloppy primarily focuses on **FAT12** as found on IBM PC compatible floppy disks. It includes definitions for many standard formats, including:

*   **3.5"**: 1.44MB (HD), 720KB (DD), 2.88MB (ED), DMF formats (1.68M, 1.72M)
*   **5.25"**: 1.2MB (HD), 360KB (DD), 180KB (DD), 160KB (DD)
*   **8"**: Various SD/DD formats (e.g., 1.2MB, 500KB, 250KB)

See `src/fatfloppy/core/format_definitions.py` for a full list of predefined profiles. The system can also attempt to work with formats detected via Greaseweazle's `ibm.scan` or by using custom parameters.

## Contributing

Contributions are welcome! Please feel free to:

*   Report bugs and suggest features by opening an issue.
*   Submit pull requests for improvements or bug fixes.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details (or assume MIT if no LICENSE file is present).

## Acknowledgements

*   This project heavily relies on the excellent **Greaseweazle** hardware and software by Keir Fraser. ([Greaseweazle GitHub](https://github.com/keirf/Greaseweazle))
*   Built using the **PyQt6** library for the graphical interface.
