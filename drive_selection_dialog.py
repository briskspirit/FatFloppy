# drive_selection_dialog.py
from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QComboBox,
                              QDialogButtonBox, QGroupBox, QRadioButton)

class DriveSelectionDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Floppy Drive Selection")
        self.resize(400, 200)

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
        size_layout.addWidget(self.size_combo)
        size_layout.addStretch()

        main_layout.addLayout(size_layout)

        # Buttons
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        main_layout.addStretch()
        main_layout.addWidget(buttons)

        self.setLayout(main_layout)

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

    def get_selection(self):
        drive = self.drive_combo.currentData()
        size = self.size_combo.currentData()
        return drive, size
