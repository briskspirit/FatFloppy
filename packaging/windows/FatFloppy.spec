# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec file for FatFloppy Windows application.
Uses onedir mode for efficient distribution with Inno Setup installer.
"""

from pathlib import Path

# Get project root (spec is in packaging/windows/, project root is ../..)
project_root = Path(SPECPATH).parent.parent
version_file = project_root / "src" / "fatfloppy" / "_version.py"

# Read version
version_dict = {}
with open(version_file) as f:
    exec(f.read(), version_dict)
VERSION = version_dict["__version__"]

# Path to Windows icon
icon_path = str(project_root / "assets" / "icons" / "fatfloppy_icon.ico")

a = Analysis(
    [str(project_root / "src" / "fatfloppy" / "main.py")],
    pathex=[str(project_root / "src")],
    binaries=[],
    datas=[
        (str(project_root / "assets" / "icons" / "fatfloppy_icon.ico"), "assets/icons"),
        (str(project_root / "assets" / "icons" / "fatfloppy_icon.png"), "assets/icons"),
        (str(project_root / "assets" / "fonts" / "JetBrainsMono-Regular.ttf"), "assets/fonts"),
        (str(Path(SPECPATH) / "qt.conf"), "."),
    ],
    hiddenimports=[
        # PyQt6 modules
        "PyQt6.QtCore",
        "PyQt6.QtGui",
        "PyQt6.QtWidgets",
        # External dependencies
        "greaseweazle",
        # FatFloppy core modules
        "fatfloppy.core",
        "fatfloppy.gui",
        # Filesystem plugins (dynamically discovered)
        "fatfloppy.core.filesystems.fat12_fs",
        "fatfloppy.core.filesystems.cpm_fs",
        "fatfloppy.core.filesystems.hdos_fs",
        "fatfloppy.core.filesystems.fs_base",
        # Filesystem format definitions
        "fatfloppy.core.filesystems.formats",
        "fatfloppy.core.filesystems.formats.fat12_formats",
        "fatfloppy.core.filesystems.formats.cpm_formats",
        "fatfloppy.core.filesystems.formats.hdos_formats",
        # Driver plugins (dynamically discovered)
        "fatfloppy.core.drivers.img",
        "fatfloppy.core.drivers.imd",
        "fatfloppy.core.drivers.h17",
        "fatfloppy.core.drivers.mits_dsk",
        "fatfloppy.core.drivers.greaseweazle",
        "fatfloppy.core.drivers.base_driver",
        # Detector plugins (dynamically discovered)
        "fatfloppy.core.drivers.detectors",
        "fatfloppy.core.drivers.detectors.img_detector",
        "fatfloppy.core.drivers.detectors.imd_detector",
        "fatfloppy.core.drivers.detectors.h17_detector",
        "fatfloppy.core.drivers.detectors.mits_dsk_detector",
        "fatfloppy.core.drivers.detectors.greaseweazle_detector",
        # Plugin discovery system
        "fatfloppy.core.plugin_scanner",
        "fatfloppy.core.driver_factory",
        "fatfloppy.core.filesystem_registry",
        "fatfloppy.core.detector_registry",
        "fatfloppy.core.format_detection",
    ],
    hookspath=[str(Path(SPECPATH) / "hooks")],
    hooksconfig={},
    runtime_hooks=[str(Path(SPECPATH) / "hooks" / "rthook_pyqt6.py")],
    excludes=[
        # Standard exclusions
        "tkinter",
        "matplotlib",
        "numpy",
        "scipy",
        "pandas",
        # PyQt6 modules we don't use
        "PyQt6.QtBluetooth",
        "PyQt6.QtDBus",
        "PyQt6.QtDesigner",
        "PyQt6.QtHelp",
        "PyQt6.QtLocation",
        "PyQt6.QtMultimedia",
        "PyQt6.QtMultimediaWidgets",
        "PyQt6.QtNetwork",
        "PyQt6.QtNetworkAuth",
        "PyQt6.QtNfc",
        "PyQt6.QtOpenGL",
        "PyQt6.QtOpenGLWidgets",
        "PyQt6.QtPdf",
        "PyQt6.QtPdfWidgets",
        "PyQt6.QtPositioning",
        "PyQt6.QtPrintSupport",
        "PyQt6.QtQml",
        "PyQt6.QtQuick",
        "PyQt6.QtQuick3D",
        "PyQt6.QtQuickWidgets",
        "PyQt6.QtRemoteObjects",
        "PyQt6.QtSensors",
        "PyQt6.QtSerialPort",
        "PyQt6.QtSql",
        "PyQt6.QtSvg",
        "PyQt6.QtSvgWidgets",
        "PyQt6.QtTest",
        "PyQt6.QtTextToSpeech",
        "PyQt6.QtWebChannel",
        "PyQt6.QtWebEngineCore",
        "PyQt6.QtWebEngineQuick",
        "PyQt6.QtWebEngineWidgets",
        "PyQt6.QtWebSockets",
        "PyQt6.QtXml",
    ],
    noarchive=False,
    optimize=1,
)

# Remove unnecessary Qt plugins to reduce size
# Normalize paths to forward slashes for cross-platform compatibility
a.datas = [
    x
    for x in a.datas
    if not any(
        [
            "PyQt6/Qt6/plugins/qmlls" in x[0].replace("\\", "/"),
            "PyQt6/Qt6/plugins/sceneparsers" in x[0].replace("\\", "/"),
            "PyQt6/Qt6/plugins/assetimporters" in x[0].replace("\\", "/"),
            "PyQt6/Qt6/plugins/sqldrivers" in x[0].replace("\\", "/"),
            "PyQt6/Qt6/plugins/renderers" in x[0].replace("\\", "/"),
            "PyQt6/Qt6/plugins/multimedia" in x[0].replace("\\", "/"),
            "PyQt6/Qt6/plugins/tls" in x[0].replace("\\", "/"),
            "PyQt6/Qt6/plugins/texttospeech" in x[0].replace("\\", "/"),
            "PyQt6/Qt6/plugins/position" in x[0].replace("\\", "/"),
            "PyQt6/Qt6/plugins/geometryloaders" in x[0].replace("\\", "/"),
            "PyQt6/Qt6/plugins/webview" in x[0].replace("\\", "/"),
            "PyQt6/Qt6/plugins/qmllint" in x[0].replace("\\", "/"),
            "PyQt6/Qt6/plugins/scxmldatamodel" in x[0].replace("\\", "/"),
            "PyQt6/Qt6/plugins/renderplugins" in x[0].replace("\\", "/"),
            "PyQt6/Qt6/plugins/sensors" in x[0].replace("\\", "/"),
            "PyQt6/Qt6/plugins/help" in x[0].replace("\\", "/"),
            "PyQt6/Qt6/plugins/networkinformation" in x[0].replace("\\", "/"),
            "PyQt6/Qt6/qml/" in x[0].replace("\\", "/"),
        ]
    )
]

# Remove unnecessary Qt DLLs (Windows naming convention: Qt6Module.dll)
a.binaries = [
    x
    for x in a.binaries
    if not any(
        [
            "Qt6Quick" in x[0],
            "Qt6Qml" in x[0],
            "Qt6Pdf" in x[0],
            "Qt6Designer" in x[0],
            "Qt6Multimedia" in x[0],
            "Qt6Bluetooth" in x[0],
            "Qt6Nfc" in x[0],
            "Qt6Sensors" in x[0],
            "Qt6WebEngine" in x[0],
            "Qt6WebChannel" in x[0],
            "Qt6WebSockets" in x[0],
            "Qt63D" in x[0],
            "Qt6Shader" in x[0],
            "Qt6Svg" in x[0],
            "Qt6Sql" in x[0],
            "Qt6Test" in x[0],
            "Qt6RemoteObjects" in x[0],
            "Qt6NetworkAuth" in x[0],
            "Qt6Help" in x[0],
            "Qt6OpenGL" in x[0],
            "Qt6PrintSupport" in x[0],
            "Qt6TextToSpeech" in x[0],
            "Qt6SerialPort" in x[0],
        ]
    )
]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,  # onedir mode - binaries separate
    name="FatFloppy",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # GUI application, no console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon_path,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="FatFloppy",
)
