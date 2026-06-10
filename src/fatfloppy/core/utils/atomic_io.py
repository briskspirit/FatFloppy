"""
Crash-safe file writing helpers.

Disk-image drivers persist the whole image on flush. Writing in place (open the
original with "wb" and rewrite it) means a crash, kill, or ENOSPC partway through
leaves the user's only copy truncated or empty. ``atomic_write`` instead writes a
temporary file in the same directory, fsyncs it, and atomically renames it over
the target, so the original is never left in a partially written state.
"""

import contextlib
import os
import tempfile
from pathlib import Path


def atomic_write(file_path: str, data: bytes) -> None:
    """
    Atomically writes ``data`` to ``file_path``.

    The data is written to a temporary file in the same directory, flushed to
    stable storage with ``os.fsync``, and then atomically renamed over the
    target. On any failure the temporary file is removed and the original file
    (if any) is left untouched.

    Args:
        file_path: Destination path.
        data: Bytes to write.

    Raises:
        OSError: If the data cannot be written or the rename fails.
    """
    target = Path(file_path)

    # Special files (character devices like /dev/null, FIFOs, etc.) cannot be
    # atomically replaced - a rename would swap the node for a regular file.
    # Write to them directly; there is nothing to protect.
    if target.exists() and not target.is_file():
        with target.open("wb") as f:
            f.write(data)
        return

    fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp"
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        tmp_path.replace(target)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise
