"""Path resolution for the Codex config and the codex-swap store.

Codex keeps all of its authentication state in a single file,
``$CODEX_HOME/auth.json`` (``~/.codex/auth.json`` by default) — there is no
keychain involvement on any platform, which is what makes swapping accounts a
matter of replacing one file.

The codex-swap store lives separately so that ``codex logout`` and Codex
upgrades cannot touch it:

* ``$CODEX_SWAP_HOME`` when set (absolute paths only),
* else ``$XDG_DATA_HOME/codex-swap`` on Linux/WSL (default
  ``~/.local/share/codex-swap``), per the XDG Base Directory Specification,
* else ``~/.codex-swap`` on macOS/Windows, where XDG is not a convention.

References:
- Codex CLI ``CODEX_HOME`` resolution (codex-rs/core/src/config.rs)
- XDG Base Directory Specification
  https://specifications.freedesktop.org/basedir-spec/basedir-spec-latest.html
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from codex_swap.exceptions import StoreError
from codex_swap.locking import FileLock
from codex_swap.models import Platform

#: Store layout written by the pre-1.0 single-file prototype. Migrated once,
#: on first run, so early users keep their accounts.
LEGACY_STORE_DIRNAME = ".codex-swap-backup"


def codex_home() -> Path:
    """Resolve ``CODEX_HOME`` the way the Codex CLI does."""
    env = os.environ.get("CODEX_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".codex"


def auth_path() -> Path:
    """The live Codex credential file that a swap replaces."""
    return codex_home() / "auth.json"


def config_path() -> Path:
    """Codex's TOML config. Reported by ``doctor``; never read or modified.

    Its presence is worth knowing when diagnosing a Codex that behaves
    differently after a swap, but nothing here parses it — a swap only ever
    touches ``auth.json``.
    """
    return codex_home() / "config.toml"


def legacy_store_root() -> Path:
    return Path.home() / LEGACY_STORE_DIRNAME


def store_root() -> Path:
    """Directory holding the account store for the current platform."""
    env = os.environ.get("CODEX_SWAP_HOME")
    if env:
        # An empty or relative value is ignored rather than silently creating a
        # store under the current working directory.
        candidate = Path(os.path.expanduser(env))
        if candidate.is_absolute():
            return candidate

    if Platform.detect() in (Platform.LINUX, Platform.WSL):
        xdg = os.environ.get("XDG_DATA_HOME", "")
        if xdg:
            # The spec says to ignore XDG_DATA_HOME unless it is absolute. The
            # `~` expansion covers systemd units and Dockerfiles, which set the
            # variable without a shell to expand it.
            xdg_path = Path(os.path.expanduser(xdg))
            if xdg_path.is_absolute():
                return xdg_path / "codex-swap"
        return Path.home() / ".local" / "share" / "codex-swap"

    return Path.home() / ".codex-swap"


def slugify(email: str) -> str:
    """Filename-safe rendering of an email for the on-disk blob name.

    Kept readable on purpose: the store is meant to be inspectable with ``ls``
    when something has gone wrong.
    """
    safe = "".join(c if (c.isalnum() or c in "._@+-") else "_" for c in email)
    return safe or "unknown"


# Artifacts any prior run may have created without user data being present. A
# target holding only these is treated as empty, since wiping them loses
# nothing.
_THROWAWAY_NAMES = frozenset({"cache"})
_THROWAWAY_PREFIXES = ("codex-swap.log",)


def _has_meaningful_data(target: Path) -> bool:
    try:
        entries = list(target.iterdir())
    except (FileNotFoundError, NotADirectoryError):
        return False
    for entry in entries:
        if entry.name in _THROWAWAY_NAMES:
            continue
        if any(entry.name.startswith(p) for p in _THROWAWAY_PREFIXES):
            continue
        return True
    return False


def _clear(target: Path) -> None:
    for entry in target.iterdir():
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    target.rmdir()


#: The prototype kept credential blobs in ``auth/``; the package uses
#: ``accounts/``. Moving the directory is not enough — without this rename every
#: migrated account would report "missing credentials".
LEGACY_ACCOUNTS_DIRNAME = "auth"
ACCOUNTS_DIRNAME = "accounts"


def _rename_legacy_accounts_dir(target: Path) -> None:
    old = target / LEGACY_ACCOUNTS_DIRNAME
    new = target / ACCOUNTS_DIRNAME
    if old.is_dir() and not new.exists():
        old.rename(new)


def migrate_legacy_store(target: Path | None = None) -> bool:
    """Move the prototype's ``~/.codex-swap-backup`` to the real store root.

    Guarded by a ``<target>.migrating`` flag touched before the move and removed
    after, which is what lets an interrupted migration be told apart from a
    genuine collision on the next run:

    * flag present, legacy still there  -> resume (discard partial target, retry)
    * flag present, legacy gone         -> previous run finished, drop the flag
    * no flag, both exist               -> genuine collision, refuse, unless the
      target holds only throwaway artifacts

    Returns True only if a move happened in this call.
    """
    target = target or store_root()
    legacy = legacy_store_root()

    try:
        if legacy.resolve() == target.resolve():
            return False
    except OSError:
        if legacy == target:
            return False

    flag = target.parent / f".{target.name}.migrating"

    if not legacy.exists():
        flag.unlink(missing_ok=True)
        return False

    # One lock for the whole check-and-move. `migrate_legacy_store` runs before
    # the store (and its own lock) exists, so two first runs racing here could
    # otherwise both pass `legacy.exists()` and have one delete what the other
    # just migrated. The lock lives beside the store, not inside it, because the
    # store directory is the thing being replaced.
    with FileLock(target.parent / f".{target.name}.migrate.lock"):
        if not legacy.exists():
            flag.unlink(missing_ok=True)
            return False
        try:
            # A target holding real data is never discarded, flag or no flag. An
            # interrupted cross-filesystem move leaves the *legacy* copy partial
            # and the target complete, so treating "flag present" as "target is
            # garbage" would delete the good copy.
            if target.exists() and _has_meaningful_data(target):
                raise StoreError(
                    f"Both the legacy store ({legacy}) and the current one ({target}) "
                    f"hold data. Refusing to merge — inspect both and remove the stale "
                    f"one before re-running."
                )
            if target.exists():
                _clear(target)

            target.parent.mkdir(parents=True, exist_ok=True)
            flag.touch()
            shutil.move(str(legacy), str(target))
            _rename_legacy_accounts_dir(target)
            flag.unlink(missing_ok=True)
        except OSError as exc:
            raise StoreError(f"Migration of {legacy} -> {target} failed: {exc}") from exc

    return True
