"""File locking shared by the dependency-light launcher and runtime."""

import errno
import os
import sys
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def file_lock(path: Path, *, busy_error: type[Exception] = BlockingIOError):
    """A nonblocking OS lock, released automatically when its process exits."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as f:
        if os.fstat(f.fileno()).st_size == 0:
            f.write(b"0")
            f.flush()
        f.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN):
                raise
            raise busy_error("another operation is already running") from exc
        try:
            yield
        finally:
            f.seek(0)
            if sys.platform == "win32":
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(f, fcntl.LOCK_UN)
