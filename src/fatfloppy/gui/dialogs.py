# src/fatfloppy/gui/dialogs.py
import logging
from pathlib import Path
from typing import Any, Optional

from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

logger = logging.getLogger(__name__)


def _create_format_parameters_group() -> tuple[QGroupBox, dict[str, QWidget]]:
    """
    Creates the 'Format Parameters' group box with all its widgets.

    This helper function is used to avoid code duplication between dialogs
    that require the same set of format parameter input fields.

    Returns:
        A tuple containing the created QGroupBox and a dictionary mapping
        widget names to the widget instances.
    """
    group = QGroupBox("Format Parameters")
    layout = QFormLayout()
    widgets = {}

    widgets["cylinders_spin"] = QSpinBox()
    widgets["cylinders_spin"].setRange(1, 100)
    widgets["cylinders_spin"].setValue(80)
    layout.addRow("Cylinders:", widgets["cylinders_spin"])

    widgets["heads_spin"] = QSpinBox()
    widgets["heads_spin"].setRange(1, 2)
    widgets["heads_spin"].setValue(2)
    layout.addRow("Heads:", widgets["heads_spin"])

    widgets["sectors_spin"] = QSpinBox()
    widgets["sectors_spin"].setRange(1, 100)
    widgets["sectors_spin"].setValue(18)
    layout.addRow("Sectors/track:", widgets["sectors_spin"])

    widgets["bytes_per_sector_combo"] = QComboBox()
    for size_val in [128, 256, 512, 1024, 2048, 4096, 8192]:
        widgets["bytes_per_sector_combo"].addItem(f"{size_val} bytes", size_val)
    widgets["bytes_per_sector_combo"].setCurrentIndex(2)
    layout.addRow("Sector size:", widgets["bytes_per_sector_combo"])

    widgets["encoding_combo"] = QComboBox()
    widgets["encoding_combo"].addItem("MFM", "MFM")
    widgets["encoding_combo"].addItem("FM", "FM")
    layout.addRow("Encoding:", widgets["encoding_combo"])

    widgets["rate_combo"] = QComboBox()
    for rate_val in [125, 250, 300, 500, 1000]:
        widgets["rate_combo"].addItem(f"{rate_val} kbps", rate_val)
    widgets["rate_combo"].setCurrentIndex(3)
    layout.addRow("Data rate:", widgets["rate_combo"])

    widgets["rpm_combo"] = QComboBox()
    for rpm_val in [300, 360]:
        widgets["rpm_combo"].addItem(f"{rpm_val} RPM", rpm_val)
    layout.addRow("RPM:", widgets["rpm_combo"])

    widgets["interleave_spin"] = QSpinBox()
    widgets["interleave_spin"].setRange(1, 255)
    widgets["interleave_spin"].setValue(1)
    layout.addRow("Interleave:", widgets["interleave_spin"])

    widgets["id_start_spin"] = QSpinBox()
    widgets["id_start_spin"].setRange(0, 255)
    widgets["id_start_spin"].setValue(1)
    layout.addRow("Sector ID Start:", widgets["id_start_spin"])

    widgets["iam_present_check"] = QCheckBox("IAM Present")
    widgets["iam_present_check"].setChecked(True)
    layout.addRow(widgets["iam_present_check"])

    for gap_name, default_val in [
        ("gap1_bytes", 0),
        ("gap2_bytes", 0),
        ("gap3_bytes", 84),
    ]:
        widgets[f"{gap_name}_spin"] = QSpinBox()
        widgets[f"{gap_name}_spin"].setRange(0, 255)
        widgets[f"{gap_name}_spin"].setSpecialValueText("Default (0)")
        if default_val != 0:
            widgets[f"{gap_name}_spin"].setValue(default_val)
        layout.addRow(
            f"{gap_name.replace('_', ' ').capitalize()}:", widgets[f"{gap_name}_spin"]
        )

    for skew_name, default_val in [("cskew", 0), ("hskew", 0)]:
        widgets[f"{skew_name}_spin"] = QSpinBox()
        widgets[f"{skew_name}_spin"].setRange(0, 255)
        widgets[f"{skew_name}_spin"].setSpecialValueText("Default (0)")
        widgets[f"{skew_name}_spin"].setValue(default_val)
        layout.addRow(
            f"{skew_name.replace('skew', ' Skew').capitalize()}:",
            widgets[f"{skew_name}_spin"],
        )

    group.setLayout(layout)
    return group, widgets


