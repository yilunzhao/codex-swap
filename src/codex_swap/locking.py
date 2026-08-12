"""Cross-platform advisory lock around store mutations.

Two codex-swap processes racing on a swap could interleave "back up the live
blob" with "overwrite the live blob" and lose an account's refresh token, so
every mutating command runs under this lock.

POSIX uses ``fcntl.flock``; Windows uses ``msvcrt.locking``. Both are advisory
and both release automatically if the holding process dies, which matters more
than strict correctness here: a stale lock file left by a crashed run must never
wedge the tool permanently.
"""

from __future__ import annotations

import contextlib
import os
import time
from pathlib import Path
from types import TracebackType

from codex_swap.exceptions import LockTimeout

if os.name == "nt":  # pragma: no cover - exercised on the Windows CI job
    import msvcrt

    def _try_acquire(fh) -> bool:
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _release(fh) -> None:
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass

else:
    import fcntl

    def _try_acquire(fh) -> bool:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _release(fh) -> None:
        with contextlib.suppress(OSError):
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


DEFAULT_TIMEOUT = 15.0
_POLL_INTERVAL = 0.05


class FileLock:
    """Exclusive advisory lock on ``path``, usable as a context manager."""

    def __init__(self, path: Path | str, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.path = Path(path)
        self.timeout = timeout
        self._fh = None

    def acquire(self) -> FileLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # "a+" so the file is created if absent and never truncated if a
        # concurrent holder is mid-write. Deliberately not a `with` block: the
        # handle must stay open for as long as the lock is held, and is closed
        # in `release`.
        fh = open(self.path, "a+")  # noqa: SIM115
        deadline = time.monotonic() + self.timeout
        while True:
            if _try_acquire(fh):
                self._fh = fh
                return self
            if time.monotonic() >= deadline:
                fh.close()
                raise LockTimeout(
                    f"another codex-swap operation is in progress (lock held on {self.path})"
                )
            time.sleep(_POLL_INTERVAL)

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            _release(self._fh)
        finally:
            self._fh.close()
            self._fh = None

    def __enter__(self) -> FileLock:
        return self.acquire()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        self.release()
        return False
