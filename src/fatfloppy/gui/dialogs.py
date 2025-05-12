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
        self.resize(400, 650)

        main_layout = QVBoxLayout()

        drive_type_group = QGroupBox("Drive Interface Type")
        drive_type_layout = QHBoxLayout()
        self.ibm_radio = QRadioButton("IBM PC (A/B)")
        self.ibm_radio.setChecked(True)
        self.ibm_radio.toggled.connect(self.update_drive_options)
        self.shugart_radio = QRadioButton("Shugart (0-3)")
        drive_type_layout.addWidget(self.ibm_radio)
        drive_type_layout.addWidget(self.shugart_radio)
        drive_type_group.setLayout(drive_type_layout)
        main_layout.addWidget(drive_type_group)

        drive_layout = QHBoxLayout()
        drive_layout.addWidget(QLabel("Drive:"))
        self.drive_combo = QComboBox()
        self.update_drive_options(True)
        drive_layout.addWidget(self.drive_combo)
        drive_layout.addStretch()
        main_layout.addLayout(drive_layout)

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

        format_layout = QHBoxLayout()
        format_layout.addWidget(QLabel("Format (optional):"))
        self.format_combo = QComboBox()
        self.format_combo.addItem("Auto-detect", None)
        self.format_combo.addItem("Custom...", "custom")
        format_layout.addWidget(self.format_combo)
        format_layout.addStretch()
        main_layout.addLayout(format_layout)

        self.format_params_group = QGroupBox("Format Parameters")
        self.format_params_group.setEnabled(False)
        format_params_layout = QFormLayout()

        self.cylinders_spin = QSpinBox()
        self.cylinders_spin.setRange(1, 100)
        self.cylinders_spin.setValue(80)
        format_params_layout.addRow("Cylinders:", self.cylinders_spin)
        self.heads_spin = QSpinBox()
        self.heads_spin.setRange(1, 2)
        self.heads_spin.setValue(2)
        format_params_layout.addRow("Heads:", self.heads_spin)
        self.sectors_spin = QSpinBox()
        self.sectors_spin.setRange(1, 100)
        self.sectors_spin.setValue(18)
        format_params_layout.addRow("Sectors/track:", self.sectors_spin)
        self.bytes_per_sector_combo = QComboBox()
        for size_val in [128, 256, 512, 1024, 2048, 4096, 8192]:
            self.bytes_per_sector_combo.addItem(f"{size_val} bytes", size_val)
        self.bytes_per_sector_combo.setCurrentIndex(2)
        format_params_layout.addRow("Sector size:", self.bytes_per_sector_combo)
        self.encoding_combo = QComboBox()
        self.encoding_combo.addItem("MFM", "MFM")
        self.encoding_combo.addItem("FM", "FM")
        format_params_layout.addRow("Encoding:", self.encoding_combo)
        self.rate_combo = QComboBox()
        for rate_val in [125, 250, 300, 500, 1000]:
            self.rate_combo.addItem(f"{rate_val} kbps", rate_val)
        self.rate_combo.setCurrentIndex(3)
        format_params_layout.addRow("Data rate:", self.rate_combo)
        self.rpm_combo = QComboBox()
        for rpm_val in [300, 360]:
            self.rpm_combo.addItem(f"{rpm_val} RPM", rpm_val)
        format_params_layout.addRow("RPM:", self.rpm_combo)
        self.interleave_spin = QSpinBox()
        self.interleave_spin.setRange(1, 255)
        self.interleave_spin.setValue(1)
        format_params_layout.addRow("Interleave:", self.interleave_spin)

        self.id_start_spin = QSpinBox()
        self.id_start_spin.setRange(0, 255); self.id_start_spin.setValue(1)
        format_params_layout.addRow("Sector ID Start:", self.id_start_spin)
        self.iam_present_check = QCheckBox("IAM Present")
        self.iam_present_check.setChecked(True)
        format_params_layout.addRow(self.iam_present_check)
        self.gap1_bytes_spin = QSpinBox(); self.gap1_bytes_spin.setRange(0,255); self.gap1_bytes_spin.setSpecialValueText("Default (0)")
        format_params_layout.addRow("Gap1 Bytes:", self.gap1_bytes_spin)
        self.gap2_bytes_spin = QSpinBox(); self.gap2_bytes_spin.setRange(0,255); self.gap2_bytes_spin.setSpecialValueText("Default (0)")
        format_params_layout.addRow("Gap2 Bytes:", self.gap2_bytes_spin)
        self.gap3_bytes_spin = QSpinBox()
        self.gap3_bytes_spin.setRange(0, 255); self.gap3_bytes_spin.setValue(84); self.gap3_bytes_spin.setSpecialValueText("Default (0)")
        format_params_layout.addRow("Gap3 Bytes:", self.gap3_bytes_spin)
        self.cskew_spin = QSpinBox()
        self.cskew_spin.setRange(0, 255); self.cskew_spin.setValue(0); self.cskew_spin.setSpecialValueText("Default (0)")
        format_params_layout.addRow("Cylinder Skew:", self.cskew_spin)
        self.hskew_spin = QSpinBox(); self.hskew_spin.setRange(0,255); self.hskew_spin.setSpecialValueText("Default (0)")
        format_params_layout.addRow("Head Skew:", self.hskew_spin)

        self.format_params_group.setLayout(format_params_layout)
        main_layout.addWidget(self.format_params_group)

        self.update_format_list()
        self.format_combo.currentIndexChanged.connect(self.on_format_changed)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
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

    def update_drive_options(self, checked=None):
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
                    self.format_combo.addItem(f"{profile.description} ({name})", name)
                elif drive_size == "5.25" and "5.25\"" in profile.description:
                    self.format_combo.addItem(f"{profile.description} ({name})", name)
                elif drive_size == "8" and "8\"" in profile.description:
                    self.format_combo.addItem(f"{profile.description} ({name})", name)
        except Exception as e:
            print(f"Error loading format definitions: {e}")
        if current_data is not None:
            idx = self.format_combo.findData(current_data)
            if idx != -1:
                self.format_combo.setCurrentIndex(idx)
        self.on_format_changed()


    def on_format_changed(self):
        format_key = self.format_combo.currentData()
        is_custom = (format_key == "custom")
        self.format_params_group.setEnabled(is_custom)

        if is_custom:
            self.set_default_parameters_for_size(self.size_combo.currentData())
        elif format_key is None:
            self.format_params_group.setEnabled(False)
        else:
            self.format_params_group.setEnabled(False)
            try:
                from ..core.format_definitions import FLOPPY_FORMATS
                profile = FLOPPY_FORMATS.get(format_key)
                if profile and profile.physical_format and profile.physical_format.track_formats:
                    pf = profile.physical_format
                    tf = pf.track_formats[0]
                    self.cylinders_spin.setValue(pf.cylinders)
                    self.heads_spin.setValue(pf.heads)
                    self.sectors_spin.setValue(tf.sectors_per_track)
                    self.bytes_per_sector_combo.setCurrentIndex(self.bytes_per_sector_combo.findData(pf.bytes_per_sector))
                    self.encoding_combo.setCurrentIndex(self.encoding_combo.findData(tf.encoding))
                    self.rate_combo.setCurrentIndex(self.rate_combo.findData(tf.rate))
                    self.rpm_combo.setCurrentIndex(self.rpm_combo.findData(pf.rpm))
                    self.interleave_spin.setValue(tf.interleave)
                    val_id_start = getattr(tf, 'id_start', 1)
                    self.id_start_spin.setValue(val_id_start if val_id_start is not None else 1)
                    self.iam_present_check.setChecked(getattr(tf, 'iam_present', True))
                    val_gap1 = getattr(tf, 'gap1_bytes', None)
                    self.gap1_bytes_spin.setValue(val_gap1 if val_gap1 is not None else 0)
                    val_gap2 = getattr(tf, 'gap2_bytes', None)
                    self.gap2_bytes_spin.setValue(val_gap2 if val_gap2 is not None else 0)
                    val_gap3 = getattr(tf, 'gap3_bytes', None)
                    self.gap3_bytes_spin.setValue(val_gap3 if val_gap3 is not None else 0)
                    val_cskew = getattr(tf, 'cskew', None)
                    self.cskew_spin.setValue(val_cskew if val_cskew is not None else 0)
                    val_hskew = getattr(tf, 'hskew', None)
                    self.hskew_spin.setValue(val_hskew if val_hskew is not None else 0)
            except Exception as e:
                print(f"Error setting format parameters from profile: {e}")

    def set_default_parameters_for_size(self, size):
        if size == "3.5":
            self.cylinders_spin.setValue(80); self.heads_spin.setValue(2); self.sectors_spin.setValue(18)
            self.bytes_per_sector_combo.setCurrentIndex(self.bytes_per_sector_combo.findData(512))
            self.encoding_combo.setCurrentIndex(self.encoding_combo.findData("MFM"))
            self.rate_combo.setCurrentIndex(self.rate_combo.findData(500))
            self.rpm_combo.setCurrentIndex(self.rpm_combo.findData(300))
            self.interleave_spin.setValue(1); self.id_start_spin.setValue(1); self.iam_present_check.setChecked(True)
            self.gap1_bytes_spin.setValue(0); self.gap2_bytes_spin.setValue(0); self.gap3_bytes_spin.setValue(84)
            self.cskew_spin.setValue(0); self.hskew_spin.setValue(0)
        elif size == "5.25":
            self.cylinders_spin.setValue(40); self.heads_spin.setValue(2); self.sectors_spin.setValue(9)
            self.bytes_per_sector_combo.setCurrentIndex(self.bytes_per_sector_combo.findData(512))
            self.encoding_combo.setCurrentIndex(self.encoding_combo.findData("MFM"))
            self.rate_combo.setCurrentIndex(self.rate_combo.findData(250))
            self.rpm_combo.setCurrentIndex(self.rpm_combo.findData(300))
            self.interleave_spin.setValue(1); self.id_start_spin.setValue(1); self.iam_present_check.setChecked(True)
            self.gap1_bytes_spin.setValue(0); self.gap2_bytes_spin.setValue(0); self.gap3_bytes_spin.setValue(50)
            self.cskew_spin.setValue(0); self.hskew_spin.setValue(0)
        elif size == "8":
            self.cylinders_spin.setValue(77); self.heads_spin.setValue(1); self.sectors_spin.setValue(26)
            self.bytes_per_sector_combo.setCurrentIndex(self.bytes_per_sector_combo.findData(128))
            self.encoding_combo.setCurrentIndex(self.encoding_combo.findData("FM"))
            self.rate_combo.setCurrentIndex(self.rate_combo.findData(250))
            self.rpm_combo.setCurrentIndex(self.rpm_combo.findData(360))
            self.interleave_spin.setValue(1); self.id_start_spin.setValue(1); self.iam_present_check.setChecked(True)
            self.gap1_bytes_spin.setValue(0); self.gap2_bytes_spin.setValue(0); self.gap3_bytes_spin.setValue(26)
            self.cskew_spin.setValue(0); self.hskew_spin.setValue(0)

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
                "interleave": self.interleave_spin.value(),
                "id_start": self.id_start_spin.value(),
                "iam_present": self.iam_present_check.isChecked(),
                "gap1_bytes": self.gap1_bytes_spin.value() if self.gap1_bytes_spin.value() != 0 else None,
                "gap2_bytes": self.gap2_bytes_spin.value() if self.gap2_bytes_spin.value() != 0 else None,
                "gap3_bytes": self.gap3_bytes_spin.value() if self.gap3_bytes_spin.value() != 0 else None,
                "cskew": self.cskew_spin.value() if self.cskew_spin.value() != 0 else None,
                "hskew": self.hskew_spin.value() if self.hskew_spin.value() != 0 else None,
            }
        elif format_key is not None and format_key != "custom":
            format_info = {"profile_name": format_key}
        return drive, size, format_info

    def is_greaseweazle_connected(self):
        try:
            from greaseweazle.tools.util import usb_open
            usb = usb_open(None)
            if usb and hasattr(usb, 'ser') and usb.ser:
                 usb.ser.close()
            return True
        except Exception:
            return False


class CreateImageDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Create Disk Image")
        self.resize(400, 650)

        main_layout = QVBoxLayout()

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

        format_layout = QHBoxLayout()
        format_layout.addWidget(QLabel("Format:"))
        self.format_combo = QComboBox()
        format_layout.addWidget(self.format_combo)
        format_layout.addStretch()
        main_layout.addLayout(format_layout)

        directory_layout = QHBoxLayout()
        directory_layout.addWidget(QLabel("Directory:"))
        self.directory_input = QLineEdit()
        self.directory_input.setPlaceholderText("Select or enter directory")
        directory_layout.addWidget(self.directory_input)
        self.select_directory_button = QPushButton("...")
        self.select_directory_button.clicked.connect(self.select_directory)
        directory_layout.addWidget(self.select_directory_button)
        main_layout.addLayout(directory_layout)

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

        self.advanced_checkbox = QCheckBox("Advanced Settings (Custom Geometry)")
        self.advanced_checkbox.stateChanged.connect(self.toggle_advanced_settings)
        main_layout.addWidget(self.advanced_checkbox)

        self.format_params_group = QGroupBox("Format Parameters")
        self.format_params_group.setEnabled(False)
        format_params_layout = QFormLayout()

        self.cylinders_spin = QSpinBox()
        self.cylinders_spin.setRange(1, 100); self.cylinders_spin.setValue(80)
        format_params_layout.addRow("Cylinders:", self.cylinders_spin)
        self.heads_spin = QSpinBox()
        self.heads_spin.setRange(1, 2); self.heads_spin.setValue(2)
        format_params_layout.addRow("Heads:", self.heads_spin)
        self.sectors_spin = QSpinBox()
        self.sectors_spin.setRange(1, 100); self.sectors_spin.setValue(18)
        format_params_layout.addRow("Sectors/track:", self.sectors_spin)
        self.bytes_per_sector_combo = QComboBox()
        for size_val_ci in [128, 256, 512, 1024, 2048, 4096, 8192]:
            self.bytes_per_sector_combo.addItem(f"{size_val_ci} bytes", size_val_ci)
        self.bytes_per_sector_combo.setCurrentIndex(2)
        format_params_layout.addRow("Sector size:", self.bytes_per_sector_combo)
        self.encoding_combo = QComboBox()
        self.encoding_combo.addItem("MFM", "MFM"); self.encoding_combo.addItem("FM", "FM")
        format_params_layout.addRow("Encoding:", self.encoding_combo)
        self.rate_combo = QComboBox()
        for rate_val_ci in [125, 250, 300, 500, 1000]:
            self.rate_combo.addItem(f"{rate_val_ci} kbps", rate_val_ci)
        self.rate_combo.setCurrentIndex(3)
        format_params_layout.addRow("Data rate:", self.rate_combo)
        self.rpm_combo = QComboBox()
        for rpm_val_ci in [300, 360]: self.rpm_combo.addItem(f"{rpm_val_ci} RPM", rpm_val_ci)
        format_params_layout.addRow("RPM:", self.rpm_combo)
        self.interleave_spin = QSpinBox()
        self.interleave_spin.setRange(1, 255); self.interleave_spin.setValue(1)
        format_params_layout.addRow("Interleave:", self.interleave_spin)

        self.id_start_spin = QSpinBox()
        self.id_start_spin.setRange(0, 255); self.id_start_spin.setValue(1)
        format_params_layout.addRow("Sector ID Start:", self.id_start_spin)
        self.iam_present_check = QCheckBox("IAM Present")
        self.iam_present_check.setChecked(True)
        format_params_layout.addRow(self.iam_present_check)
        self.gap1_bytes_spin = QSpinBox(); self.gap1_bytes_spin.setRange(0,255); self.gap1_bytes_spin.setSpecialValueText("Default (0)")
        format_params_layout.addRow("Gap1 Bytes:", self.gap1_bytes_spin)
        self.gap2_bytes_spin = QSpinBox(); self.gap2_bytes_spin.setRange(0,255); self.gap2_bytes_spin.setSpecialValueText("Default (0)")
        format_params_layout.addRow("Gap2 Bytes:", self.gap2_bytes_spin)
        self.gap3_bytes_spin = QSpinBox()
        self.gap3_bytes_spin.setRange(0, 255); self.gap3_bytes_spin.setValue(84); self.gap3_bytes_spin.setSpecialValueText("Default (0)")
        format_params_layout.addRow("Gap3 Bytes:", self.gap3_bytes_spin)
        self.cskew_spin = QSpinBox()
        self.cskew_spin.setRange(0, 255); self.cskew_spin.setValue(0); self.cskew_spin.setSpecialValueText("Default (0)")
        format_params_layout.addRow("Cylinder Skew:", self.cskew_spin)
        self.hskew_spin = QSpinBox(); self.hskew_spin.setRange(0,255); self.hskew_spin.setSpecialValueText("Default (0)")
        format_params_layout.addRow("Head Skew:", self.hskew_spin)

        self.format_params_group.setLayout(format_params_layout)
        main_layout.addWidget(self.format_params_group)

        volume_label_layout = QHBoxLayout()
        volume_label_layout.addWidget(QLabel("Volume Label:"))
        self.volume_label_input = QLineEdit()
        self.volume_label_input.setMaxLength(11)
        volume_label_layout.addWidget(self.volume_label_input)
        volume_label_layout.addStretch()
        main_layout.addLayout(volume_label_layout)

        self.update_format_list()
        self.format_combo.currentIndexChanged.connect(self.on_format_changed)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        main_layout.addStretch()
        main_layout.addWidget(buttons)
        self.setLayout(main_layout)
        self.toggle_advanced_settings(self.advanced_checkbox.checkState())

    def update_format_list(self):
        current_data = self.format_combo.currentData()
        self.format_combo.clear()
        drive_size = self.size_combo.currentData()
        try:
            from ..core.format_definitions import FLOPPY_FORMATS
            for name, profile in FLOPPY_FORMATS.items():
                if drive_size == "3.5" and "3.5\"" in profile.description:
                    self.format_combo.addItem(f"{profile.description} ({name})", name)
                elif drive_size == "5.25" and "5.25\"" in profile.description:
                    self.format_combo.addItem(f"{profile.description} ({name})", name)
                elif drive_size == "8" and "8\"" in profile.description:
                    self.format_combo.addItem(f"{profile.description} ({name})", name)
            if self.format_combo.count() > 0:
                idx_to_select = 0
                if current_data is not None:
                    found_idx = self.format_combo.findData(current_data)
                    if found_idx != -1:
                        idx_to_select = found_idx
                self.format_combo.setCurrentIndex(idx_to_select)
        except Exception as e:
            print(f"Error loading format definitions: {e}")
        self.on_format_changed()

    def on_format_changed(self):
        if self.advanced_checkbox.isChecked():
            self.set_default_parameters_for_size(self.size_combo.currentData())
            return

        format_key = self.format_combo.currentData()
        if format_key:
            try:
                from ..core.format_definitions import FLOPPY_FORMATS
                profile = FLOPPY_FORMATS.get(format_key)
                if profile and profile.physical_format and profile.physical_format.track_formats:
                    pf = profile.physical_format
                    tf = pf.track_formats[0]
                    self.cylinders_spin.setValue(pf.cylinders)
                    self.heads_spin.setValue(pf.heads)
                    self.sectors_spin.setValue(tf.sectors_per_track)
                    self.bytes_per_sector_combo.setCurrentIndex(self.bytes_per_sector_combo.findData(pf.bytes_per_sector))
                    self.encoding_combo.setCurrentIndex(self.encoding_combo.findData(tf.encoding))
                    self.rate_combo.setCurrentIndex(self.rate_combo.findData(tf.rate))
                    self.rpm_combo.setCurrentIndex(self.rpm_combo.findData(pf.rpm))
                    self.interleave_spin.setValue(tf.interleave)

                    val_id_start = getattr(tf, 'id_start', 1)
                    self.id_start_spin.setValue(val_id_start if val_id_start is not None else 1)
                    self.iam_present_check.setChecked(getattr(tf, 'iam_present', True))
                    val_gap1 = getattr(tf, 'gap1_bytes', None)
                    self.gap1_bytes_spin.setValue(val_gap1 if val_gap1 is not None else 0)
                    val_gap2 = getattr(tf, 'gap2_bytes', None)
                    self.gap2_bytes_spin.setValue(val_gap2 if val_gap2 is not None else 0)
                    val_gap3 = getattr(tf, 'gap3_bytes', None)
                    self.gap3_bytes_spin.setValue(val_gap3 if val_gap3 is not None else 0)
                    val_cskew = getattr(tf, 'cskew', None)
                    self.cskew_spin.setValue(val_cskew if val_cskew is not None else 0)
                    val_hskew = getattr(tf, 'hskew', None)
                    self.hskew_spin.setValue(val_hskew if val_hskew is not None else 0)
                    self.update_file_name()
            except Exception as e:
                print(f"Error setting format parameters from profile: {e}")
        else:
             self.set_default_parameters_for_size(self.size_combo.currentData())

    def toggle_advanced_settings(self, state):
        is_checked = (state == Qt.CheckState.Checked.value if isinstance(state, int) else bool(state))
        self.format_params_group.setEnabled(is_checked)
        if is_checked:
            self.set_default_parameters_for_size(self.size_combo.currentData())
        else:
            self.on_format_changed()

    def set_default_parameters_for_size(self, size):
        if size == "3.5":
            self.cylinders_spin.setValue(80); self.heads_spin.setValue(2); self.sectors_spin.setValue(18)
            self.bytes_per_sector_combo.setCurrentIndex(self.bytes_per_sector_combo.findData(512))
            self.encoding_combo.setCurrentIndex(self.encoding_combo.findData("MFM"))
            self.rate_combo.setCurrentIndex(self.rate_combo.findData(500))
            self.rpm_combo.setCurrentIndex(self.rpm_combo.findData(300))
            self.interleave_spin.setValue(1); self.id_start_spin.setValue(1); self.iam_present_check.setChecked(True)
            self.gap1_bytes_spin.setValue(0); self.gap2_bytes_spin.setValue(0); self.gap3_bytes_spin.setValue(84)
            self.cskew_spin.setValue(0); self.hskew_spin.setValue(0)
        elif size == "5.25":
            self.cylinders_spin.setValue(40); self.heads_spin.setValue(2); self.sectors_spin.setValue(9)
            self.bytes_per_sector_combo.setCurrentIndex(self.bytes_per_sector_combo.findData(512))
            self.encoding_combo.setCurrentIndex(self.encoding_combo.findData("MFM"))
            self.rate_combo.setCurrentIndex(self.rate_combo.findData(250))
            self.rpm_combo.setCurrentIndex(self.rpm_combo.findData(300))
            self.interleave_spin.setValue(1); self.id_start_spin.setValue(1); self.iam_present_check.setChecked(True)
            self.gap1_bytes_spin.setValue(0); self.gap2_bytes_spin.setValue(0); self.gap3_bytes_spin.setValue(50)
            self.cskew_spin.setValue(0); self.hskew_spin.setValue(0)
        elif size == "8":
            self.cylinders_spin.setValue(77); self.heads_spin.setValue(1); self.sectors_spin.setValue(26)
            self.bytes_per_sector_combo.setCurrentIndex(self.bytes_per_sector_combo.findData(128))
            self.encoding_combo.setCurrentIndex(self.encoding_combo.findData("FM"))
            self.rate_combo.setCurrentIndex(self.rate_combo.findData(250))
            self.rpm_combo.setCurrentIndex(self.rpm_combo.findData(360))
            self.interleave_spin.setValue(1); self.id_start_spin.setValue(1); self.iam_present_check.setChecked(True)
            self.gap1_bytes_spin.setValue(0); self.gap2_bytes_spin.setValue(0); self.gap3_bytes_spin.setValue(26)
            self.cskew_spin.setValue(0); self.hskew_spin.setValue(0)


    def select_directory(self):
        directory = QFileDialog.getExistingDirectory(self, "Select Directory", self.directory_input.text() or os.path.expanduser("~"))
        if directory:
            self.directory_input.setText(directory)

    def update_file_name(self):
        current_name_base = self.file_name_input.text().strip()
        if not current_name_base and self.format_combo.currentData():
            current_name_base = self.format_combo.currentData().replace("\"", "").replace(" ", "_")

        if "." in current_name_base:
            current_name_base = current_name_base.rsplit(".",1)[0]

        extension = self.extension_combo.currentText()
        if current_name_base:
             self.file_name_input.setText(f"{current_name_base}{extension}")
        elif self.format_combo.currentData():
             base_from_format = self.format_combo.currentData().replace("\"", "").replace(" ", "_")
             self.file_name_input.setText(f"{base_from_format}{extension}")

    def get_selection(self):
        directory = self.directory_input.text().strip()
        file_name = self.file_name_input.text().strip()
        if not directory or not file_name:
            raise ValueError("Both directory and file name must be provided.")

        file_path = os.path.join(directory, file_name)
        if os.path.isdir(file_path):
            raise ValueError(f"The path '{file_path}' is a directory. Please specify a valid file name.")

        volume_label = self.volume_label_input.text().strip().upper() or "NO NAME"
        output_format = self.extension_combo.currentData()

        format_info = {}
        if self.advanced_checkbox.isChecked():
            format_info = {
                "cylinders": self.cylinders_spin.value(),
                "heads": self.heads_spin.value(),
                "sectors_per_track": self.sectors_spin.value(),
                "bytes_per_sector": self.bytes_per_sector_combo.currentData(),
                "encoding": self.encoding_combo.currentData(),
                "rate": self.rate_combo.currentData(),
                "rpm": self.rpm_combo.currentData(),
                "interleave": self.interleave_spin.value(),
                "id_start": self.id_start_spin.value(),
                "iam_present": self.iam_present_check.isChecked(),
                "gap1_bytes": self.gap1_bytes_spin.value() if self.gap1_bytes_spin.value() != 0 else None,
                "gap2_bytes": self.gap2_bytes_spin.value() if self.gap2_bytes_spin.value() != 0 else None,
                "gap3_bytes": self.gap3_bytes_spin.value() if self.gap3_bytes_spin.value() != 0 else None,
                "cskew": self.cskew_spin.value() if self.cskew_spin.value() != 0 else None,
                "hskew": self.hskew_spin.value() if self.hskew_spin.value() != 0 else None,
            }
        else:
            profile_name = self.format_combo.currentData()
            if not profile_name:
                raise ValueError("A format profile must be selected if not using advanced settings.")
            format_info = {"profile_name": profile_name}

        return file_path, format_info, volume_label, output_format
