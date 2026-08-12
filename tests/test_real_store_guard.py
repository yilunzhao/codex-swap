"""The test process must be structurally unable to write real Codex data.

These are the control tests for the audit hook installed in ``conftest``. They
matter because the fixture-based isolation around them unwinds at teardown: a
thread that outlives its own test sees the real ``$HOME`` again, and a guard
that also unwinds would already be gone by then.
"""

from __future__ import annotations

import os
import shutil
import threading
from pathlib import Path

import pytest

from tests import conftest


def test_protected_roots_were_frozen():
    """The guard is meaningless if it protects nothing."""
    assert conftest.PROTECTED_ROOTS, "no real roots were frozen at import time"
    names = {p.name for p in conftest.PROTECTED_ROOTS}
    assert ".codex" in names or any("codex" in p.name for p in conftest.PROTECTED_ROOTS)


def test_control_a_tmp_writes_are_allowed(tmp_path: Path):
    """CONTROL A: the guard must not block everything."""
    target = tmp_path / "allowed.json"
    target.write_text("{}", encoding="utf-8")
    assert target.read_text(encoding="utf-8") == "{}"


def test_control_b_real_root_write_is_refused():
    """CONTROL B: a direct write under a real root is refused, in-thread."""
    victim = conftest.PROTECTED_ROOTS[0] / "codex-swap-guard-probe.json"
    with pytest.raises(conftest.RealStoreWriteBlocked), open(victim, "w", encoding="utf-8") as fh:
        fh.write("should never land")
    assert not victim.exists()


def test_control_c_thread_outliving_isolation_is_refused():
    """CONTROL C: the case that actually matters — a background thread.

    ``Path.home`` and ``os.environ`` are process-global, so a thread running
    after its test's monkeypatch unwound would see the real home. The audit
    hook is global too, which is why it still catches this.
    """
    victim = conftest.PROTECTED_ROOTS[0] / "codex-swap-guard-thread-probe.json"
    outcome: list[BaseException | None] = []

    def writer() -> None:
        try:
            with open(victim, "w", encoding="utf-8") as fh:
                fh.write("should never land")
            outcome.append(None)
        except BaseException as exc:
            outcome.append(exc)

    thread = threading.Thread(target=writer)
    thread.start()
    thread.join(timeout=5)

    assert outcome, "writer thread did not finish"
    assert isinstance(outcome[0], conftest.RealStoreWriteBlocked)
    assert not victim.exists()


def test_guard_refuses_rename_into_a_real_root(tmp_path: Path):
    """Atomic writes land via os.replace, so the rename path needs covering."""
    source = tmp_path / "staged.json"
    source.write_text("{}", encoding="utf-8")
    victim = conftest.PROTECTED_ROOTS[0] / "codex-swap-guard-rename-probe.json"
    with pytest.raises(conftest.RealStoreWriteBlocked):
        os.replace(source, victim)
    assert not victim.exists()


def test_guard_refuses_rmtree_of_a_real_root():
    """`purge` calls rmtree; pointed at a real root it must be refused."""
    with pytest.raises(conftest.RealStoreWriteBlocked):
        shutil.rmtree(conftest.PROTECTED_ROOTS[0])


def test_guard_is_not_an_oserror():
    """`mkdir(exist_ok=True)` swallows OSError, so the refusal must not be one."""
    assert not issubclass(conftest.RealStoreWriteBlocked, OSError)


def test_isolated_env_points_away_from_real_paths():
    """The first isolation layer is doing its job too."""
    from codex_swap import paths

    for resolved in (paths.codex_home(), paths.store_root()):
        assert not any(
            resolved == root or root in resolved.parents for root in conftest.PROTECTED_ROOTS
        ), f"{resolved} resolves inside a protected root"