def _set_default_parameters_for_size(size: str, widgets: dict[str, QWidget]) -> None:
    """
    Sets default format parameters on the widgets based on the drive size.

    Args:
        size: The drive size string ("3.5", "5.25", or "8").
        widgets: A dictionary of the format parameter widgets.
    """
    if size == "3.5":
        widgets["cylinders_spin"].setValue(80)
        widgets["heads_spin"].setValue(2)
        widgets["sectors_spin"].setValue(18)
        widgets["bytes_per_sector_combo"].setCurrentIndex(
            widgets["bytes_per_sector_combo"].findData(512)
        )
        widgets["encoding_combo"].setCurrentIndex(
            widgets["encoding_combo"].findData("MFM")
        )
        widgets["rate_combo"].setCurrentIndex(widgets["rate_combo"].findData(500))
        widgets["rpm_combo"].setCurrentIndex(widgets["rpm_combo"].findData(300))
        widgets["gap3_bytes_spin"].setValue(84)
    elif size == "5.25":
        widgets["cylinders_spin"].setValue(40)
        widgets["heads_spin"].setValue(2)
        widgets["sectors_spin"].setValue(9)
        widgets["bytes_per_sector_combo"].setCurrentIndex(
            widgets["bytes_per_sector_combo"].findData(512)
        )
        widgets["encoding_combo"].setCurrentIndex(
            widgets["encoding_combo"].findData("MFM")
        )
        widgets["rate_combo"].setCurrentIndex(widgets["rate_combo"].findData(250))
        widgets["rpm_combo"].setCurrentIndex(widgets["rpm_combo"].findData(300))
        widgets["gap3_bytes_spin"].setValue(50)
    elif size == "8":
        widgets["cylinders_spin"].setValue(77)
        widgets["heads_spin"].setValue(1)
        widgets["sectors_spin"].setValue(26)
        widgets["bytes_per_sector_combo"].setCurrentIndex(
            widgets["bytes_per_sector_combo"].findData(128)
        )
        widgets["encoding_combo"].setCurrentIndex(
            widgets["encoding_combo"].findData("FM")
        )
        widgets["rate_combo"].setCurrentIndex(widgets["rate_combo"].findData(250))
        widgets["rpm_combo"].setCurrentIndex(widgets["rpm_combo"].findData(360))
        widgets["gap3_bytes_spin"].setValue(26)

    widgets["interleave_spin"].setValue(1)
    widgets["id_start_spin"].setValue(1)
    widgets["iam_present_check"].setChecked(True)
    widgets["gap1_bytes_spin"].setValue(0)
    widgets["gap2_bytes_spin"].setValue(0)
    widgets["cskew_spin"].setValue(0)
    widgets["hskew_spin"].setValue(0)


def _populate_params_from_profile(profile: Any, widgets: dict[str, QWidget]) -> None:
    """
    Populates the format parameter widgets from a format profile object.

    Args:
        profile: The format profile object.
        widgets: A dictionary of the format parameter widgets.
    """
    try:
        if not (
            profile
            and profile.physical_format
            and profile.physical_format.track_formats
        ):
            return

        pf = profile.physical_format
        tf = pf.track_formats[0]

        widgets["cylinders_spin"].setValue(pf.cylinders)
        widgets["heads_spin"].setValue(pf.heads)
        widgets["sectors_spin"].setValue(tf.sectors_per_track)
        widgets["bytes_per_sector_combo"].setCurrentIndex(
            widgets["bytes_per_sector_combo"].findData(pf.bytes_per_sector)
        )
        widgets["encoding_combo"].setCurrentIndex(
            widgets["encoding_combo"].findData(tf.encoding)
        )
        widgets["rate_combo"].setCurrentIndex(widgets["rate_combo"].findData(tf.rate))
        widgets["rpm_combo"].setCurrentIndex(widgets["rpm_combo"].findData(pf.rpm))
        widgets["interleave_spin"].setValue(tf.interleave)

        val_id_start = getattr(tf, "id_start", 1)
        widgets["id_start_spin"].setValue(
            val_id_start if val_id_start is not None else 1
        )
        widgets["iam_present_check"].setChecked(getattr(tf, "iam_present", True))

        for attr_name in ["gap1_bytes", "gap2_bytes", "gap3_bytes", "cskew", "hskew"]:
            val = getattr(tf, attr_name, None)
            widgets[f"{attr_name}_spin"].setValue(val if val is not None else 0)

    except Exception as e:
        logger.error(f"Error setting format parameters from profile: {e}")


