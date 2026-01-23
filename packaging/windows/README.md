# Windows Packaging

This directory contains everything needed to build FatFloppy for Windows.

## Files

- `FatFloppy.spec` - PyInstaller specification
- `build_windows.ps1` - PowerShell build script
- `installer.iss` - Inno Setup installer script
- `qt.conf` - Qt plugin configuration
- `hooks/` - Custom PyInstaller hooks for PyQt6

## Prerequisites

1. **Python 3.9+** (64-bit)
2. **PyInstaller**: `pip install pyinstaller`
3. **Pillow** (for icon conversion): `pip install pillow`
4. **Inno Setup 6**: Download from https://jrsoftware.org/isdown.php

## Building

From PowerShell in the project root:

```powershell
# Full build (PyInstaller + Inno Setup installer)
.\packaging\windows\build_windows.ps1

# PyInstaller only (skip installer)
.\packaging\windows\build_windows.ps1 -SkipInstaller
```

## Output

- `dist\FatFloppy\` - Application folder (portable)
- `dist\FatFloppy-{version}-Windows-Setup.exe` - Installer

## Icon

Windows requires `.ico` format. If `assets/icons/fatfloppy_icon.ico` doesn't exist,
the build script will convert from PNG using Pillow.

To manually convert using ImageMagick:
```cmd
magick assets\icons\fatfloppy_icon.png -define icon:auto-resize=256,128,64,48,32,16 assets\icons\fatfloppy_icon.ico
```

## Installer Features

- Modern wizard UI
- Per-user installation (no admin required by default)
- Optional desktop shortcut
- Optional file associations (.img, .ima, .imd)
- Start menu shortcuts
- Clean uninstaller

## Architecture

64-bit Windows only. Supports:
- Windows 10 (64-bit)
- Windows 11 (64-bit)

## Optimization

The spec file excludes unused Qt modules to reduce size:
- Removed: QtQml, QtQuick, Qt3D, QtMultimedia, QtWebEngine, etc.
- Kept: QtCore, QtGui, QtWidgets (required for the app)
