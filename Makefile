.PHONY: help clean build-macos dmg build-linux appimage clean-linux install-deps test

help:
	@echo "FatFloppy Build Commands:"
	@echo "  make install-deps  - Install PyInstaller and dependencies"
	@echo "  make build-macos   - Build macOS .app bundle"
	@echo "  make dmg           - Build macOS DMG installer"
	@echo "  make build-linux   - Build Linux executable with PyInstaller"
	@echo "  make appimage      - Build Linux AppImage"
	@echo "  make clean         - Remove build artifacts"
	@echo "  make clean-linux   - Remove Linux-specific build artifacts"
	@echo "  make test          - Run tests"

install-deps:
	pip install -e ".[dev]"
	pip install pyinstaller

clean:
	rm -rf build dist packaging/macos/*.spec~ packaging/linux/*.spec~
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true

clean-linux:
	rm -rf build dist AppDir *.AppImage linuxdeploy-plugin-qt-*.AppImage

build-macos:
	pyinstaller packaging/macos/FatFloppy.spec

dmg:
	packaging/macos/build_macos.sh

build-linux:
	pyinstaller packaging/linux/FatFloppy.spec

appimage:
	bash packaging/linux/build_appimage.sh

test:
	pytest
