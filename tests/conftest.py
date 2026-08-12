"""Shared fixtures, plus a hard guarantee that tests cannot touch real accounts.

Isolation here is two layers, on purpose:

1. An autouse fixture repoints ``CODEX_HOME``/``CODEX_SWAP_HOME`` at ``tmp_path``
   so every test operates on its own throwaway store.
2. A process-global :func:`sys.addaudithook` refuses any *write* whose target
   lands under the developer's REAL Codex directories.

The second layer exists because the first one unwinds. ``monkeypatch`` restores
the environment at teardown, so anything that outlives its own test — a thread,
an ``atexit`` handler, a fixture finalizer that runs late — sees the real
``$HOME`` again by the time it writes. An audit hook cannot be uninstalled
(CPython offers no removal API), which is exactly the property needed: the
layer that cannot unwind is the one protecting a file whose loss costs a
re-login.

The real roots are frozen ONCE at import time, before any fixture has touched
the environment. Re-resolving them at check time would be tautological — during
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


def _freeze_protected_roots() -> tuple[Path, ...]:
    """Snapshot the real Codex / codex-swap roots exactly once, at import."""
    overrides = ("CODEX_HOME", "CODEX_SWAP_HOME", "XDG_DATA_HOME")
    saved = {key: os.environ.get(key) for key in overrides}

    roots: set[Path] = set()

    def _collect() -> None:
        from codex_swap import paths

        for resolver in (paths.codex_home, paths.store_root, paths.legacy_store_root):
            with contextlib.suppress(OSError, RuntimeError):  # pragma: no cover
                roots.add(Path(os.path.abspath(resolver())))

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

_WRITE_MODE_CHARS = frozenset("wxa+")


def _is_protected(target) -> bool:
    if target is None:
        return False
    try:
        path = Path(os.path.abspath(os.fspath(target)))
    except (TypeError, ValueError):
        return False
    return any(path == root or root in path.parents for root in PROTECTED_ROOTS)


def _audit(event: str, args: tuple) -> None:
    target = None
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
        target = args[0]
    elif event in ("os.rename", "os.replace", "os.link", "os.symlink"):
        target = args[1] if len(args) > 1 else None
    elif event in (
        "os.remove",
        "os.unlink",
        "os.mkdir",
        "os.rmdir",
        "os.truncate",
        "os.chmod",
        "shutil.rmtree",
    ):
        target = args[0] if args else None
    else:
        return

    if _is_protected(target):
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
    monkeypatch.setenv("CODEX_SWAP_HOME", str(store_home))
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    from codex_swap import printer

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
    from codex_swap.store import AccountStore

    return AccountStore(tmp_path / "codex-swap-store")


@pytest.fixture
def switcher(store):
    from codex_swap.paths import auth_path
    from codex_swap.switcher import Switcher

    return Switcher(store=store, auth_file=auth_path())


@pytest.fixture
def live_auth():
    """Write a blob to the live auth.json path and return its text."""
    from codex_swap.fsutil import write_secret
    from codex_swap.paths import auth_path

    def _write(text: str) -> str:
        write_secret(auth_path(), text)
        return text

    return _write
