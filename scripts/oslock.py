"""Portability helpers: file locks (fcntl.flock on POSIX, msvcrt.locking on Windows),
directory links, and a local HTTP server that never waits on DNS.

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


def local_http_server(address, handler):
    """A ThreadingHTTPServer that binds without a reverse DNS lookup.

    http.server calls socket.getfqdn() in server_bind, which can hang for a
    long time on machines with slow or broken DNS (seen on macOS CI runners
    and behind VPNs), so a local server would not answer at all.
    """
    import socketserver
    from http.server import ThreadingHTTPServer

    class _Server(ThreadingHTTPServer):
        # On Windows SO_REUSEADDR lets a second process bind the same port, so a
        # second worker would start; ask for an exclusive bind there instead.
        allow_reuse_address = os.name != "nt"

        def server_bind(self):
            if os.name == "nt":
                import socket
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            socketserver.TCPServer.server_bind(self)
            host, port = self.server_address[:2]
            self.server_name, self.server_port = str(host), port

    return _Server(address, handler)
