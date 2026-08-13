"""Shared fixtures, plus a hard guarantee that tests cannot touch real accounts.

Isolation here is two layers, on purpose:

1. An autouse fixture repoints ``CODEX_HOME``/``CODEX_SWAP_HOME`` at ``tmp_path``
   so every test operates on its own throwaway store.
2. A process-global :func:`sys.addaudithook` refuses any *write* whose target
   lands under the developer's REAL Codex directories.

The second layer exists because the first one unwinds. ``monkeypatch`` restores
the environment at teardown, so anything that outlives its own test (a thread,
an ``atexit`` handler, a fixture finalizer that runs late) sees the real
``$HOME`` again by the time it writes. An audit hook cannot be uninstalled
(CPython offers no removal API), which is exactly the property needed: the
layer that cannot unwind is the one protecting a file whose loss costs a
re-login.

The real roots are frozen ONCE at import time, before any fixture has touched
the environment. Re-resolving them at check time would be tautological: during
an isolated test ``paths.store_root()`` correctly points at the test's own tmp
directory, so "does this write match what paths resolves to now" is true for
legitimate writes too.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest


class RealStoreWriteBlocked(Exception):
    """A test tried to write the developer's real Codex or codex-swap data.

    Deliberately NOT an ``OSError`` subclass. ``Path.mkdir(parents=True,
    exist_ok=True)`` swallows ``OSError`` internally, so an OSError-based
    refusal into an existing protected directory would be absorbed by the very
    call shape it is meant to guard.
    """


#: A root that exists only to be attacked. The control tests aim their write and
#: delete probes here instead of at the developer's real ``~/.codex``: a probe
#: that only passes while the guard works is a probe that deletes real
#: credentials the day the guard regresses.
SENTINEL_ROOT = Path(tempfile.gettempdir()) / "codex-swap-guard-sentinel-do-not-create"


def _freeze_protected_roots() -> tuple[Path, ...]:
    """Snapshot the real Codex / codex-swap roots exactly once, at import."""
    overrides = ("CODEX_HOME", "XSWAP_HOME", "CODEX_SWAP_HOME", "XDG_DATA_HOME")
    saved = {key: os.environ.get(key) for key in overrides}

    roots: set[Path] = {SENTINEL_ROOT}

    def _collect() -> None:
        from codex_account_switcher import paths

        resolvers = (
            paths.codex_home,
            paths.store_root,
            paths.prototype_store_root,
            paths.former_store_root,
        )
        for resolver in resolvers:
            with contextlib.suppress(OSError, RuntimeError):  # pragma: no cover
                resolved = resolver()
                roots.add(Path(os.path.abspath(resolved)))
                # `abspath` does not follow symlinks, so a home directory (or a
                # ~/.codex) that is itself a link would otherwise never compare
                # equal to the path a write actually lands on.
                with contextlib.suppress(OSError):
                    roots.add(Path(resolved).resolve())

    # Both notions of "real" are protected: the genuine defaults, and whatever
    # the developer actually has exported in their shell.
    _collect()
    try:
        for key in overrides:
            os.environ.pop(key, None)
        _collect()
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    return tuple(sorted(roots))


PROTECTED_ROOTS = _freeze_protected_roots()

#: The real roots only, for the assertion that the guard covers them. Excluded
#: from the probes so that no test ever aims a delete at real credentials.
REAL_PROTECTED_ROOTS = tuple(r for r in PROTECTED_ROOTS if r != SENTINEL_ROOT)

_WRITE_MODE_CHARS = frozenset("wxa+")


def _path_of_fd(fd: int) -> str | None:
    """Best-effort path for an open descriptor.

    Needed because ``shutil.rmtree`` walks with ``dir_fd`` and deletes children
    by *relative* name, so the per-file audit events carry "auth.json" and
    nothing else. Resolving those against the process CWD, which is what
    ``abspath`` does, makes every one of them invisible to the guard.
    """
    if sys.platform == "darwin":
        try:
            import fcntl

            F_GETPATH = 50  # <sys/fcntl.h>; absent from the fcntl module
            buf = fcntl.fcntl(fd, F_GETPATH, b"\0" * 1024)
            return buf.split(b"\0", 1)[0].decode()
        except (OSError, ImportError, ValueError, UnicodeDecodeError):
            return None
    try:
        return os.readlink(f"/proc/self/fd/{fd}")
    except OSError:
        return None


def _candidate_paths(target, dir_fd=None):
    """Every filesystem path a write argument could denote."""
    if target is None:
        return
    if isinstance(target, int):
        # An fd-based call (ftruncate/fchmod); resolve it or treat as unknown.
        resolved = _path_of_fd(target)
        if resolved:
            yield Path(resolved)
        return
    try:
        # `os.fsdecode` accepts str, bytes and PathLike alike; `os.fspath`
        # raises TypeError on bytes, which the old guard swallowed into "safe".
        text = os.fsdecode(target)
    except (TypeError, ValueError):
        return

    if dir_fd is not None and not os.path.isabs(text):
        base = _path_of_fd(dir_fd)
        if base:
            yield Path(os.path.abspath(os.path.join(base, text)))
            return
        # Descriptor could not be resolved on this platform. Fall through to the
        # CWD interpretation: no worse than having no dir_fd handling at all,
        # and the front-door `shutil.rmtree` check still stands. Refusing here
        # instead would block pytest's own tmp-directory cleanup, which walks
        # with dir_fd exactly the same way.

    yield Path(os.path.abspath(text))
    with contextlib.suppress(OSError, RuntimeError):
        yield Path(text).resolve()


def _is_protected(target, dir_fd=None) -> bool:
    for path in _candidate_paths(target, dir_fd):
        if any(path == root or root in path.parents for root in PROTECTED_ROOTS):
            return True
    return False


def _audit(event: str, args: tuple) -> None:
    targets: list[tuple[object, object]] = []
    if event == "open":
        # (path, mode, flags); mode is None for os.open-style calls, where the
        # flags int carries the intent instead.
        if len(args) < 2:
            return
        mode = args[1]
        if mode is None:
            flags = args[2] if len(args) > 2 else 0
            if not isinstance(flags, int) or not (flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT)):
                return
        elif not (_WRITE_MODE_CHARS & set(str(mode))):
            return
        targets = [(args[0], None)]
    elif event in ("os.rename", "os.replace", "os.link", "os.symlink"):
        # (src, dst, src_dir_fd, dst_dir_fd); only the destination is written.
        dst_dir_fd = args[3] if len(args) > 3 else None
        targets = [(args[1] if len(args) > 1 else None, dst_dir_fd)]
    elif event in ("os.remove", "os.unlink", "os.rmdir", "os.truncate"):
        dir_fd = args[1] if len(args) > 1 else None
        targets = [(args[0] if args else None, dir_fd)]
    elif event == "os.mkdir" or event == "os.chmod":
        dir_fd = args[2] if len(args) > 2 else None
        targets = [(args[0] if args else None, dir_fd)]
    elif event == "shutil.rmtree":
        targets = [(args[0] if args else None, None)]
    elif event == "subprocess.Popen":
        # Audit hooks are per-process and children never inherit them, so any
        # subprocess is a hole in every guarantee above. The one this codebase
        # actually reaches for is `codex login`, which rewrites the developer's
        # real auth.json; tests must inject a runner instead.
        #
        # The event's shape differs by platform: POSIX passes an executable plus
        # an argv list, Windows passes executable=None and a single command-line
        # string. Both are normalised here, because handling only the POSIX shape meant
        # the guard silently did nothing on Windows.
        words: list[str] = []
        for candidate in (args[0] if args else None, args[1] if len(args) > 1 else None):
            if candidate is None:
                continue
            if isinstance(candidate, (list, tuple)):
                for item in candidate:
                    with contextlib.suppress(TypeError, ValueError):
                        words.append(os.fsdecode(item))
            else:
                with contextlib.suppress(TypeError, ValueError):
                    words.extend(os.fsdecode(candidate).split())

        # The program is whichever of the first couple of words names a binary.
        for word in words[:2]:
            if os.path.basename(word.replace("\\", "/")).lower() in ("codex", "codex.exe"):
                raise RealStoreWriteBlocked(
                    "test process tried to spawn the real `codex` binary, which would "
                    "rewrite real credentials; inject a runner instead"
                )
        for word in words:
            if _is_protected(word):
                raise RealStoreWriteBlocked(
                    f"test process tried to hand a real Codex path to a subprocess: {word!r}"
                )
        return
    else:
        return

    for target, dir_fd in targets:
        if _is_protected(target, dir_fd):
            raise RealStoreWriteBlocked(
                f"test process attempted to write real Codex data: {target!r} "
                f"(protected roots: {[str(r) for r in PROTECTED_ROOTS]})"
            )


sys.addaudithook(_audit)


# ---------------------------------------------------------------------------
# environment isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Point every path resolver at this test's own tmp directory."""
    codex_home = tmp_path / "codex-home"
    store_home = tmp_path / "codex-swap-store"
    fake_home = tmp_path / "home"
    for directory in (codex_home, store_home, fake_home):
        directory.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("XSWAP_HOME", str(store_home))
    monkeypatch.delenv("CODEX_SWAP_HOME", raising=False)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    from codex_account_switcher import printer

    printer.set_color(False)
    yield
    printer.set_color(None)


