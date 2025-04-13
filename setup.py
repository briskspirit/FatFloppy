# setup.py
from setuptools import setup, find_packages

setup(
    name="FatFloppy",
    version="0.1.0",
    packages=find_packages(where="src"),
    package_dir={"": "src"},
    scripts=["scripts/fatfloppy"],
    install_requires=[
        "greaseweazle",
        "pyqt6",
    ],
    python_requires=">=3.6",
    author="Igor Biletski",
    author_email="bs@privacy.im",
    description="Floppy disk browser and utility using Greaseweazle",
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
    ],
    include_package_data=True,
    package_data={
        "": ["assets/icons/*.png"],
    },
)
