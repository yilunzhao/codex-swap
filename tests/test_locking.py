"""The advisory lock that serialises store mutations."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import threading
import time

import pytest

from codex_account_switcher.exceptions import LockTimeout
from codex_account_switcher.locking import FileLock

#: Import path for the subprocess helpers below, which run outside pytest and so
#: do not inherit the `pythonpath` setting from pyproject.toml.
SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")


def test_acquire_and_release(tmp_path):
    lock = FileLock(tmp_path / ".lock")
    with lock:
        assert (tmp_path / ".lock").exists()
    # Releasing twice must be safe: `release` is called from __exit__ and may
    # also be called explicitly by a caller unwinding its own error path.
    lock.release()


def test_creates_missing_parent_directories(tmp_path):
    with FileLock(tmp_path / "deep" / "nested" / ".lock"):
        assert (tmp_path / "deep" / "nested" / ".lock").exists()


def test_reentry_after_release_works(tmp_path):
    path = tmp_path / ".lock"
    with FileLock(path):
        pass
    with FileLock(path):
        pass


def test_a_second_process_is_blocked(tmp_path):
    """flock/msvcrt locks are per-process, so the real test needs a subprocess."""
    lock_path = tmp_path / ".lock"
    script = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {SRC!r})
        from codex_account_switcher.locking import FileLock
        from codex_account_switcher.exceptions import LockTimeout
        try:
            with FileLock({str(lock_path)!r}, timeout=0.4):
                print("ACQUIRED")
        except LockTimeout:
            print("BLOCKED")
        """
    )
    with FileLock(lock_path):
        result = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
        )
    assert result.stdout.strip() == "BLOCKED", result.stderr


def test_the_lock_is_released_for_the_next_process(tmp_path):
    lock_path = tmp_path / ".lock"
    script = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {SRC!r})
        from codex_account_switcher.locking import FileLock
        with FileLock({str(lock_path)!r}, timeout=2):
            print("ACQUIRED")
        """
    )
    with FileLock(lock_path):
        pass
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
    )
    assert result.stdout.strip() == "ACQUIRED", result.stderr


def test_timeout_raises_lock_timeout(tmp_path, monkeypatch):
    lock = FileLock(tmp_path / ".lock", timeout=0.2)
    monkeypatch.setattr("codex_account_switcher.locking._try_acquire", lambda fh: False)
    start = time.monotonic()
    with pytest.raises(LockTimeout, match="another xswap operation"):
        lock.acquire()
    assert time.monotonic() - start >= 0.2


def test_timeout_does_not_leak_a_file_handle(tmp_path, monkeypatch):
    """The handle opened for a failed acquire must be closed, not left dangling."""
    monkeypatch.setattr("codex_account_switcher.locking._try_acquire", lambda fh: False)
    lock = FileLock(tmp_path / ".lock", timeout=0.05)
    with pytest.raises(LockTimeout):
        lock.acquire()
    assert lock._fh is None


def test_exception_inside_the_block_still_releases(tmp_path):
    path = tmp_path / ".lock"
    with pytest.raises(ValueError), FileLock(path):
        raise ValueError("boom")
    # If the lock had leaked, this would time out.
    with FileLock(path, timeout=1):
        pass


def test_context_manager_does_not_swallow_exceptions(tmp_path):
    with pytest.raises(RuntimeError, match="propagated"), FileLock(tmp_path / ".lock"):
        raise RuntimeError("propagated")


def test_release_without_acquire_is_a_no_op(tmp_path):
    FileLock(tmp_path / ".lock").release()


def test_lock_file_is_not_truncated(tmp_path):
    """Opened 'a+', so a concurrent holder's bookkeeping is never clobbered."""
    path = tmp_path / ".lock"
    path.write_text("existing", encoding="utf-8")
    with FileLock(path):
        pass
    assert path.read_text(encoding="utf-8") == "existing"


def test_threads_in_one_process_do_not_deadlock_the_suite(tmp_path):
    """flock is per-file-descriptor, so two threads each opening their own
    handle genuinely contend. The point here is that contention resolves."""
    path = tmp_path / ".lock"
    order: list[str] = []

    def worker(name: str) -> None:
        try:
            with FileLock(path, timeout=5):
                order.append(name)
                time.sleep(0.05)
        except LockTimeout:  # pragma: no cover - would mean a real deadlock
            order.append(f"{name}-timeout")

    threads = [threading.Thread(target=worker, args=(f"t{i}",)) for i in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert len(order) == 3
    assert not any(name.endswith("timeout") for name in order)
