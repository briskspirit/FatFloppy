#!/bin/bash
set -e

# Navigate to project root (script is in packaging/linux/)
cd "$(dirname "$0")/../.."

ARCH=$(uname -m)
VERSION=$(python3 -c "exec(open('src/fatfloppy/_version.py').read()); print(__version__)")
APP_NAME="FatFloppy"
APPIMAGE_NAME="${APP_NAME}-${VERSION}-${ARCH}"

echo "Building ${APP_NAME} ${VERSION} AppImage for ${ARCH}..."

# Check for required tools
if ! command -v linuxdeploy &> /dev/null; then
    echo "Error: linuxdeploy not found. Please install it from:"
    echo "  https://github.com/linuxdeploy/linuxdeploy/releases"
    echo ""
    echo "Quick install:"
    echo "  wget https://github.com/linuxdeploy/linuxdeploy/releases/download/continuous/linuxdeploy-${ARCH}.AppImage"
    echo "  chmod +x linuxdeploy-${ARCH}.AppImage"
    echo "  sudo mv linuxdeploy-${ARCH}.AppImage /usr/local/bin/linuxdeploy"
    exit 1
fi

if ! command -v linuxdeploy-plugin-qt &> /dev/null; then
    echo "Warning: linuxdeploy-plugin-qt not found. Attempting to download..."
    PLUGIN_PATH="$(pwd)/linuxdeploy-plugin-qt-${ARCH}.AppImage"
    wget -q "https://github.com/linuxdeploy/linuxdeploy-plugin-qt/releases/download/continuous/linuxdeploy-plugin-qt-${ARCH}.AppImage" -O "$PLUGIN_PATH"
    chmod +x "$PLUGIN_PATH"
    export PATH="$(pwd):$PATH"
    echo "Downloaded linuxdeploy-plugin-qt to $PLUGIN_PATH"
fi

echo "Cleaning previous builds..."
rm -rf build dist AppDir *.AppImage

echo "Running PyInstaller..."
python3 -m PyInstaller packaging/linux/FatFloppy.spec

if [ ! -d "dist/fatfloppy" ]; then
    echo "Error: dist/fatfloppy directory not found"
    exit 1
fi

echo "Creating AppDir structure..."
mkdir -p AppDir/usr/bin
mkdir -p AppDir/usr/share/applications
mkdir -p AppDir/usr/share/icons/hicolor/256x256/apps
mkdir -p AppDir/usr/share/doc/fatfloppy

echo "Copying PyInstaller output to AppDir..."
cp -r dist/fatfloppy/* AppDir/usr/bin/

echo "Copying desktop file and icon..."
cp packaging/linux/fatfloppy.desktop AppDir/usr/share/applications/
cp assets/icons/fatfloppy_icon.png AppDir/usr/share/icons/hicolor/256x256/apps/fatfloppy.png

echo "Copying Greaseweazle udev rule..."
cp packaging/linux/49-greaseweazle.rules AppDir/usr/share/doc/fatfloppy/

echo "Creating AppImage with linuxdeploy..."
# Set Qt plugin to find qmake
export QML_SOURCES_PATHS=.
export QMAKE=$(which qmake6 2>/dev/null || which qmake-qt6 2>/dev/null || which qmake 2>/dev/null || echo "")

linuxdeploy \
    --appdir AppDir \
    --plugin qt \
    --executable AppDir/usr/bin/fatfloppy \
    --desktop-file packaging/linux/fatfloppy.desktop \
    --icon-file assets/icons/fatfloppy_icon.png \
    --output appimage

# Find the generated AppImage (linuxdeploy creates it with a default name)
GENERATED_APPIMAGE=$(ls -t FatFloppy-*.AppImage 2>/dev/null | head -1 || ls -t *.AppImage 2>/dev/null | head -1)

if [ -z "$GENERATED_APPIMAGE" ]; then
    echo "Error: AppImage not created"
    exit 1
fi

# Rename to our standard naming
mv "$GENERATED_APPIMAGE" "${APPIMAGE_NAME}.AppImage"

# Make sure it's executable
chmod +x "${APPIMAGE_NAME}.AppImage"

APPIMAGE_SIZE=$(du -sh "${APPIMAGE_NAME}.AppImage" | cut -f1)
DIST_SIZE=$(du -sh "dist/fatfloppy" | cut -f1)

echo ""
echo "=========================================="
echo "Build complete!"
echo "=========================================="
echo "AppImage:     ${APPIMAGE_NAME}.AppImage (${APPIMAGE_SIZE})"
echo "Dist folder:  dist/fatfloppy (${DIST_SIZE})"
echo "Architecture: ${ARCH}"
echo ""
echo "To run the AppImage:"
echo "  chmod +x ${APPIMAGE_NAME}.AppImage"
echo "  ./${APPIMAGE_NAME}.AppImage"
echo ""
echo "If you encounter FUSE errors, use:"
echo "  ./${APPIMAGE_NAME}.AppImage --appimage-extract-and-run"
echo ""
echo "To install Greaseweazle USB support:"
echo "  sudo cp AppDir/usr/share/doc/fatfloppy/49-greaseweazle.rules /etc/udev/rules.d/"
echo "  sudo udevadm control --reload-rules"
echo "  sudo udevadm trigger"
echo "=========================================="