# ---------------------------------------------------------------------------
# auth blob factory
# ---------------------------------------------------------------------------


def _b64(payload: dict) -> str:
    raw = json.dumps(payload).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def make_id_token(
    email: str = "alice@example.com",
    *,
    plan: str = "pro",
    account_id: str = "acct-alice",
    expires_in: int = 3600,
) -> str:
    """A structurally valid, unsigned JWT carrying the claims Codex reads."""
    header = _b64({"alg": "RS256", "typ": "JWT"})
    payload = _b64(
        {
            "email": email,
            "exp": int(time.time()) + expires_in,
            "https://api.openai.com/auth": {
                "chatgpt_plan_type": plan,
                "chatgpt_account_id": account_id,
            },
        }
    )
    return f"{header}.{payload}.not-a-real-signature"


def make_auth(
    email: str = "alice@example.com",
    *,
    plan: str = "pro",
    account_id: str = "acct-alice",
    expires_in: int = 3600,
    access_token: str | None = None,
    last_refresh: str = "2026-08-08T17:33:16.621745Z",
) -> str:
    """Serialised ``auth.json`` for a ChatGPT-mode login."""
    return json.dumps(
        {
            "auth_mode": "chatgpt",
            "OPENAI_API_KEY": None,
            "tokens": {
                "id_token": make_id_token(
                    email, plan=plan, account_id=account_id, expires_in=expires_in
                ),
                "access_token": access_token or f"at.{email}",
                "refresh_token": f"rt.{email}",
                "account_id": account_id,
            },
            "last_refresh": last_refresh,
        },
        indent=2,
    )


def make_api_key_auth(key: str = "sk-proj-abcdef123456") -> str:
    """Serialised ``auth.json`` for an API-key login (no tokens at all)."""
    return json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": key, "tokens": None})


@pytest.fixture
def auth_factory():
    return make_auth


@pytest.fixture
def store(tmp_path):
    from codex_account_switcher.store import AccountStore

    return AccountStore(tmp_path / "codex-swap-store")


@pytest.fixture
def switcher(store):
    from codex_account_switcher.paths import auth_path
    from codex_account_switcher.switcher import Switcher

    return Switcher(store=store, auth_file=auth_path())


@pytest.fixture
def live_auth():
    """Write a blob to the live auth.json path and return its text."""
    from codex_account_switcher.fsutil import write_secret
    from codex_account_switcher.paths import auth_path

    def _write(text: str) -> str:
        write_secret(auth_path(), text)
        return text

    return _write
