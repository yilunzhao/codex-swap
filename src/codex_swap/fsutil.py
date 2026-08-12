"""Filesystem primitives for handling credential material.

Every write here goes through a temp file in the *destination* directory and
then ``os.replace``, which is atomic on the same filesystem. That matters more
than usual for this tool: the file being replaced is the only copy of a live
OAuth refresh token, and a half-written ``auth.json`` means a re-login.

The temp file is created with 0600 by ``mkstemp`` and chmod'ed again before the
rename, so the secret is never briefly world-readable — writing then chmod'ing
the final path would leave exactly that window.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path

from codex_swap.exceptions import UnreadableAuthError

#: Owner-only file / directory modes. No-ops on Windows, where ACLs inherited
#: from the user profile already restrict access.
FILE_MODE = 0o600
DIR_MODE = 0o700


def ensure_private_dir(path: Path) -> Path:
    """Create ``path`` (and parents) and make it owner-only."""
    path.mkdir(parents=True, exist_ok=True)
    # Windows, or a filesystem without POSIX modes: the mkdir succeeded and that
    # is what callers depend on.
    with contextlib.suppress(OSError):
        os.chmod(path, DIR_MODE)
    return path


def write_secret(path: Path, text: str) -> None:
    """Atomically write ``text`` to ``path`` with owner-only permissions."""
    ensure_private_dir(path.parent)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        with contextlib.suppress(OSError):
            os.chmod(tmp, FILE_MODE)
        os.replace(tmp, path)
        tmp = None
    finally:
        if tmp is not None and os.path.exists(tmp):
            with contextlib.suppress(OSError):
                os.unlink(tmp)


def read_text(path: Path) -> str | None:
    """Read ``path`` as UTF-8, or None when there is no file there.

    A directory is reported as "no file", not as an error, because callers use
    this to ask whether an account blob exists. The explicit ``is_dir`` check is
    load-bearing on Windows, which raises ``PermissionError`` rather than
    ``IsADirectoryError`` when a directory is opened for reading — catching
    ``PermissionError`` instead would swallow genuine permission problems on
    real files.
    """
    try:
        if path.is_dir():
            return None
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except IsADirectoryError:  # pragma: no cover - POSIX race after the is_dir check
        return None
    except UnicodeDecodeError as exc:
        # Reporting this as "absent" would send the user to `codex login` and
        # quietly discard a file that may still hold a usable refresh token.
        raise UnreadableAuthError(
            f"{path} is not valid UTF-8 ({exc.reason}); it may be truncated or "
            f"written in another encoding"
        ) from exc
    except PermissionError as exc:
        raise UnreadableAuthError(
            f"{path} cannot be read: {exc.strerror}. If it was created by `sudo codex`, "
            f"fix its ownership rather than deleting it"
        ) from exc


def is_private(path: Path) -> bool | None:
    """Whether ``path`` is owner-only. None when the question does not apply."""
    if os.name == "nt":
        return None
    try:
        mode = path.stat().st_mode & 0o777
    except OSError:
        return None
    return mode & 0o077 == 0
