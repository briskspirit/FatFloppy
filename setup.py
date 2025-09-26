# setup.py
"""
Setup script for the FatFloppy package.

This script uses setuptools to package and distribute the FatFloppy application,
a graphical utility for interacting with vintage floppy disks via Greaseweazle
hardware. It defines the project's metadata, dependencies, and entry points.
"""

from setuptools import find_packages, setup

try:
    from src.fatfloppy._version import __version__ as version
except ImportError:
    version = "0.0.0.dev0"
    print(f"Warning: Could not import version, falling back to {version}")

try:
    with open("README.md", "r", encoding="utf-8") as fh:
        long_description = fh.read()
except FileNotFoundError:
    long_description = "A modern GUI for browsing and managing vintage floppy disks using Greaseweazle."


setup(
    # --- Project Metadata ---
    name="FatFloppy",
    version=version,
    author="Igor Biletski",
    author_email="bs@privacy.im",
    description="A modern GUI for browsing and managing vintage floppy disks using Greaseweazle.",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/briskspirit/fatfloppy",
    license="MIT",

    # --- Package Configuration ---
    package_dir={"": "src"},
    packages=find_packages(where="src"),
    include_package_data=True,
    package_data={
        "fatfloppy": ["assets/icons/*.png"],
    },

    # --- Dependencies ---
    python_requires=">=3.6",
    install_requires=[
        "greaseweazle",
        "PyQt6",
    ],

    # --- Entry Points ---
    entry_points={
        "gui_scripts": [
            "fatfloppy=fatfloppy.main:main",
        ],
    },

    # --- Classifiers ---
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: End Users/Desktop",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
        "Operating System :: Microsoft :: Windows",
        "Operating System :: POSIX :: Linux",
        "Operating System :: MacOS",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.6",
        "Programming Language :: Python :: 3.7",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Topic :: System :: Filesystems",
        "Topic :: Utilities",
        "Environment :: X11 Applications :: Qt",
        "Environment :: MacOS X :: Cocoa",
        "Environment :: Win32 (MS Windows)",
    ],
)
