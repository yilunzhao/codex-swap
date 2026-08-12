"""The test process must be structurally unable to write real Codex data.

These are the control tests for the audit hook installed in ``conftest``. They
matter because the fixture-based isolation around them unwinds at teardown: a
thread that outlives its own test sees the real ``$HOME`` again, and a guard
that also unwinds would already be gone by then.

Every probe here is aimed at :data:`conftest.SENTINEL_ROOT`, a path that is a
member of ``PROTECTED_ROOTS`` but names nothing on disk. Aiming them at the
developer's real ``~/.codex`` — as the first version of this file did — gives a
test that passes only while the guard works, and deletes real credentials the
day it stops. A separate test asserts the real roots are covered, without
attacking them.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from tests import conftest

SENTINEL = conftest.SENTINEL_ROOT


def _blocked():
    return pytest.raises(conftest.RealStoreWriteBlocked)


class TestCoverage:
    def test_the_real_roots_are_protected(self):
        """The guard is meaningless if it protects nothing real."""
        assert conftest.REAL_PROTECTED_ROOTS, "no real roots were frozen at import"
        assert any("codex" in root.name for root in conftest.REAL_PROTECTED_ROOTS)

    def test_the_sentinel_is_protected_but_absent(self):
        assert SENTINEL in conftest.PROTECTED_ROOTS
        assert not SENTINEL.exists(), "the sentinel must never be created"

    def test_roots_are_frozen_even_with_no_codex_installed(self, monkeypatch, tmp_path):
        """A fresh machine (or a CI runner) has no ~/.codex; the roots are pure
        path arithmetic, so they must still be non-empty."""
        monkeypatch.setenv("HOME", str(tmp_path / "empty"))
        assert len(conftest._freeze_protected_roots()) >= 2

    def test_the_refusal_is_not_an_oserror(self):
        """`mkdir(exist_ok=True)` swallows OSError, so it must not be one."""
        assert not issubclass(conftest.RealStoreWriteBlocked, OSError)


class TestControls:
    def test_a_tmp_write_is_allowed(self, tmp_path):
        """CONTROL: the guard must not block everything."""
        target = tmp_path / "allowed.json"
        target.write_text("{}", encoding="utf-8")
        assert target.read_text(encoding="utf-8") == "{}"

    def test_a_write_under_a_protected_root_is_refused(self):
        with _blocked(), open(SENTINEL / "probe.json", "w", encoding="utf-8") as fh:
            fh.write("should never land")

    def test_a_thread_outliving_its_isolation_is_refused(self):
        """The case that actually matters.

        ``Path.home`` and ``os.environ`` are process-global, so a thread running
        after its test's monkeypatch unwound sees the real home. The audit hook
        is global too, which is why it still catches this.
        """
        outcome: list[BaseException | None] = []

        def writer() -> None:
            try:
                with open(SENTINEL / "thread-probe.json", "w", encoding="utf-8") as fh:
                    fh.write("should never land")
                outcome.append(None)
            except BaseException as exc:
                outcome.append(exc)

        thread = threading.Thread(target=writer)
        thread.start()
        thread.join(timeout=5)

        assert outcome and isinstance(outcome[0], conftest.RealStoreWriteBlocked)

    def test_isolated_env_points_away_from_the_real_paths(self):
        """The first isolation layer is doing its job too."""
        from codex_swap import paths

        for resolved in (paths.codex_home(), paths.store_root()):
            assert not any(
                resolved == root or root in resolved.parents for root in conftest.PROTECTED_ROOTS
            ), f"{resolved} resolves inside a protected root"


class TestWriteShapes:
    """One case per way a write can reach the filesystem."""

    def test_pathlib_write_text(self):
        with _blocked():
            (SENTINEL / "p.json").write_text("x", encoding="utf-8")

    def test_mkdir(self):
        with _blocked():
            (SENTINEL / "sub").mkdir(parents=True)

    def test_os_replace_destination(self, tmp_path):
        source = tmp_path / "staged.json"
        source.write_text("{}", encoding="utf-8")
        with _blocked():
            os.replace(source, SENTINEL / "renamed.json")

    def test_os_remove(self):
        with _blocked():
            os.remove(SENTINEL / "gone.json")

    def test_shutil_rmtree(self):
        with _blocked():
            shutil.rmtree(SENTINEL)

    def test_shutil_copy_destination(self, tmp_path):
        source = tmp_path / "src.json"
        source.write_text("{}", encoding="utf-8")
        with _blocked():
            shutil.copy(source, SENTINEL / "copied.json")

    def test_os_open_with_write_flags(self):
        with _blocked():
            os.open(str(SENTINEL / "raw.json"), os.O_WRONLY | os.O_CREAT)

    def test_a_read_is_allowed(self, tmp_path):
        """Reads must stay unimpeded, or the guard breaks every fixture."""
        target = tmp_path / "readable.json"
        target.write_text("{}", encoding="utf-8")
        assert target.read_text(encoding="utf-8") == "{}"


class TestBypassAttempts:
    """Each of these landed against the first version of the guard."""

    def test_a_bytes_path_cannot_slip_through(self):
        """`os.fspath` raises TypeError on bytes, which was swallowed as safe."""
        with _blocked(), open(os.fsencode(str(SENTINEL / "bytes.json")), "wb") as fh:
            fh.write(b"x")

    def test_a_bytes_path_to_os_remove_cannot_slip_through(self):
        with _blocked():
            os.remove(os.fsencode(str(SENTINEL / "bytes.json")))

    @pytest.mark.skipif(sys.platform == "win32", reason="dir_fd is POSIX-only")
    def test_a_relative_name_under_a_protected_dir_fd_is_refused(self, tmp_path):
        """How `shutil.rmtree` deletes children: a bare name plus a dir_fd.

        Resolving that against the process CWD — which is what `abspath` does —
        made every per-file deletion inside an rmtree invisible, so the
        front-door check on `shutil.rmtree` was all that stood in its way.
        """
        stand_in = tmp_path / "stand-in-root"
        stand_in.mkdir()
        (stand_in / "auth.json").write_text("{}", encoding="utf-8")
        fd = os.open(stand_in, os.O_RDONLY)
        original = conftest.PROTECTED_ROOTS
        conftest.PROTECTED_ROOTS = (*original, Path(os.path.abspath(stand_in)))
        try:
            with _blocked():
                os.remove("auth.json", dir_fd=fd)
            assert (stand_in / "auth.json").exists()
        finally:
            conftest.PROTECTED_ROOTS = original
            os.close(fd)

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
    def test_writing_through_a_symlink_into_a_protected_root_is_refused(self, tmp_path):
        """`abspath` does not follow links, so `tmp/link -> ~/.codex` resolved to
        a tmp path and passed. Real setups hit this: a `~/.codex` synced through
        Dropbox, or a `$HOME` with a symlinked component."""
        stand_in = tmp_path / "stand-in-root"
        stand_in.mkdir()
        link = tmp_path / "link"
        link.symlink_to(stand_in)

        original = conftest.PROTECTED_ROOTS
        conftest.PROTECTED_ROOTS = (*original, Path(os.path.abspath(stand_in)))
        try:
            with _blocked():
                (link / "auth.json").write_text("landed", encoding="utf-8")
            assert not (stand_in / "auth.json").exists()
        finally:
            conftest.PROTECTED_ROOTS = original

    def test_spawning_the_real_codex_binary_is_refused(self):
        """Audit hooks are per-process; a child inherits none of this.

        `codex login` rewrites the developer's real auth.json, and `cmd_login`
        shells out to it — so a CLI test written without an injected runner
        would overwrite live credentials with the guard none the wiser.
        """
        with _blocked():
            subprocess.Popen(["codex", "login"])

    def test_handing_a_protected_path_to_a_subprocess_is_refused(self):
        with _blocked():
            subprocess.Popen(["/bin/echo", str(SENTINEL / "auth.json")])

    def test_an_unrelated_subprocess_is_still_allowed(self):
        """`ps` is how process detection works; blocking it would break the tool."""
        result = subprocess.run(
            [sys.executable, "-c", "print('ok')"], capture_output=True, text=True
        )
        assert result.stdout.strip() == "ok"