class DriveSelectionDialog(QDialog):
    """A dialog for selecting a floppy drive and its format parameters."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        """
        Initializes the DriveSelectionDialog.

        Args:
            parent: The parent widget, if any.
        """
        super().__init__(parent)
        self.setWindowTitle("Floppy Drive Selection")
        self.resize(400, 650)

        self.format_params_group: QGroupBox
        self.format_widgets: dict[str, QWidget]
        self._init_ui()

    def get_selection(self) -> tuple[str, str, Optional[dict[str, Any]]]:
        """
        Gets the user's drive and format selection from the dialog.

        Returns:
            A tuple containing the selected drive, size, and format info.
            The format info is a dictionary for custom formats, a profile name
            for predefined formats, or None for auto-detect.
        """
        drive = self.drive_combo.currentData()
        size = self.size_combo.currentData()
        format_key = self.format_combo.currentData()
        format_info: Optional[dict[str, Any]] = None

        if format_key == "custom" and self.format_params_group.isEnabled():
            format_info = {
                "cylinders": self.format_widgets["cylinders_spin"].value(),
                "heads": self.format_widgets["heads_spin"].value(),
                "sectors_per_track": self.format_widgets["sectors_spin"].value(),
                "bytes_per_sector": (
                    self.format_widgets["bytes_per_sector_combo"].currentData()
                ),
                "encoding": self.format_widgets["encoding_combo"].currentData(),
                "rate": self.format_widgets["rate_combo"].currentData(),
                "rpm": self.format_widgets["rpm_combo"].currentData(),
                "interleave": self.format_widgets["interleave_spin"].value(),
                "id_start": self.format_widgets["id_start_spin"].value(),
                "iam_present": (self.format_widgets["iam_present_check"].isChecked()),
            }
            for key in ["gap1_bytes", "gap2_bytes", "gap3_bytes", "cskew", "hskew"]:
                value = self.format_widgets[f"{key}_spin"].value()
                format_info[key] = value if value != 0 else None

        elif format_key is not None and format_key != "custom":
            format_info = {"profile_name": format_key}

        return drive, size, format_info

    def _create_buttons(self, layout: QVBoxLayout) -> None:
        """Creates the OK and Cancel buttons."""
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        layout.addWidget(buttons)

    def _create_drive_selection_widgets(self, layout: QVBoxLayout) -> None:
        """Creates widgets for drive selection."""
        drive_layout = QHBoxLayout()
        drive_layout.addWidget(QLabel("Drive:"))
        self.drive_combo = QComboBox()
        self._update_drive_options(True)
        drive_layout.addWidget(self.drive_combo)
        drive_layout.addStretch()
        layout.addLayout(drive_layout)

    def _create_drive_type_group(self, layout: QVBoxLayout) -> None:
        """Creates the group for selecting the drive interface type."""
        drive_type_group = QGroupBox("Drive Interface Type")
        drive_type_layout = QHBoxLayout()
        self.ibm_radio = QRadioButton("IBM PC (A/B)")
        self.ibm_radio.setChecked(True)
        self.ibm_radio.toggled.connect(self._update_drive_options)
        self.shugart_radio = QRadioButton("Shugart (0-3)")
        drive_type_layout.addWidget(self.ibm_radio)
        drive_type_layout.addWidget(self.shugart_radio)
        drive_type_group.setLayout(drive_type_layout)
        layout.addWidget(drive_type_group)

    def _create_format_widgets(self, layout: QVBoxLayout) -> None:
        """Creates widgets for format selection."""
        format_layout = QHBoxLayout()
        format_layout.addWidget(QLabel("Format (optional):"))
        self.format_combo = QComboBox()
        self.format_combo.addItem("Auto-detect", None)
        self.format_combo.addItem("Custom...", "custom")
        format_layout.addWidget(self.format_combo)
        format_layout.addStretch()
        layout.addLayout(format_layout)

    def _create_size_widgets(self, layout: QVBoxLayout) -> None:
        """Creates widgets for drive size selection."""
        size_layout = QHBoxLayout()
        size_layout.addWidget(QLabel("Drive Size:"))
        self.size_combo = QComboBox()
        self.size_combo.addItem('3.5"', "3.5")
        self.size_combo.addItem('5.25"', "5.25")
        self.size_combo.addItem('8"', "8")
        self.size_combo.currentIndexChanged.connect(self._update_format_list)
        size_layout.addWidget(self.size_combo)
        size_layout.addStretch()
        layout.addLayout(size_layout)

    def _init_ui(self) -> None:
        """Initializes and lays out the UI components."""
        main_layout = QVBoxLayout()

        self._create_drive_type_group(main_layout)
        self._create_drive_selection_widgets(main_layout)
        self._create_size_widgets(main_layout)
        self._create_format_widgets(main_layout)

        self.format_params_group, self.format_widgets = (
            _create_format_parameters_group()
        )
        self.format_params_group.setEnabled(False)
        main_layout.addWidget(self.format_params_group)

        self._update_format_list()
        self.format_combo.currentIndexChanged.connect(self._on_format_changed)

        self._create_buttons(main_layout)

        main_layout.addStretch()
        self.setLayout(main_layout)

        if not self._is_greaseweazle_connected():
            self.ok_button.setEnabled(False)
            no_device_label = QLabel("No Greaseweazle device found")
            main_layout.addWidget(no_device_label)

    def _is_greaseweazle_connected(self) -> bool:
        """
        Checks if a Greaseweazle device is connected.

        Returns:
            True if a device is found, False otherwise.
        """
        try:
            from greaseweazle.tools.util import usb_open

            usb = usb_open(None)
            if usb and hasattr(usb, "ser") and usb.ser:
                usb.ser.close()
            return True
        except Exception as e:
            logger.warning(f"Could not check for Greaseweazle device: {e}")
            return False

    def _on_format_changed(self) -> None:
        """
        Handles changes in the format selection combo box.

        Enables/disables and populates the format parameters group.
        """
        format_key = self.format_combo.currentData()
        is_custom = format_key == "custom"
        self.format_params_group.setEnabled(is_custom)

        if is_custom:
            _set_default_parameters_for_size(
                self.size_combo.currentData(), self.format_widgets
            )
        elif format_key is not None:
            try:
                from ..core.filesystem_registry import FilesystemRegistry

                all_formats = FilesystemRegistry.get_all_formats()
                profile = all_formats.get(format_key)
                _populate_params_from_profile(profile, self.format_widgets)
            except Exception as e:
                logger.error(f"Error loading format definition for '{format_key}': {e}")

    def _update_drive_options(self, _checked: Optional[bool] = None) -> None:
        """
        Updates the available drive options based on the selected interface type.

        Args:
            _checked: Unused parameter from signal.
        """
        self.drive_combo.clear()
        if self.ibm_radio.isChecked():
            self.drive_combo.addItem("A", "A")
            self.drive_combo.addItem("B", "B")
        else:
            for i in range(4):
                self.drive_combo.addItem(str(i), str(i))

    def _update_format_list(self) -> None:
        """
        Updates the list of available formats based on the selected drive size.
        """
        current_data = self.format_combo.currentData()
        self.format_combo.clear()
        self.format_combo.addItem("Auto-detect", None)
        self.format_combo.addItem("Custom...", "custom")
        drive_size = self.size_combo.currentData()

        try:
            from ..core.filesystem_registry import FilesystemRegistry

            all_formats = FilesystemRegistry.get_all_formats()
            for name, profile in all_formats.items():
                if f'{drive_size}"' in profile.description:
                    self.format_combo.addItem(f"{profile.description} ({name})", name)
        except Exception as e:
            logger.error(f"Error loading format definitions: {e}")

        if current_data is not None:
            idx = self.format_combo.findData(current_data)
            if idx != -1:
                self.format_combo.setCurrentIndex(idx)

        self._on_format_changed()


class CreateImageDialog(QDialog):
    """A dialog for creating a new blank disk image file with specified parameters."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        """
        Initializes the CreateImageDialog.

        Args:
            parent: The parent widget, if any.
        """
        super().__init__(parent)
        self.setWindowTitle("Create Disk Image")
        self.resize(400, 650)

        self.format_params_group: QGroupBox
        self.format_widgets: dict[str, QWidget]
        self._init_ui()

    def get_selection(self) -> tuple[str, dict[str, Any], str, str]:
        """
        Gets the user's selections for creating a new disk image.

        Returns:
            A tuple containing the full file path, format information dictionary,
            volume label, and output format type.

        Raises:
            ValueError: If the directory or file name is not provided, or if
                        the specified path is a directory.
        """
        directory = self.directory_input.text().strip()
        file_name = self.file_name_input.text().strip()
        if not directory or not file_name:
            raise ValueError("Both directory and file name must be provided.")

        file_path = Path(directory) / file_name
        if file_path.is_dir():
            raise ValueError(
                f"The path '{file_path}' is a directory. Please specify a "
                f"valid file name."
            )

        volume_label = self.volume_label_input.text().strip().upper() or "NO NAME"
        output_format = self.extension_combo.currentData()
        format_info: dict[str, Any] = {}

        if self.advanced_checkbox.isChecked():
            format_info = {
                "cylinders": self.format_widgets["cylinders_spin"].value(),
                "heads": self.format_widgets["heads_spin"].value(),
                "sectors_per_track": self.format_widgets["sectors_spin"].value(),
                "bytes_per_sector": (
                    self.format_widgets["bytes_per_sector_combo"].currentData()
                ),
                "encoding": self.format_widgets["encoding_combo"].currentData(),
                "rate": self.format_widgets["rate_combo"].currentData(),
                "rpm": self.format_widgets["rpm_combo"].currentData(),
                "interleave": self.format_widgets["interleave_spin"].value(),
                "id_start": self.format_widgets["id_start_spin"].value(),
                "iam_present": (self.format_widgets["iam_present_check"].isChecked()),
            }
            for key in ["gap1_bytes", "gap2_bytes", "gap3_bytes", "cskew", "hskew"]:
                value = self.format_widgets[f"{key}_spin"].value()
                format_info[key] = value if value != 0 else None
        else:
            profile_name = self.format_combo.currentData()
            if not profile_name:
                raise ValueError(
                    "A format profile must be selected if not using advanced settings."
                )
            format_info = {"profile_name": profile_name}

        return str(file_path), format_info, volume_label, output_format

    def _create_advanced_settings_widgets(self, layout: QVBoxLayout) -> None:
        """Creates the 'Advanced Settings' checkbox."""
        self.advanced_checkbox = QCheckBox("Advanced Settings (Custom Geometry)")
        self.advanced_checkbox.stateChanged.connect(self._toggle_advanced_settings)
        layout.addWidget(self.advanced_checkbox)

    def _create_buttons(self, layout: QVBoxLayout) -> None:
        """Creates the OK and Cancel buttons."""
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addStretch()
        layout.addWidget(buttons)

    def _create_file_path_widgets(self, layout: QVBoxLayout) -> None:
        """Creates widgets for selecting the output directory and file name."""
        directory_layout = QHBoxLayout()
        directory_layout.addWidget(QLabel("Directory:"))
        self.directory_input = QLineEdit()
        self.directory_input.setPlaceholderText("Select or enter directory")
        directory_layout.addWidget(self.directory_input)
        self.select_directory_button = QPushButton("...")
        self.select_directory_button.clicked.connect(self._select_directory)
        directory_layout.addWidget(self.select_directory_button)
        layout.addLayout(directory_layout)

        file_name_layout = QHBoxLayout()
        file_name_layout.addWidget(QLabel("File Name:"))
        self.file_name_input = QLineEdit()
        self.file_name_input.setPlaceholderText("Enter file name")
        file_name_layout.addWidget(self.file_name_input)
        self.extension_combo = QComboBox()
        self.extension_combo.addItem(".img", "IMG")
        self.extension_combo.addItem(".ima", "IMG")
        self.extension_combo.addItem(".h8d", "IMG")
        self.extension_combo.addItem(".imd", "IMD")
        self.extension_combo.addItem(".h17", "H17")
        self.extension_combo.currentIndexChanged.connect(self._update_file_name)
        file_name_layout.addWidget(self.extension_combo)
        layout.addLayout(file_name_layout)

    def _create_format_widgets(self, layout: QVBoxLayout) -> None:
        """Creates widgets for format selection."""
        format_layout = QHBoxLayout()
        format_layout.addWidget(QLabel("Format:"))
        self.format_combo = QComboBox()
        format_layout.addWidget(self.format_combo)
        format_layout.addStretch()
        layout.addLayout(format_layout)

    def _create_size_widgets(self, layout: QVBoxLayout) -> None:
        """Creates widgets for disk size selection."""
        size_layout = QHBoxLayout()
        size_layout.addWidget(QLabel("Disk Size:"))
        self.size_combo = QComboBox()
        self.size_combo.addItem('3.5"', "3.5")
        self.size_combo.addItem('5.25"', "5.25")
        self.size_combo.addItem('8"', "8")
        self.size_combo.currentIndexChanged.connect(self._update_format_list)
        size_layout.addWidget(self.size_combo)
        size_layout.addStretch()
        layout.addLayout(size_layout)

    def _create_volume_label_widgets(self, layout: QVBoxLayout) -> None:
        """Creates the widget for entering a volume label."""
        volume_label_layout = QHBoxLayout()
        volume_label_layout.addWidget(QLabel("Volume Label:"))
        self.volume_label_input = QLineEdit()
        self.volume_label_input.setMaxLength(11)
        volume_label_layout.addWidget(self.volume_label_input)
        volume_label_layout.addStretch()
        layout.addLayout(volume_label_layout)

    def _init_ui(self) -> None:
        """Initializes and lays out the UI components."""
        main_layout = QVBoxLayout()

        self._create_size_widgets(main_layout)
        self._create_format_widgets(main_layout)
        self._create_file_path_widgets(main_layout)
        self._create_advanced_settings_widgets(main_layout)

        self.format_params_group, self.format_widgets = (
            _create_format_parameters_group()
        )
        main_layout.addWidget(self.format_params_group)

        self._create_volume_label_widgets(main_layout)

        self._update_format_list()
        self.format_combo.currentIndexChanged.connect(self._on_format_changed)

        self._create_buttons(main_layout)
        self.setLayout(main_layout)
        self._toggle_advanced_settings()

    def _on_format_changed(self) -> None:
        """
        Handles changes in the format selection combo box.

        Populates the format parameter fields from the selected profile.
        """
        if self.advanced_checkbox.isChecked():
            _set_default_parameters_for_size(
                self.size_combo.currentData(), self.format_widgets
            )
            return

        format_key = self.format_combo.currentData()
        if format_key:
            try:
                from ..core.filesystem_registry import FilesystemRegistry

                all_formats = FilesystemRegistry.get_all_formats()
                profile = all_formats.get(format_key)
                _populate_params_from_profile(profile, self.format_widgets)
                self._update_file_name()
            except Exception as e:
                logger.error(f"Error processing format change: {e}")
        else:
            _set_default_parameters_for_size(
                self.size_combo.currentData(), self.format_widgets
            )

    def _select_directory(self) -> None:
        """Opens a dialog to select a directory."""
        start_dir = self.directory_input.text() or str(Path.home())
        directory = QFileDialog.getExistingDirectory(
            self, "Select Directory", start_dir
        )
        if directory:
            self.directory_input.setText(directory)

    def _toggle_advanced_settings(self) -> None:
        """
        Shows or hides the advanced format parameters group.

        Based on the state of the 'Advanced Settings' checkbox.
        """
        is_checked = self.advanced_checkbox.isChecked()
        self.format_params_group.setEnabled(is_checked)
        if is_checked:
            _set_default_parameters_for_size(
                self.size_combo.currentData(), self.format_widgets
            )
        else:
            self._on_format_changed()

    def _update_file_name(self) -> None:
        """
        Updates the file name with the correct extension.

        Uses a default name based on the selected format if no name is entered.
        """
        current_text = self.file_name_input.text().strip()
        path = Path(current_text)
        base_name = path.stem

        if not base_name and self.format_combo.currentData():
            base_name = (
                self.format_combo.currentData().replace('"', "").replace(" ", "_")
            )

        extension = self.extension_combo.currentText()
        if base_name:
            self.file_name_input.setText(f"{base_name}{extension}")

    def _update_format_list(self) -> None:
        """
        Updates the list of available formats based on the selected disk size.
        """
        current_data = self.format_combo.currentData()
        self.format_combo.clear()
        drive_size = self.size_combo.currentData()

        try:
            from ..core.filesystem_registry import FilesystemRegistry

            all_formats = FilesystemRegistry.get_all_formats()
            for name, profile in all_formats.items():
                if f'{drive_size}"' in profile.description:
                    self.format_combo.addItem(f"{profile.description} ({name})", name)

            if self.format_combo.count() > 0:
                idx_to_select = 0
                if current_data is not None:
                    found_idx = self.format_combo.findData(current_data)
                    if found_idx != -1:
                        idx_to_select = found_idx
                self.format_combo.setCurrentIndex(idx_to_select)
        except Exception as e:
            logger.error(f"Error loading format definitions: {e}")

        self._on_format_changed()
