# macOS Packaging

This directory contains everything needed to build FatFloppy for macOS.

## Files

- `FatFloppy.spec` - PyInstaller specification
- `build_macos.sh` - Build script that creates DMG
- `entitlements.plist` - macOS entitlements for USB access
- `qt.conf` - Qt plugin configuration
- `hooks/` - Custom PyInstaller hooks for PyQt6

## Building

From the project root:

```bash
# Quick build
make dmg

# Or manually
packaging/macos/build_macos.sh
```

## Output

- `dist/FatFloppy.app` - Application bundle (~101MB with symlinks)
- `dist/FatFloppy-{version}-macOS-{arch}.dmg` - Installer (~181MB)

## Architecture

The build is architecture-specific (arm64 or x86_64). For both:
- Build on Apple Silicon Mac → arm64
- Build on Intel Mac → x86_64
- Use GitHub Actions → both automatically

## Optimization

The spec file excludes unused Qt frameworks to reduce size by 57%:
- Removed: QtQml, QtQuick, Qt3D, QtMultimedia, QtWebEngine, etc.
- Kept: QtCore, QtGui, QtWidgets (required for the app)
