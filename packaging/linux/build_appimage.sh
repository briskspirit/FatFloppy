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

# Qt plugin not needed - PyInstaller already bundles all Qt dependencies

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

# Resize icon to 256x256 (linuxdeploy only accepts standard icon sizes)
if command -v convert &> /dev/null; then
    convert assets/icons/fatfloppy_icon.png -resize 256x256 AppDir/usr/share/icons/hicolor/256x256/apps/fatfloppy.png
elif command -v magick &> /dev/null; then
    magick assets/icons/fatfloppy_icon.png -resize 256x256 AppDir/usr/share/icons/hicolor/256x256/apps/fatfloppy.png
else
    echo "Warning: ImageMagick not found, attempting to use original icon..."
    cp assets/icons/fatfloppy_icon.png AppDir/usr/share/icons/hicolor/256x256/apps/fatfloppy.png
fi

echo "Copying Greaseweazle udev rule..."
cp packaging/linux/49-greaseweazle.rules AppDir/usr/share/doc/fatfloppy/

echo "Setting up AppImage icon..."
# Copy icon to AppDir root as .DirIcon for file manager display
cp AppDir/usr/share/icons/hicolor/256x256/apps/fatfloppy.png AppDir/.DirIcon

echo "Creating AppImage with linuxdeploy..."
# PyInstaller already bundled all Qt dependencies, so we don't need the Qt plugin
# We need to tell linuxdeploy about the Qt libraries so it can bundle xcb dependencies

# Deploy Qt libraries explicitly so linuxdeploy can find xcb dependencies
QT_LIBS=""
for lib in AppDir/usr/bin/PyQt6/Qt6/lib/libQt6*.so.6; do
    if [ -f "$lib" ]; then
        QT_LIBS="$QT_LIBS --library $lib"
    fi
done

linuxdeploy \
    --appdir AppDir \
    --executable AppDir/usr/bin/fatfloppy \
    --desktop-file packaging/linux/fatfloppy.desktop \
    --icon-file AppDir/usr/share/icons/hicolor/256x256/apps/fatfloppy.png \
    $QT_LIBS \
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
