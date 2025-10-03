# src/fatfloppy/gui/managers/disk_manager.py
"""
Manages disk-related operations including opening, creating, and visualizing disks.
"""

import copy
import logging
import os
from typing import Any, Dict, List, Optional

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QMessageBox, QMainWindow

from ...core.controller import DiskController
from ...core.format_definitions import FLOPPY_FORMATS


class DiskManager(QObject):
    """
    Handles all disk-related operations for the GUI application.
    """

    # Signals
    disk_opened = pyqtSignal(str)  # disk source path/name
    disk_closed = pyqtSignal()
    status_message = pyqtSignal(str)
    error_occurred = pyqtSignal(str, str)  # title, message
    geometry_updated = pyqtSignal()
    filesystem_updated = pyqtSignal()
    map_update_needed = pyqtSignal()

    def __init__(self, parent: 'QMainWindow') -> None:
        """
        Initialize the disk manager.

        Args:
            parent: The main window that owns this manager.
        """
        super().__init__(parent)
        self.parent = parent
        self.logger: logging.Logger = parent.logger

        # Disk visualization state
        self.current_head: int = 0
        self.busy_units: List[Any] = []
        self.free_space: int = 0
        self.total_space: int = 0
        self.selected_file_path: Optional[str] = None
        self.selected_file_units: List[int] = []

    # --- Public Methods ---

    def clear_file_selection(self) -> None:
        """Clears the currently selected file highlighting on disk map."""
        self.selected_file_path = None
        self.selected_file_units = []
        self.map_update_needed.emit()

    def create_disk_image(self) -> None:
        """Opens dialog to create a new disk image and formats it."""
        from ..dialogs import CreateImageDialog

        self.logger.debug("Attempting to create a new disk image.")
        dialog = CreateImageDialog(self.parent)
        if not dialog.exec():
            self.logger.debug("Create Disk Image dialog cancelled.")
            return

        try:
            file_path, format_info, volume_label, output_format = dialog.get_selection()
            self.logger.info(
                f"Creating image: {file_path}, format_info: {format_info}, "
                f"volume_label: {volume_label}, output_format: {output_format}"
            )
        except ValueError as e:
            QMessageBox.warning(self.parent, "Warning", str(e))
            self.logger.warning(f"Invalid selection for disk image creation: {e}")
            return

        try:
            profile = self._get_format_profile(format_info)
            if not profile:
                self.error_occurred.emit("Error", "Failed to determine format profile for creation.")
                self.logger.error("Failed to get format profile for disk image creation.")
                return
            if not profile.physical_format or not profile.filesystem_config:
                self.error_occurred.emit("Error", "Selected format profile is incomplete for creation.")
                self.logger.error(f"Incomplete format profile for creation: {profile.name}")
                return

            controller = DiskController()
            self.status_message.emit(f"Creating and formatting disk image: {os.path.basename(file_path)}...")

            if controller.create_and_format_image(file_path, profile, volume_label, output_format):
                self.logger.info(f"Successfully created and formatted disk image: {file_path}")
                self.parent.controller = controller
                self._finalize_disk_open(file_path, is_image=True)
            else:
                self.error_occurred.emit("Error", "Failed to create and format disk image.")
                self.logger.error(f"Failed to create/format disk image: {file_path}")
        except Exception as e:
            self.error_occurred.emit("Error", f"An unexpected error occurred during image creation: {str(e)}")
            self.logger.exception("Unexpected error during disk image creation.")

    def get_disk_map_data(self) -> Dict[str, Any]:
        """
        Gets current data needed for disk map rendering.

        Returns:
            Dictionary containing disk map rendering data.
        """
        return {
            'controller': self.parent.controller,
            'current_head': self.current_head,
            'busy_units': self.busy_units,
            'free_space': self.free_space,
            'total_space': self.total_space,
            'selected_file_units': self.selected_file_units,
            'selected_file_path': self.selected_file_path
        }

    def open_disk_image_by_path(self, file_path: str) -> None:
        """
        Opens a disk image file at the specified path.

        Args:
            file_path: The path to the disk image file to open.
        """
        if not file_path or not os.path.exists(file_path):
            self.logger.warning(f"File path does not exist: {file_path}")
            return

        _, ext = os.path.splitext(file_path)
        ext_lower = ext.lower()

        # Use parent's extension map
        disk_type = self.parent.extension_to_driver_map.get(ext_lower, "IMG")
        self.logger.info(f"File extension '{ext_lower}' mapped to driver type '{disk_type}'.")

        try:
            self.disk_closed.emit()  # Reset UI first
            controller = DiskController()
            self.status_message.emit(f"Opening {disk_type} disk: {os.path.basename(file_path)}...")

            if controller.open_disk(file_path, disk_type):
                self.logger.info(f"Successfully opened disk image: {file_path} (Type: {disk_type})")
                self.parent.controller = controller
                self._finalize_disk_open(file_path, is_image=True)
            else:
                self.disk_closed.emit()
                self.error_occurred.emit(
                    "Error",
                    f"Failed to open {disk_type} disk image. Check logs for details."
                )
                self.logger.error(f"Failed to open disk image: {file_path}")
        except ValueError as e:
            self.disk_closed.emit()
            self.error_occurred.emit("Error", f"Failed to parse or load image file: {str(e)}")
            self.logger.error(f"ValueError opening disk image {file_path}: {e}")
        except Exception as e:
            self.disk_closed.emit()
            self.error_occurred.emit("Error", f"Failed to open disk image: {str(e)}")
            self.logger.exception(f"Unexpected error opening disk image {file_path}.")

    def open_disk_image_file(self) -> None:
        """Opens a disk image file selected by the user."""
        from PyQt6.QtWidgets import QFileDialog

        self.logger.debug("Attempting to open a disk image file.")
        file_path, _ = QFileDialog.getOpenFileName(
            self.parent, "Open Disk Image", "", self.parent.file_dialog_filter
        )
        if not file_path:
            self.logger.debug("Open Disk Image file dialog cancelled.")
            return

        self.open_disk_image_by_path(file_path)

    def open_physical_floppy(self) -> None:
        """Opens a physical floppy drive using Greaseweazle."""
        from ..dialogs import DriveSelectionDialog

        self.logger.debug("Attempting to open a physical floppy.")

        try:
            dialog = DriveSelectionDialog(self.parent)
            if not dialog.exec():
                self.logger.debug("Drive Selection dialog cancelled.")
                return

            drive_letter, drive_size, format_info = dialog.get_selection()
            self.logger.info(
                f"Opening physical floppy: Drive {drive_letter}, Size {drive_size}, Format {format_info}"
            )

            def open_operation(progress_callback=None):
                if progress_callback:
                    progress_callback(0, 100, "Initializing device...")

                controller = DiskController()

                if progress_callback:
                    progress_callback(30, 100, "Opening drive...")

                success = controller.open_disk(
                    None, "physical",
                    drive_letter=drive_letter,
                    drive_size=drive_size,
                    format_info=format_info
                )

                if not success:
                    raise Exception("Failed to open physical floppy")

                if progress_callback:
                    progress_callback(100, 100, "Complete")

                return controller

            def on_success(controller):
                self.parent.controller = controller
                self._finalize_disk_open(
                    f"Drive {drive_letter} ({drive_size}\")",
                    is_image=False,
                    drive_letter=drive_letter,
                    drive_size=drive_size,
                    format_info=format_info
                )

            def on_error(exception):
                self.disk_closed.emit()
                self.error_occurred.emit("Error", f"Failed to open physical floppy: {str(exception)}")
                self.logger.exception("Error opening physical floppy.")

            self.parent._run_threaded_operation(
                open_operation,
                "Opening Physical Floppy",
                on_success=on_success,
                on_error=on_error,
                cancelable=False
            )

        except Exception as e:
            self.disk_closed.emit()
            self.error_occurred.emit("Error", f"Failed to open physical floppy: {str(e)}")
            self.logger.exception("Unexpected error opening physical floppy.")

    def toggle_head(self) -> None:
        """Switches between disk heads for visualization if the disk has multiple heads."""
        if self.parent.controller and self.parent.controller.physical_format.heads > 1:
            self.current_head = 1 - self.current_head
            self.logger.info(f"Switched to head: {self.current_head}")
            self.map_update_needed.emit()
        else:
            QMessageBox.information(
                self.parent, "Info",
                "Head switching is not available for this disk (single-sided)."
            )
            self.logger.info("Attempted to switch head on a single-sided disk.")

    def update_file_selection(self, file_path: Optional[str]) -> None:
        """
        Updates which file is highlighted on the disk map.

        Args:
            file_path: Path to the file to highlight, or None to clear.
        """
        self.selected_file_path = file_path
        self.selected_file_units = []

        if file_path and self.parent.controller:
            units = self.parent.controller.get_file_allocation_units(file_path)
            if units:
                self.selected_file_units = units
                self.logger.debug(
                    f"File '{os.path.basename(file_path)}' uses {len(units)} units: "
                    f"{units[:10]}{'...' if len(units) > 10 else ''}"
                )
            else:
                self.logger.debug(f"File '{os.path.basename(file_path)}' has no allocation units")

        self.map_update_needed.emit()

    def update_geometry_info(self) -> None:
        """Updates the physical geometry information display."""
        if not self.parent.controller or not self.parent.controller.physical_format:
            self.parent.physical_format_info.setText("Disk geometry not available")
            return

        geometry = self.parent.controller.physical_format
        total_sectors = geometry.total_sectors
        total_bytes = total_sectors * geometry.bytes_per_sector

        imd_comment = ""
        if hasattr(self.parent.controller.driver, 'comment') and self.parent.controller.driver.comment:
            comment = self.parent.controller.driver.comment
            display_comment = (comment[:60] + '...') if len(comment) > 63 else comment
            imd_comment = f"IMD Comment: {display_comment}\n"

        encoding_text, rate_text, spt_text = "N/A", "N/A", "N/A"
        if geometry.track_formats:
            encodings = {tf.encoding for tf in geometry.track_formats}
            rates = {tf.rate for tf in geometry.track_formats}
            spts = {tf.sectors_per_track for tf in geometry.track_formats}

            encoding_text = list(encodings)[0] if len(encodings) == 1 else "variable"
            rate_text = f"{list(rates)[0]} kbps" if len(rates) == 1 else "variable"
            spt_text = str(list(spts)[0]) if len(spts) == 1 else "variable"

        info = (
            f"{imd_comment}"
            f"Encoding: {encoding_text}\n"
            f"Data Rate: {rate_text}\n"
            f"Rotation Speed: {geometry.rpm} RPM\n"
            f"Bytes per Sector: {geometry.bytes_per_sector}\n"
            f"Sectors per Track: {spt_text}\n"
            f"Number of Heads: {geometry.heads}\n"
            f"Number of Cylinders: {geometry.cylinders}\n"
            f"Total Sectors: {total_sectors}\n"
            f"Total Size: {total_bytes / 1024:.1f} KB"
        )
        self.parent.physical_format_info.setText(info)
        self.logger.debug("Physical geometry info updated.")

    def update_filesystem_info(self) -> None:
        """Updates the filesystem information display."""
        if not self.parent.controller:
            self.parent.filesystem_info.setText("No disk loaded")
            return

        if self.parent.controller.filesystem:
            fs_info_dict = self.parent.controller.filesystem.get_display_info()
            fs_info_lines: List[str] = []

            if "Filesystem Type" in fs_info_dict:
                fs_info_lines.append(f"Filesystem Type: {fs_info_dict.pop('Filesystem Type')}")
            if "Volume Label" in fs_info_dict:
                fs_info_lines.append(f"Volume Label: {fs_info_dict.pop('Volume Label')}")

            for key, value in fs_info_dict.items():
                fs_info_lines.append(f"{key}: {value}")

            fs_details = "\n".join(fs_info_lines)

            space_info = self.parent.controller.get_free_space()
            if space_info:
                free_bytes, total_bytes = space_info
                free_kb = free_bytes / 1024
                total_kb = total_bytes / 1024
                percent_free = (free_bytes / total_bytes * 100) if total_bytes > 0 else 0
                space_str = f"Free Space: {free_kb:.1f} KB / {total_kb:.1f} KB ({percent_free:.1f}%)"
            else:
                space_str = "Free Space: N/A"
                self.logger.warning("Could not retrieve filesystem free space information.")

            info = f"{fs_details}\n{space_str}"
            self.parent.filesystem_info.setText(info)
            self.logger.debug("Filesystem info updated.")
        else:
            self.parent.filesystem_info.setText("No filesystem detected")
            self.logger.debug("No filesystem detected, display updated accordingly.")

    def update_space_info(self) -> None:
        """Retrieves allocated units and updates free/total space information."""
        if not self.parent.controller:
            self.busy_units = []
            self.free_space = 0
            self.total_space = 0
            self.logger.debug("No controller, busy units and space reset.")
            return

        try:
            self.busy_units = self.parent.controller.get_allocated_units()
            allocation_unit_size_bytes = 512

            if self.parent.controller.filesystem:
                if self.parent.controller.filesystem.allocation_unit_size > 0:
                    allocation_unit_size_bytes = self.parent.controller.filesystem.allocation_unit_size
                else:
                    self.logger.warning("Filesystem reported zero allocation unit size. Using default 512 bytes.")

                space_info = self.parent.controller.get_free_space()
                if space_info:
                    free_bytes, total_bytes = space_info
                    if allocation_unit_size_bytes > 0:
                        self.free_space = free_bytes // allocation_unit_size_bytes
                        self.total_space = total_bytes // allocation_unit_size_bytes
                    else:
                        self.logger.warning("Cannot calculate space in units (allocation unit size is zero).")
                        self.free_space = 0
                        self.total_space = 0
                else:
                    self.logger.warning("Could not retrieve free space information from filesystem.")
                    self.free_space = 0
                    if allocation_unit_size_bytes > 0 and self.parent.controller.physical_format:
                        total_data_bytes_geom = self.parent.controller.physical_format.total_bytes
                        self.total_space = total_data_bytes_geom // allocation_unit_size_bytes
                        self.free_space = max(0, self.total_space - len(self.busy_units))
                    else:
                        self.total_space = len(self.busy_units)
                        self.free_space = 0
            else:
                self.logger.warning("No filesystem detected, cannot calculate accurate free/total space.")
                self.busy_units = []
                self.free_space = 0
                self.total_space = 0

            self.logger.debug(
                f"Busy units: {len(self.busy_units)}, Free units: {self.free_space}, "
                f"Total units: {self.total_space}"
            )

        except Exception as e:
            self.logger.error(f"Error getting busy units/space info: {e}", exc_info=True)
            self.busy_units = []
            self.free_space = 0
            self.total_space = 0

    # --- Private Methods ---

    def _finalize_disk_open(
        self,
        source_identifier: str,
        is_image: bool,
        drive_letter: Optional[str] = None,
        drive_size: Optional[str] = None,
        format_info: Optional[Dict[str, Any]] = None
    ) -> None:
        """
        Completes the disk opening process and updates UI.

        Args:
            source_identifier: Path or description of disk source.
            is_image: True if disk image file, False if physical.
            drive_letter: Drive letter for physical floppies.
            drive_size: Drive size for physical floppies.
            format_info: Format information for physical floppies.
        """
        self.disk_opened.emit(source_identifier)

        # Build status message
        if is_image:
            self.status_message.emit(f"Loaded: {os.path.basename(source_identifier)}")
        else:
            format_name, _ = self.parent.controller.detect_format()
            format_text = f" using {format_name}" if format_name else ""
            if format_info and not format_info.get("profile_name"):
                format_text += (
                    f" (Custom format: {format_info.get('cylinders')}x"
                    f"{format_info.get('heads')}x{format_info.get('sectors_per_track')})"
                )
            self.status_message.emit(
                f"Loaded physical floppy{format_text} (Drive: {drive_letter}, Size: {drive_size}\")"
            )
            self.logger.info(f"Successfully opened physical floppy on drive {drive_letter}.")

    def _get_format_profile(self, format_info: Dict[str, Any]) -> Optional[Any]:
        """
        Retrieves a format profile from the provided format_info.

        Args:
            format_info: Dictionary containing format details.

        Returns:
            A format profile object or None if not found/created.
        """
        if "profile_name" in format_info:
            profile_name = format_info["profile_name"]
            original_profile = FLOPPY_FORMATS.get(profile_name)
            if original_profile:
                self.logger.debug(f"Retrieved predefined format profile: {profile_name}")
                return copy.deepcopy(original_profile)
            else:
                QMessageBox.critical(
                    self.parent, "Error",
                    f"Predefined format '{profile_name}' not found."
                )
                self.logger.error(f"Predefined format '{profile_name}' not found.")
                return None
        else:
            controller = self.parent.controller or DiskController()
            self.logger.debug(f"Creating custom format profile with info: {format_info}")
            return controller.create_custom_profile(format_info)
