# src/fatfloppy/gui/models.py


class FileSystemNode:
    def __init__(self, name, size=0, is_dir=False, modified="N/A", attributes="-", parent=None):
        self.name = name
        self.size = size
        self.is_dir = is_dir
        self.modified = modified
        self.attributes = attributes
        self.parent = parent
        self.children = []

    def appendChild(self, child):
        self.children.append(child)
