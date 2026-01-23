"""
Custom hook to collect xcb platform plugin dependencies for Linux.
PyQt6 on Linux requires libxcb-cursor and other xcb libraries.
"""

from pathlib import Path

# Collect xcb-related libraries that Qt needs
binaries = []

# Common library paths on Linux
lib_paths = [
    Path("/usr/lib/x86_64-linux-gnu"),
    Path("/usr/lib64"),
    Path("/lib/x86_64-linux-gnu"),
    Path("/lib64"),
]

# Libraries needed by Qt xcb platform plugin
needed_libs = [
    "libxcb-cursor.so.*",
    "libxcb-icccm.so.*",
    "libxcb-image.so.*",
    "libxcb-keysyms.so.*",
    "libxcb-randr.so.*",
    "libxcb-render.so.*",
    "libxcb-render-util.so.*",
    "libxcb-shape.so.*",
    "libxcb-shm.so.*",
    "libxcb-sync.so.*",
    "libxcb-xfixes.so.*",
    "libxcb-xinerama.so.*",
    "libxcb-xkb.so.*",
]

for lib_path in lib_paths:
    if lib_path.exists():
        for lib_pattern in needed_libs:
            matches = list(lib_path.glob(lib_pattern))
            for match in matches:
                binaries.append((str(match), "."))
