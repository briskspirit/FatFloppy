.PHONY: help clean build-macos dmg install-deps test

help:
	@echo "FatFloppy Build Commands:"
	@echo "  make install-deps  - Install PyInstaller and dependencies"
	@echo "  make build-macos   - Build macOS .app bundle"
	@echo "  make dmg           - Build macOS DMG installer"
	@echo "  make clean         - Remove build artifacts"
	@echo "  make test          - Run tests"

install-deps:
	pip install -e ".[dev]"
	pip install pyinstaller

clean:
	rm -rf build dist packaging/macos/*.spec~
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true

build-macos:
	pyinstaller packaging/macos/FatFloppy.spec

dmg:
	packaging/macos/build_macos.sh

test:
	pytest
