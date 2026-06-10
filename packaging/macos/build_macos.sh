#!/bin/bash
set -e

# Navigate to project root (script is in packaging/macos/)
cd "$(dirname "$0")/../.."

ARCH=$(uname -m)
VERSION=$(python3 -c "exec(open('src/fatfloppy/_version.py').read()); print(__version__)")
APP_NAME="FatFloppy"
DMG_NAME="${APP_NAME}-${VERSION}-macOS-${ARCH}"
DEREFERENCE_SYMLINKS=${DEREFERENCE_SYMLINKS:-true}

echo "Building ${APP_NAME} ${VERSION} for ${ARCH}..."

echo "Cleaning previous builds..."
rm -rf build dist

echo "Running PyInstaller..."
python3 -m PyInstaller packaging/macos/FatFloppy.spec

if [ ! -d "dist/${APP_NAME}.app" ]; then
    echo "Error: ${APP_NAME}.app not found in dist/"
    exit 1
fi

echo "Creating DMG..."
mkdir -p dist/dmg

if [ "$DEREFERENCE_SYMLINKS" = "true" ]; then
    echo "Dereferencing symlinks to match installed size..."

    # Remove broken symlinks first (from excluded Qt frameworks)
    echo "Cleaning up broken symlinks..."
    find "dist/${APP_NAME}.app" -type l ! -exec test -e {} \; -delete 2>/dev/null || true

    # Copy and dereference symlinks so DMG size matches installed size
    rsync -aL "dist/${APP_NAME}.app/" "dist/dmg/${APP_NAME}.app/"
else
    echo "Preserving symlinks (smaller DMG, larger when installed)..."
    # Preserve symlinks (smaller DMG but will expand when copied)
    rsync -a "dist/${APP_NAME}.app/" "dist/dmg/${APP_NAME}.app/"
fi

ln -s /Applications dist/dmg/Applications

# Create DMG with symlinks preserved
hdiutil create -volname "${APP_NAME}" \
    -srcfolder dist/dmg \
    -ov -format UDZO \
    "dist/${DMG_NAME}.dmg"

rm -rf dist/dmg

DMG_SIZE=$(du -sh "dist/${DMG_NAME}.dmg" | cut -f1)
APP_SIZE=$(du -sh "dist/${APP_NAME}.app" | cut -f1)

echo ""
echo "=========================================="
echo "Build complete!"
echo "=========================================="
echo "DMG:          dist/${DMG_NAME}.dmg (${DMG_SIZE})"
echo "App bundle:   dist/${APP_NAME}.app (${APP_SIZE})"
echo "Architecture: ${ARCH}"
echo ""
if [ "$DEREFERENCE_SYMLINKS" = "true" ]; then
    echo "Note: DMG and installed sizes should match (~${DMG_SIZE})"
else
    echo "Note: Installed app will be larger due to symlink dereferencing"
fi
echo ""
if [ "$ARCH" = "arm64" ]; then
    echo "This build is for Apple Silicon Macs (M1/M2/M3/M4)."
    echo "It will also run on Intel Macs via Rosetta 2."
else
    echo "This build is for Intel Macs (x86_64)."
    echo "It will also run on Apple Silicon via Rosetta 2."
fi
echo ""
echo "For complete release, build both architectures:"
echo "  - Build arm64 on Apple Silicon Mac or GitHub Actions (macos-14)"
echo "  - Build x86_64 on Intel Mac or GitHub Actions (Rosetta on macos-14)"
echo "=========================================="
