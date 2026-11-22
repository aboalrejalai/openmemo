"""Cross-platform advisory file locks: fcntl.flock on POSIX, msvcrt.locking on Windows.

Each function takes an open file object. The lock covers the whole file on POSIX
and its first byte on Windows, which is enough for lock files that only exist
to be locked.
"""

from __future__ import annotations

import os
import time

if os.name == "nt":
    import msvcrt

    def try_lock(fh) -> bool:
        """Take the lock without waiting. True when taken."""
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def lock(fh) -> None:
        """Wait until the lock is taken."""
        while not try_lock(fh):
            time.sleep(0.05)

    def unlock(fh) -> None:
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
else:
    import fcntl

    def try_lock(fh) -> bool:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def lock(fh) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)

    def unlock(fh) -> None:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass


def link_dir(target, source) -> None:
    """Point directory `target` at `source`: a symlink on POSIX, a junction on
    Windows (no admin rights or Developer Mode needed)."""
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(source), str(target))
    else:
        os.symlink(str(source), str(target), target_is_directory=True)
