# src/fatfloppy/gui/models.py
"""
Data models for representing filesystem structures in the GUI.
"""

from __future__ import annotations
from typing import List, Optional


class FileSystemNode:
    """
    Represents a node in the filesystem tree, such as a file or a directory.

    This class is used to build an in-memory representation of the disk's
    directory structure, which is then used to populate the GUI's tree
    and file list widgets.
    """

    def __init__(
        self,
        name: str,
        size: int = 0,
        is_dir: bool = False,
        modified: str = "N/A",
        attributes: str = "-",
        parent: Optional[FileSystemNode] = None
    ) -> None:
        """
        Initializes a FileSystemNode.

        Args:
            name: The name of the file or directory.
            size: The size of the file in bytes (0 for directories).
            is_dir: True if the node is a directory, False otherwise.
            modified: The last modified timestamp as a string.
            attributes: Filesystem attributes as a string (e.g., "RHS").
            parent: The parent FileSystemNode, or None for the root.
        """
        self.name: str = name
        self.size: int = size
        self.is_dir: bool = is_dir
        self.modified: str = modified
        self.attributes: str = attributes
        self.parent: Optional[FileSystemNode] = parent
        self.children: List[FileSystemNode] = []

    def appendChild(self, child: FileSystemNode) -> None:
        """
        Adds a child node to this node's list of children.

        Args:
            child: The FileSystemNode to add as a child.
        """
        self.children.append(child)
