# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec file for FatFloppy macOS application.
Uses onedir mode with proper code signing for macOS compatibility.
"""

import sys
from pathlib import Path

# Get project root (spec is in packaging/macos/, project root is ../..)
project_root = Path(SPECPATH).parent.parent
version_file = project_root / "src" / "fatfloppy" / "_version.py"

# Read version
version_dict = {}
with open(version_file) as f:
    exec(f.read(), version_dict)
VERSION = version_dict["__version__"]

# Path to icon
icon_path = str(project_root / "assets" / "icons" / "fatfloppy_icon.icns")

a = Analysis(
    [str(project_root / 'src' / 'fatfloppy' / 'main.py')],
    pathex=[str(project_root / "src")],
    binaries=[],
    datas=[
        (str(project_root / "assets" / "icons" / "fatfloppy_icon.icns"), "assets/icons"),
        (str(project_root / "assets" / "icons" / "fatfloppy_icon.png"), "assets/icons"),
        (str(project_root / "assets" / "fonts" / "JetBrainsMono-Regular.ttf"), "assets/fonts"),
        (str(Path(SPECPATH) / "qt.conf"), "."),
    ],
    hiddenimports=[
        # PyQt6 modules
        'PyQt6.QtCore',
        'PyQt6.QtGui',
        'PyQt6.QtWidgets',
        # External dependencies
        'greaseweazle',
        # FatFloppy core modules
        'fatfloppy.core',
        'fatfloppy.gui',
        # Filesystem plugins (dynamically discovered)
        'fatfloppy.core.filesystems.fat12_fs',
        'fatfloppy.core.filesystems.cpm_fs',
        'fatfloppy.core.filesystems.hdos_fs',
        'fatfloppy.core.filesystems.fs_base',
        # Filesystem format definitions
        'fatfloppy.core.filesystems.formats',
        'fatfloppy.core.filesystems.formats.fat12_formats',
        'fatfloppy.core.filesystems.formats.cpm_formats',
        'fatfloppy.core.filesystems.formats.hdos_formats',
        # Driver plugins (dynamically discovered)
        'fatfloppy.core.drivers.img',
        'fatfloppy.core.drivers.imd',
        'fatfloppy.core.drivers.h17',
        'fatfloppy.core.drivers.mits_dsk',
        'fatfloppy.core.drivers.greaseweazle',
        'fatfloppy.core.drivers.base_driver',
        # Detector plugins (dynamically discovered)
        'fatfloppy.core.drivers.detectors',
        'fatfloppy.core.drivers.detectors.img_detector',
        'fatfloppy.core.drivers.detectors.imd_detector',
        'fatfloppy.core.drivers.detectors.h17_detector',
        'fatfloppy.core.drivers.detectors.mits_dsk_detector',
        'fatfloppy.core.drivers.detectors.greaseweazle_detector',
        # Plugin discovery system
        'fatfloppy.core.plugin_scanner',
        'fatfloppy.core.driver_factory',
        'fatfloppy.core.filesystem_registry',
        'fatfloppy.core.detector_registry',
        'fatfloppy.core.format_detection',
    ],
    hookspath=[str(Path(SPECPATH) / "hooks")],
    hooksconfig={},
    runtime_hooks=[str(Path(SPECPATH) / "hooks" / "rthook_pyqt6.py")],
    excludes=[
        # Standard exclusions
        'tkinter',
        'matplotlib',
        'numpy',
        'scipy',
        'pandas',
        # PyQt6 modules we don't use
        'PyQt6.QtBluetooth',
        'PyQt6.QtDBus',
        'PyQt6.QtDesigner',
        'PyQt6.QtHelp',
        'PyQt6.QtLocation',
        'PyQt6.QtMultimedia',
        'PyQt6.QtMultimediaWidgets',
        'PyQt6.QtNetwork',
        'PyQt6.QtNetworkAuth',
        'PyQt6.QtNfc',
        'PyQt6.QtOpenGL',
        'PyQt6.QtOpenGLWidgets',
        'PyQt6.QtPdf',
        'PyQt6.QtPdfWidgets',
        'PyQt6.QtPositioning',
        'PyQt6.QtPrintSupport',
        'PyQt6.QtQml',
        'PyQt6.QtQuick',
        'PyQt6.QtQuick3D',
        'PyQt6.QtQuickWidgets',
        'PyQt6.QtRemoteObjects',
        'PyQt6.QtSensors',
        'PyQt6.QtSerialPort',
        'PyQt6.QtSql',
        'PyQt6.QtSvg',
        'PyQt6.QtSvgWidgets',
        'PyQt6.QtTest',
        'PyQt6.QtTextToSpeech',
        'PyQt6.QtWebChannel',
        'PyQt6.QtWebEngineCore',
        'PyQt6.QtWebEngineQuick',
        'PyQt6.QtWebEngineWidgets',
        'PyQt6.QtWebSockets',
        'PyQt6.QtXml',
    ],
    noarchive=False,
    optimize=1,
)

# Remove unnecessary Qt plugins to reduce size
a.datas = [x for x in a.datas if not any([
    'Qt6/plugins/qmlls' in x[0],
    'Qt6/plugins/sceneparsers' in x[0],
    'Qt6/plugins/assetimporters' in x[0],
    'Qt6/plugins/sqldrivers' in x[0],
    'Qt6/plugins/renderers' in x[0],
    'Qt6/plugins/multimedia' in x[0],
    'Qt6/plugins/tls' in x[0],
    'Qt6/plugins/texttospeech' in x[0],
    'Qt6/plugins/position' in x[0],
    'Qt6/plugins/geometryloaders' in x[0],
    'Qt6/plugins/webview' in x[0],
    'Qt6/plugins/qmllint' in x[0],
    'Qt6/plugins/scxmldatamodel' in x[0],
    'Qt6/plugins/renderplugins' in x[0],
    'Qt6/plugins/sensors' in x[0],
    'Qt6/plugins/help' in x[0],
    'Qt6/plugins/networkinformation' in x[0],
    'Qt6/qml/' in x[0],  # Remove all QML files
])]

# Remove unnecessary Qt frameworks
a.binaries = [x for x in a.binaries if not any([
    'QtQuick' in x[0],
    'QtQml' in x[0],
    'QtPdf' in x[0],
    'QtDesigner' in x[0],
    'QtMultimedia' in x[0],
    'QtBluetooth' in x[0],
    'QtNfc' in x[0],
    'QtSensors' in x[0],
    'QtWebEngine' in x[0],
    'QtWebChannel' in x[0],
    'QtWebSockets' in x[0],
    'Qt3D' in x[0],
    'QtShader' in x[0],
    'QtSvg' in x[0],
    'QtSql' in x[0],
    'QtTest' in x[0],
    'QtRemoteObjects' in x[0],
    'QtNetworkAuth' in x[0],
    'QtHelp' in x[0],
    'QtOpenGL' in x[0],
    'QtPrintSupport' in x[0],
    'QtTextToSpeech' in x[0],
    'QtSerialPort' in x[0],
])]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,  # onedir mode - binaries separate
    name='FatFloppy',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=str(Path(SPECPATH) / 'entitlements.plist'),
    icon=icon_path,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='FatFloppy',
)

app = BUNDLE(
    coll,
    name='FatFloppy.app',
    icon=icon_path,
    bundle_identifier='com.fatfloppy.app',
    version=VERSION,
    info_plist={
        'CFBundleName': 'FatFloppy',
        'CFBundleDisplayName': 'FatFloppy',
        'CFBundleIdentifier': 'com.fatfloppy.app',
        'CFBundleVersion': VERSION,
        'CFBundleShortVersionString': VERSION,
        'CFBundlePackageType': 'APPL',
        'CFBundleSignature': '????',
        'NSHighResolutionCapable': True,
        'LSMinimumSystemVersion': '10.13.0',
        'NSPrincipalClass': 'NSApplication',
        'NSHumanReadableCopyright': 'Copyright © 2025. Licensed under MIT.',
        'LSApplicationCategoryType': 'public.app-category.utilities',
        'NSRequiresAquaSystemAppearance': False,
        'LSUIElement': False,
    },
)
