"""Atomic, owner-only writes for credential material."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from codex_swap.fsutil import ensure_private_dir, is_private, read_text, write_secret

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")


def test_write_then_read_round_trip(tmp_path):
    target = tmp_path / "auth.json"
    write_secret(target, '{"hello": "world"}')
    assert read_text(target) == '{"hello": "world"}'


def test_write_creates_missing_parents(tmp_path):
    target = tmp_path / "deep" / "nested" / "auth.json"
    write_secret(target, "{}")
    assert target.exists()


def test_overwrite_replaces_content(tmp_path):
    target = tmp_path / "auth.json"
    write_secret(target, "first")
    write_secret(target, "second")
    assert read_text(target) == "second"


@posix_only
def test_written_file_is_owner_only(tmp_path):
    target = tmp_path / "auth.json"
    write_secret(target, "secret")
    assert target.stat().st_mode & 0o777 == 0o600
    assert is_private(target) is True


@posix_only
def test_private_dir_is_owner_only(tmp_path):
    directory = ensure_private_dir(tmp_path / "accounts")
    assert directory.stat().st_mode & 0o777 == 0o700


@posix_only
def test_is_private_detects_a_loose_file(tmp_path):
    target = tmp_path / "loose.json"
    target.write_text("{}", encoding="utf-8")
    os.chmod(target, 0o644)
    assert is_private(target) is False


@pytest.fixture
def workdir(tmp_path):
    """An empty directory. `tmp_path` itself holds the isolation fixture's dirs."""
    path = tmp_path / "workdir"
    path.mkdir()
    return path


def test_no_temp_files_are_left_behind(workdir):
    target = workdir / "auth.json"
    write_secret(target, "{}")
    assert [p.name for p in workdir.iterdir()] == ["auth.json"]


def test_a_failed_write_leaves_the_original_intact(workdir, monkeypatch):
    """os.replace failing must not destroy the file being replaced."""
    target = workdir / "auth.json"
    write_secret(target, "original")

    def boom(src, dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="simulated"):
        write_secret(target, "replacement")

    assert read_text(target) == "original"
    leftovers = [p.name for p in workdir.iterdir() if p.name != "auth.json"]
    assert leftovers == [], f"temp file left behind: {leftovers}"


def test_read_text_returns_none_for_a_missing_file(tmp_path):
    assert read_text(tmp_path / "nope.json") is None


def test_read_text_returns_none_for_a_directory(tmp_path):
    assert read_text(tmp_path) is None


def test_is_private_returns_none_for_a_missing_file(tmp_path):
    if sys.platform == "win32":
        pytest.skip("POSIX modes only")
    assert is_private(tmp_path / "nope.json") is None


def test_unicode_survives_the_round_trip(tmp_path):
    target = tmp_path / "auth.json"
    payload = '{"email": "naïve@example.com", "note": "日本語"}'
    write_secret(target, payload)
    assert read_text(target) == payload


def test_newlines_are_not_translated(tmp_path):
    """Codex parses the file byte-wise; CRLF injection on Windows would corrupt it."""
    target = tmp_path / "auth.json"
    write_secret(target, "line1\nline2\n")
    assert target.read_bytes() == b"line1\nline2\n"


def test_ensure_private_dir_is_idempotent(tmp_path):
    """Called on every write, so a second call must not fail or reset the mode."""
    path = tmp_path / "accounts"
    ensure_private_dir(path)
    (path / "existing.json").write_text("keep me", encoding="utf-8")
    ensure_private_dir(path)
    assert path.is_dir()
    assert (path / "existing.json").read_text(encoding="utf-8") == "keep me"
    if sys.platform != "win32":
        assert path.stat().st_mode & 0o777 == 0o700


def test_write_secret_accepts_a_path_like(tmp_path):
    write_secret(Path(str(tmp_path / "auth.json")), "{}")
    assert (tmp_path / "auth.json").exists()
