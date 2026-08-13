"""Path resolution for the Codex config and this tool's account store.

Codex keeps all of its authentication state in a single file,
``$CODEX_HOME/auth.json`` (``~/.codex/auth.json`` by default). There is no
keychain involvement on any platform, which is what makes swapping accounts a
matter of replacing one file.

The account store lives separately, so that ``codex logout`` and Codex upgrades
cannot touch it:

* ``$XSWAP_HOME`` when set (absolute paths only),
* else ``$XDG_DATA_HOME/xswap`` on Linux/WSL (default ``~/.local/share/xswap``),
  per the XDG Base Directory Specification,
* else ``~/.xswap`` on macOS/Windows, where XDG is not a convention.

Two older roots are migrated once, on first run:

* ``~/.codex-swap-backup``, written by the pre-1.0 single-file prototype.
* ``~/.codex-swap``, used up to 0.2.0. An unrelated PyPI project named
  ``codex-swap`` also stores accounts there, in a different layout, so that
  migration is gated on recognising *this* tool's own file naming. Moving
  another program's credentials would be exactly the kind of silent loss the
  rest of this module exists to prevent.

References:
- Codex CLI ``CODEX_HOME`` resolution (codex-rs/core/src/config.rs)
- XDG Base Directory Specification
  https://specifications.freedesktop.org/basedir-spec/basedir-spec-latest.html
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from codex_account_switcher.exceptions import StoreError
from codex_account_switcher.locking import FileLock
from codex_account_switcher.models import Platform

#: Store layout written by the pre-1.0 single-file prototype.
PROTOTYPE_STORE_DIRNAME = ".codex-swap-backup"

#: Store root used up to 0.2.0, before the rename away from the taken
#: ``codex-swap`` name.
FORMER_STORE_DIRNAME = ".codex-swap"

#: Kept working for anyone who exported it against an older version.
LEGACY_HOME_ENV = "CODEX_SWAP_HOME"

#: Blob naming this tool has always used: ``auth-<slot>-<email>.json``, flat
#: inside ``accounts/``. The unrelated ``codex-swap`` uses
#: ``accounts/<slot>/auth.json`` instead, which is what makes the two
#: distinguishable on disk.
_OUR_BLOB_RE = re.compile(r"^auth-\d+-.+\.json$")


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
    differently after a swap, but nothing here parses it. A swap only ever
    touches ``auth.json``.
    """
    return codex_home() / "config.toml"


def prototype_store_root() -> Path:
    return Path.home() / PROTOTYPE_STORE_DIRNAME


def former_store_root() -> Path:
    return Path.home() / FORMER_STORE_DIRNAME


def store_root() -> Path:
    """Directory holding the account store for the current platform."""
    for name in ("XSWAP_HOME", LEGACY_HOME_ENV):
        env = os.environ.get(name)
        if not env:
            continue
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
                return xdg_path / "xswap"
        return Path.home() / ".local" / "share" / "xswap"

    return Path.home() / ".xswap"


def holds_our_layout(root: Path) -> bool:
    """Whether ``root`` was written by *this* tool rather than a namesake.

    True only for a store that is recognisably ours: our flat
    ``accounts/auth-<slot>-<email>.json`` blobs, or a bare ``sequence.json``
    with no ``accounts`` directory at all (an empty store of ours). A store with
    ``accounts/<slot>/`` sub-directories belongs to the unrelated ``codex-swap``
    project and is left strictly alone.
    """
    accounts = root / "accounts"
    if accounts.is_dir():
        entries = list(accounts.iterdir())
        if any(entry.is_dir() for entry in entries):
            return False
        return any(_OUR_BLOB_RE.match(entry.name) for entry in entries)
    return (root / "sequence.json").exists()


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
_THROWAWAY_PREFIXES = ("xswap.log", "codex-swap.log")


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
#: ``accounts/``. Moving the directory is not enough: without this rename every
#: migrated account would report "missing credentials".
LEGACY_ACCOUNTS_DIRNAME = "auth"
ACCOUNTS_DIRNAME = "accounts"


def _rename_legacy_accounts_dir(target: Path) -> None:
    old = target / LEGACY_ACCOUNTS_DIRNAME
    new = target / ACCOUNTS_DIRNAME
    if old.is_dir() and not new.exists():
        old.rename(new)


def _migrate_one(legacy: Path, target: Path) -> bool:
    """Move ``legacy`` onto ``target``. Returns True if a move happened here.

    Guarded by a ``<target>.migrating`` flag touched before the move and removed
    after, which is what lets an interrupted migration be told apart from a
    genuine collision on the next run.
    """
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

    # One lock for the whole check-and-move. Migration runs before the store
    # (and its own lock) exists, so two first runs racing here could otherwise
    # both pass `legacy.exists()` and have one delete what the other just
    # migrated. The lock lives beside the store, not inside it, because the
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
                    f"Both the old store ({legacy}) and the current one ({target}) "
                    f"hold data. Refusing to merge; inspect both and remove the stale "
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


def migrate_legacy_store(target: Path | None = None) -> bool:
    """Bring any older store forward to ``target``. True if anything moved.

    Two roots are considered, newest first:

    * ``~/.codex-swap`` (used up to 0.2.0) **only when it holds this tool's own
      layout**. An unrelated PyPI project of the same former name keeps its
      accounts in that directory too, in a ``accounts/<slot>/auth.json`` shape;
      moving those would be stealing another program's credentials.
    * ``~/.codex-swap-backup``, written by the pre-1.0 prototype.
    """
    target = target or store_root()
    moved = False

    # A former root that is not ours belongs to the namesake project: leave it
    # untouched, and do not report it as a migration.
    former = former_store_root()
    if former.exists() and former != target and holds_our_layout(former):
        moved = _migrate_one(former, target) or moved

    moved = _migrate_one(prototype_store_root(), target) or moved
    return moved
