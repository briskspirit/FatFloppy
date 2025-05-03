# src/fatfloppy/gui/dialogs.py
import os
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QComboBox,
                             QDialogButtonBox, QGroupBox, QRadioButton, QFormLayout,
                             QSpinBox, QLineEdit, QCheckBox, QPushButton, QFileDialog)


class DriveSelectionDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Floppy Drive Selection")
        self.resize(400, 500)

        main_layout = QVBoxLayout()

        # Drive Type Selection
        drive_type_group = QGroupBox("Drive Interface Type")
        drive_type_layout = QHBoxLayout()

        self.ibm_radio = QRadioButton("IBM PC (A/B)")
        self.ibm_radio.setChecked(True)  # Default selection
        self.ibm_radio.toggled.connect(self.update_drive_options)
        self.shugart_radio = QRadioButton("Shugart (0-3)")

        drive_type_layout.addWidget(self.ibm_radio)
        drive_type_layout.addWidget(self.shugart_radio)
        drive_type_group.setLayout(drive_type_layout)

        main_layout.addWidget(drive_type_group)

        # Drive Selection
        drive_layout = QHBoxLayout()
        drive_layout.addWidget(QLabel("Drive:"))
        self.drive_combo = QComboBox()
        self.drive_combo.addItem("A", "A")
        self.drive_combo.addItem("B", "B")
        drive_layout.addWidget(self.drive_combo)
        drive_layout.addStretch()

        main_layout.addLayout(drive_layout)

        # Drive Size Selection
        size_layout = QHBoxLayout()
        size_layout.addWidget(QLabel("Drive Size:"))
        self.size_combo = QComboBox()
        self.size_combo.addItem("3.5\"", "3.5")
        self.size_combo.addItem("5.25\"", "5.25")
        self.size_combo.addItem("8\"", "8")
        self.size_combo.currentIndexChanged.connect(self.update_format_list)
        size_layout.addWidget(self.size_combo)
        size_layout.addStretch()

        main_layout.addLayout(size_layout)

        # Format Selection
        format_layout = QHBoxLayout()
        format_layout.addWidget(QLabel("Format (optional):"))
        self.format_combo = QComboBox()
        self.format_combo.addItem("Auto-detect", None)
        self.format_combo.addItem("Custom...", "custom")
        format_layout.addWidget(self.format_combo)
        format_layout.addStretch()

        main_layout.addLayout(format_layout)

        # Format Parameters Group
        self.format_params_group = QGroupBox("Format Parameters")
        self.format_params_group.setEnabled(False)
        format_params_layout = QFormLayout()

        # Cylinders
        self.cylinders_spin = QSpinBox()
        self.cylinders_spin.setRange(1, 100)
        self.cylinders_spin.setValue(80)
        format_params_layout.addRow("Cylinders:", self.cylinders_spin)

        # Heads
        self.heads_spin = QSpinBox()
        self.heads_spin.setRange(1, 2)
        self.heads_spin.setValue(2)
        format_params_layout.addRow("Heads:", self.heads_spin)

        # Sectors per track
        self.sectors_spin = QSpinBox()
        self.sectors_spin.setRange(1, 100)
        self.sectors_spin.setValue(18)
        format_params_layout.addRow("Sectors per track:", self.sectors_spin)

        # Sector size
        self.bytes_per_sector_combo = QComboBox()
        for size in [128, 256, 512, 1024, 2048, 4096, 8192]:
            self.bytes_per_sector_combo.addItem(f"{size} bytes", size)
        self.bytes_per_sector_combo.setCurrentIndex(2)  # 512 bytes
        format_params_layout.addRow("Sector size:", self.bytes_per_sector_combo)

        # Encoding
        self.encoding_combo = QComboBox()
        self.encoding_combo.addItem("MFM", "MFM")
        self.encoding_combo.addItem("FM", "FM")
        format_params_layout.addRow("Encoding:", self.encoding_combo)

        # Data rate
        self.rate_combo = QComboBox()
        for rate in [125, 250, 300, 500, 1000]:
            self.rate_combo.addItem(f"{rate} kbps", rate)
        self.rate_combo.setCurrentIndex(3)  # 500 kbps
        format_params_layout.addRow("Data rate:", self.rate_combo)

        # RPM
        self.rpm_combo = QComboBox()
        for rpm in [300, 360]:
            self.rpm_combo.addItem(f"{rpm} RPM", rpm)
        format_params_layout.addRow("RPM:", self.rpm_combo)

        # Gap3
        self.gap3_spin = QSpinBox()
        self.gap3_spin.setRange(1, 255)
        self.gap3_spin.setValue(84)
        format_params_layout.addRow("Gap3:", self.gap3_spin)

        # Sector skew
        self.cskew_spin = QSpinBox()
        self.cskew_spin.setRange(0, 255)
        self.cskew_spin.setValue(0)
        format_params_layout.addRow("Sector skew:", self.cskew_spin)

        # Interleave
        self.interleave_spin = QSpinBox()
        self.interleave_spin.setRange(1, 255)
        self.interleave_spin.setValue(1)
        format_params_layout.addRow("Interleave:", self.interleave_spin)

        self.format_params_group.setLayout(format_params_layout)
        main_layout.addWidget(self.format_params_group)

        # Initialize format list
        self.update_format_list()
        self.format_combo.currentIndexChanged.connect(self.on_format_changed)

        # Buttons
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)

        main_layout.addStretch()
        main_layout.addWidget(buttons)

        self.setLayout(main_layout)

        if not self.is_greaseweazle_connected():
            self.ok_button.setEnabled(False)
            no_device_label = QLabel("No Greaseweazle device found")
            main_layout.addWidget(no_device_label)

    def update_drive_options(self, checked):
        self.drive_combo.clear()
        if self.ibm_radio.isChecked():
            self.drive_combo.addItem("A", "A")
            self.drive_combo.addItem("B", "B")
        else:
            self.drive_combo.addItem("0", "0")
            self.drive_combo.addItem("1", "1")
            self.drive_combo.addItem("2", "2")
            self.drive_combo.addItem("3", "3")

    def update_format_list(self):
        current_data = self.format_combo.currentData()
        self.format_combo.clear()
        self.format_combo.addItem("Auto-detect", None)
        self.format_combo.addItem("Custom...", "custom")
        drive_size = self.size_combo.currentData()
        try:
            from ..core.format_definitions import FLOPPY_FORMATS
            for name, profile in FLOPPY_FORMATS.items():
                if drive_size == "3.5" and "3.5\"" in profile.description:
                    self.format_combo.addItem(profile.description, name)
                elif drive_size == "5.25" and "5.25\"" in profile.description:
                    self.format_combo.addItem(profile.description, name)
                elif drive_size == "8" and "8\"" in profile.description:
                    self.format_combo.addItem(profile.description, name)
        except Exception as e:
            print(f"Error loading format definitions: {e}")
        if current_data is not None:
            for i in range(self.format_combo.count()):
                if self.format_combo.itemData(i) == current_data:
                    self.format_combo.setCurrentIndex(i)
                    break

    def on_format_changed(self):
        format_key = self.format_combo.currentData()
        if format_key == "custom":
            self.format_params_group.setEnabled(True)
            drive_size = self.size_combo.currentData()
            self.set_default_parameters_for_size(drive_size)
        elif format_key is None:
            self.format_params_group.setEnabled(False)
        else:
            self.format_params_group.setEnabled(False)
            try:
                from ..core.format_definitions import FLOPPY_FORMATS
                profile = FLOPPY_FORMATS.get(format_key)
                if profile:
                    self.cylinders_spin.setValue(profile.physical_format.cylinders)
                    self.heads_spin.setValue(profile.physical_format.heads)
                    self.sectors_spin.setValue(profile.physical_format.sectors_per_track)
                    index = self.bytes_per_sector_combo.findData(profile.physical_format.bytes_per_sector)
                    if index >= 0:
                        self.bytes_per_sector_combo.setCurrentIndex(index)
                    index = self.encoding_combo.findData(profile.physical_format.encoding)
                    if index >= 0:
                        self.encoding_combo.setCurrentIndex(index)
                    index = self.rate_combo.findData(profile.physical_format.rate)
                    if index >= 0:
                        self.rate_combo.setCurrentIndex(index)
                    index = self.rpm_combo.findData(profile.physical_format.rpm)
                    if index >= 0:
                        self.rpm_combo.setCurrentIndex(index)
                    self.gap3_spin.setValue(profile.physical_format.gap3)
                    self.cskew_spin.setValue(profile.physical_format.cskew)
                    self.interleave_spin.setValue(profile.physical_format.interleave)
            except Exception as e:
                print(f"Error setting format parameters: {e}")

    def set_default_parameters_for_size(self, size):
        if size == "3.5":
            self.cylinders_spin.setValue(80)
            self.heads_spin.setValue(2)
            self.sectors_spin.setValue(18)
            self.bytes_per_sector_combo.setCurrentIndex(2)
            self.encoding_combo.setCurrentIndex(0)
            self.rate_combo.setCurrentIndex(3)
            self.rpm_combo.setCurrentIndex(0)
            self.gap3_spin.setValue(84)
            self.cskew_spin.setValue(0)
            self.interleave_spin.setValue(1)
        elif size == "5.25":
            self.cylinders_spin.setValue(40)
            self.heads_spin.setValue(2)
            self.sectors_spin.setValue(9)
            self.bytes_per_sector_combo.setCurrentIndex(2)
            self.encoding_combo.setCurrentIndex(0)
            self.rate_combo.setCurrentIndex(1)
            self.rpm_combo.setCurrentIndex(0)
            self.gap3_spin.setValue(84)
            self.cskew_spin.setValue(0)
            self.interleave_spin.setValue(1)
        elif size == "8":
            self.cylinders_spin.setValue(77)
            self.heads_spin.setValue(2)
            self.sectors_spin.setValue(26)
            self.bytes_per_sector_combo.setCurrentIndex(0)
            self.encoding_combo.setCurrentIndex(1)
            self.rate_combo.setCurrentIndex(1)
            self.rpm_combo.setCurrentIndex(1)
            self.gap3_spin.setValue(26)
            self.cskew_spin.setValue(0)
            self.interleave_spin.setValue(1)

    def get_selection(self):
        drive = self.drive_combo.currentData()
        size = self.size_combo.currentData()
        format_key = self.format_combo.currentData()
        format_info = None
        if format_key == "custom" and self.format_params_group.isEnabled():
            format_info = {
                "cylinders": self.cylinders_spin.value(),
                "heads": self.heads_spin.value(),
                "sectors_per_track": self.sectors_spin.value(),
                "bytes_per_sector": self.bytes_per_sector_combo.currentData(),
                "encoding": self.encoding_combo.currentData(),
                "rate": self.rate_combo.currentData(),
                "rpm": self.rpm_combo.currentData(),
                "gap3": self.gap3_spin.value(),
                "cskew": self.cskew_spin.value(),
                "interleave": self.interleave_spin.value()
            }
        elif format_key is not None and format_key != "custom":
            try:
                from ..core.format_definitions import FLOPPY_FORMATS
                profile = FLOPPY_FORMATS.get(format_key)
                if profile:
                    format_info = {
                        "profile_name": format_key,
                        "cylinders": profile.physical_format.cylinders,
                        "heads": profile.physical_format.heads,
                        "sectors_per_track": profile.physical_format.sectors_per_track,
                        "bytes_per_sector": profile.physical_format.bytes_per_sector,
                        "encoding": profile.physical_format.encoding,
                        "rate": profile.physical_format.rate,
                        "rpm": profile.physical_format.rpm,
                        "gap3": profile.physical_format.gap3,
                        "cskew": profile.physical_format.cskew,
                        "interleave": profile.physical_format.interleave
                    }
            except Exception as e:
                print(f"Error getting format parameters: {e}")
        return drive, size, format_info

    def is_greaseweazle_connected(self):
        try:
            from greaseweazle.tools.util import usb_open
            usb = usb_open(None)
            usb.ser.close()
            return True
        except Exception:
            return False

class CreateImageDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Create Disk Image")
        self.resize(400, 500)

        main_layout = QVBoxLayout()

        # Disk Size Selection
        size_layout = QHBoxLayout()
        size_layout.addWidget(QLabel("Disk Size:"))
        self.size_combo = QComboBox()
        self.size_combo.addItem("3.5\"", "3.5")
        self.size_combo.addItem("5.25\"", "5.25")
        self.size_combo.addItem("8\"", "8")
        self.size_combo.currentIndexChanged.connect(self.update_format_list)
        size_layout.addWidget(self.size_combo)
        size_layout.addStretch()
        main_layout.addLayout(size_layout)

        # Format Selection (Mandatory)
        format_layout = QHBoxLayout()
        format_layout.addWidget(QLabel("Format:"))
        self.format_combo = QComboBox()
        # No "Select Format" option to make selection mandatory
        format_layout.addWidget(self.format_combo)
        format_layout.addStretch()
        main_layout.addLayout(format_layout)

        # Directory Selection
        directory_layout = QHBoxLayout()
        directory_layout.addWidget(QLabel("Directory:"))
        self.directory_input = QLineEdit()
        self.directory_input.setPlaceholderText("Select or enter directory")
        directory_layout.addWidget(self.directory_input)
        self.select_directory_button = QPushButton("...")
        self.select_directory_button.clicked.connect(self.select_directory)
        directory_layout.addWidget(self.select_directory_button)
        main_layout.addLayout(directory_layout)

        # File Name and Extension Selection
        file_name_layout = QHBoxLayout()
        file_name_layout.addWidget(QLabel("File Name:"))
        self.file_name_input = QLineEdit()
        self.file_name_input.setPlaceholderText("Enter file name")
        file_name_layout.addWidget(self.file_name_input)
        self.extension_combo = QComboBox()
        self.extension_combo.addItem(".img", "IMG")
        self.extension_combo.addItem(".ima", "IMG")
        self.extension_combo.addItem(".imd", "IMD")
        self.extension_combo.currentIndexChanged.connect(self.update_file_name)
        file_name_layout.addWidget(self.extension_combo)
        file_name_layout.addStretch()
        main_layout.addLayout(file_name_layout)

        # Advanced Settings Checkbox
        self.advanced_checkbox = QCheckBox("Advanced Settings")
        self.advanced_checkbox.stateChanged.connect(self.toggle_advanced_settings)
        main_layout.addWidget(self.advanced_checkbox)

        # Format Parameters Group
        self.format_params_group = QGroupBox("Format Parameters")
        self.format_params_group.setEnabled(False)
        format_params_layout = QFormLayout()

        # Cylinders
        self.cylinders_spin = QSpinBox()
        self.cylinders_spin.setRange(1, 100)
        self.cylinders_spin.setValue(80)
        format_params_layout.addRow("Cylinders:", self.cylinders_spin)

        # Heads
        self.heads_spin = QSpinBox()
        self.heads_spin.setRange(1, 2)
        self.heads_spin.setValue(2)
        format_params_layout.addRow("Heads:", self.heads_spin)

        # Sectors per track
        self.sectors_spin = QSpinBox()
        self.sectors_spin.setRange(1, 100)
        self.sectors_spin.setValue(18)
        format_params_layout.addRow("Sectors per track:", self.sectors_spin)

        # Sector size
        self.bytes_per_sector_combo = QComboBox()
        for size in [128, 256, 512, 1024, 2048, 4096, 8192]:
            self.bytes_per_sector_combo.addItem(f"{size} bytes", size)
        self.bytes_per_sector_combo.setCurrentIndex(2)  # 512 bytes
        format_params_layout.addRow("Sector size:", self.bytes_per_sector_combo)

        # Encoding
        self.encoding_combo = QComboBox()
        self.encoding_combo.addItem("MFM", "MFM")
        self.encoding_combo.addItem("FM", "FM")
        format_params_layout.addRow("Encoding:", self.encoding_combo)

        # Data rate
        self.rate_combo = QComboBox()
        for rate in [125, 250, 300, 500, 1000]:
            self.rate_combo.addItem(f"{rate} kbps", rate)
        self.rate_combo.setCurrentIndex(3)  # 500 kbps
        format_params_layout.addRow("Data rate:", self.rate_combo)

        # RPM
        self.rpm_combo = QComboBox()
        for rpm in [300, 360]:
            self.rpm_combo.addItem(f"{rpm} RPM", rpm)
        format_params_layout.addRow("RPM:", self.rpm_combo)

        # Gap3
        self.gap3_spin = QSpinBox()
        self.gap3_spin.setRange(1, 255)
        self.gap3_spin.setValue(84)
        format_params_layout.addRow("Gap3:", self.gap3_spin)

        # Sector skew
        self.cskew_spin = QSpinBox()
        self.cskew_spin.setRange(0, 255)
        self.cskew_spin.setValue(0)
        format_params_layout.addRow("Sector skew:", self.cskew_spin)

        # Interleave
        self.interleave_spin = QSpinBox()
        self.interleave_spin.setRange(1, 255)
        self.interleave_spin.setValue(1)
        format_params_layout.addRow("Interleave:", self.interleave_spin)

        self.format_params_group.setLayout(format_params_layout)
        main_layout.addWidget(self.format_params_group)

        # Volume Label
        volume_label_layout = QHBoxLayout()
        volume_label_layout.addWidget(QLabel("Volume Label:"))
        self.volume_label_input = QLineEdit()
        self.volume_label_input.setMaxLength(11)
        volume_label_layout.addWidget(self.volume_label_input)
        volume_label_layout.addStretch()
        main_layout.addLayout(volume_label_layout)

        # Initialize format list
        self.update_format_list()
        self.format_combo.currentIndexChanged.connect(self.on_format_changed)

        # Buttons
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        main_layout.addStretch()
        main_layout.addWidget(buttons)

        self.setLayout(main_layout)

    def update_format_list(self):
        current_data = self.format_combo.currentData()
        self.format_combo.clear()
        drive_size = self.size_combo.currentData()
        try:
            from ..core.format_definitions import FLOPPY_FORMATS
            for name, profile in FLOPPY_FORMATS.items():
                if drive_size == "3.5" and "3.5\"" in profile.description:
                    self.format_combo.addItem(profile.description, name)
                elif drive_size == "5.25" and "5.25\"" in profile.description:
                    self.format_combo.addItem(profile.description, name)
                elif drive_size == "8" and "8\"" in profile.description:
                    self.format_combo.addItem(profile.description, name)
            if self.format_combo.count() > 0:
                self.format_combo.setCurrentIndex(0)  # Default to first format
        except Exception as e:
            print(f"Error loading format definitions: {e}")
        if current_data is not None:
            for i in range(self.format_combo.count()):
                if self.format_combo.itemData(i) == current_data:
                    self.format_combo.setCurrentIndex(i)
                    break

    def on_format_changed(self):
        """Handle changes in the format selection and update format parameters."""
        format_key = self.format_combo.currentData()
        if format_key is None:
            self.format_params_group.setEnabled(self.advanced_checkbox.isChecked())
        else:
            self.format_params_group.setEnabled(False)
            try:
                from ..core.format_definitions import FLOPPY_FORMATS
                profile = FLOPPY_FORMATS.get(format_key)
                if profile:
                    self.cylinders_spin.setValue(profile.physical_format.cylinders)
                    self.heads_spin.setValue(profile.physical_format.heads)
                    # Access sectors_per_track from the first TrackFormat object
                    if profile.physical_format.track_formats and len(profile.physical_format.track_formats) > 0:
                        track_format = profile.physical_format.track_formats[0]
                        self.sectors_spin.setValue(track_format.sectors_per_track)
                        self.gap3_spin.setValue(track_format.gap3)
                        self.cskew_spin.setValue(track_format.cskew)
                        self.interleave_spin.setValue(track_format.interleave)
                        index = self.encoding_combo.findData(track_format.encoding)
                        if index >= 0:
                            self.encoding_combo.setCurrentIndex(index)
                        index = self.rate_combo.findData(track_format.rate)
                        if index >= 0:
                            self.rate_combo.setCurrentIndex(index)
                    else:
                        self.sectors_spin.setValue(0)  # Default value if no track formats
                    # Set bytes_per_sector and rpm from PhysicalFormat
                    index = self.bytes_per_sector_combo.findData(profile.physical_format.bytes_per_sector)
                    if index >= 0:
                        self.bytes_per_sector_combo.setCurrentIndex(index)
                    index = self.rpm_combo.findData(profile.physical_format.rpm)
                    if index >= 0:
                        self.rpm_combo.setCurrentIndex(index)
            except Exception as e:
                print(f"Error setting format parameters: {e}")

    def toggle_advanced_settings(self, state):
        self.format_params_group.setEnabled(state == Qt.CheckState.Checked)

    def select_directory(self):
        directory = QFileDialog.getExistingDirectory(self, "Select Directory")
        if directory:
            self.directory_input.setText(directory)

    def update_file_name(self):
        current_name = self.file_name_input.text().strip()
        if current_name:
            extension = self.extension_combo.currentText()
            # Remove existing extension if present
            if "." in current_name:
                current_name = current_name.rsplit(".", 1)[0]
            new_name = f"{current_name}{extension}"
            self.file_name_input.setText(new_name)

    def get_selection(self):
        """Retrieve and validate user selections for image creation."""
        directory = self.directory_input.text().strip()
        file_name = self.file_name_input.text().strip()
        if not directory or not file_name:
            raise ValueError("Both directory and file name must be provided.")

        file_path = os.path.join(directory, file_name)
        if os.path.isdir(file_path):
            raise ValueError(f"The path '{file_path}' is a directory, not a file. Please specify a valid file name.")

        size = self.size_combo.currentData()
        format_key = self.format_combo.currentData()
        volume_label = self.volume_label_input.text().strip().upper() or "NO NAME"
        output_format = self.extension_combo.currentData()

        if format_key is None or self.advanced_checkbox.isChecked():
            format_info = {
                "cylinders": self.cylinders_spin.value(),
                "heads": self.heads_spin.value(),
                "sectors_per_track": self.sectors_spin.value(),
                "bytes_per_sector": self.bytes_per_sector_combo.currentData(),
                "encoding": self.encoding_combo.currentData(),
                "rate": self.rate_combo.currentData(),
                "rpm": self.rpm_combo.currentData(),
                "gap3": self.gap3_spin.value(),
                "cskew": self.cskew_spin.value(),
                "interleave": self.interleave_spin.value()
            }
        else:
            format_info = {"profile_name": format_key}

        return file_path, format_info, volume_label, output_format
