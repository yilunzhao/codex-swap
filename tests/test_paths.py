"""Path resolution, platform rules, and the legacy-store migration."""

from __future__ import annotations

from pathlib import Path

import pytest

from codex_swap import paths
from codex_swap.exceptions import StoreError
from codex_swap.models import Platform


def test_codex_home_follows_the_env_var(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "elsewhere"))
    assert paths.codex_home() == tmp_path / "elsewhere"
    assert paths.auth_path() == tmp_path / "elsewhere" / "auth.json"
    assert paths.config_path() == tmp_path / "elsewhere" / "config.toml"


def test_codex_home_defaults_under_home(monkeypatch, tmp_path):
    monkeypatch.delenv("CODEX_HOME", raising=False)
    assert paths.codex_home() == Path.home() / ".codex"


def test_codex_home_expands_a_tilde(monkeypatch):
    monkeypatch.setenv("CODEX_HOME", "~/custom-codex")
    assert paths.codex_home() == Path.home() / "custom-codex"


def test_store_root_prefers_an_absolute_override(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_SWAP_HOME", str(tmp_path / "store"))
    assert paths.store_root() == tmp_path / "store"


def test_store_root_ignores_a_relative_override(monkeypatch):
    """A relative value must not create a store in the working directory."""
    monkeypatch.setenv("CODEX_SWAP_HOME", "relative/path")
    monkeypatch.setattr(Platform, "detect", classmethod(lambda cls: Platform.MACOS))
    assert paths.store_root() == Path.home() / ".codex-swap"


@pytest.mark.parametrize("platform", [Platform.MACOS, Platform.WINDOWS, Platform.UNKNOWN])
def test_store_root_on_non_xdg_platforms(monkeypatch, platform):
    monkeypatch.delenv("CODEX_SWAP_HOME", raising=False)
    monkeypatch.setattr(Platform, "detect", classmethod(lambda cls: platform))
    assert paths.store_root() == Path.home() / ".codex-swap"


@pytest.mark.parametrize("platform", [Platform.LINUX, Platform.WSL])
def test_store_root_follows_xdg(monkeypatch, tmp_path, platform):
    monkeypatch.delenv("CODEX_SWAP_HOME", raising=False)
    monkeypatch.setattr(Platform, "detect", classmethod(lambda cls: platform))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert paths.store_root() == tmp_path / "xdg" / "codex-swap"


@pytest.mark.parametrize("value", ["", "not/absolute"])
def test_xdg_is_ignored_when_unset_or_relative(monkeypatch, value):
    """The XDG spec says to ignore the variable unless it is absolute."""
    monkeypatch.delenv("CODEX_SWAP_HOME", raising=False)
    monkeypatch.setattr(Platform, "detect", classmethod(lambda cls: Platform.LINUX))
    monkeypatch.setenv("XDG_DATA_HOME", value)
    assert paths.store_root() == Path.home() / ".local" / "share" / "codex-swap"


def test_xdg_expands_a_tilde(monkeypatch):
    """systemd units and Dockerfiles set the value without a shell to expand it."""
    monkeypatch.delenv("CODEX_SWAP_HOME", raising=False)
    monkeypatch.setattr(Platform, "detect", classmethod(lambda cls: Platform.LINUX))
    monkeypatch.setenv("XDG_DATA_HOME", "~/data")
    assert paths.store_root() == Path.home() / "data" / "codex-swap"


@pytest.mark.parametrize(
    ("email", "expected"),
    [
        ("alice@example.com", "alice@example.com"),
        ("a b/c", "a_b_c"),
        ("../../escape", ".._.._escape"),
        ("", "unknown"),
        ("naïve@example.com", "naïve@example.com"),
    ],
)
def test_slugify(email, expected):
    assert paths.slugify(email) == expected


def test_slugify_blocks_path_traversal():
    """A hostile email must not be able to write outside the accounts dir."""
    slug = paths.slugify("../../../etc/passwd")
    assert "/" not in slug and "\\" not in slug


class TestLegacyMigration:
    def _legacy(self, tmp_path, monkeypatch) -> Path:
        legacy = tmp_path / "home" / paths.LEGACY_STORE_DIRNAME
        legacy.mkdir(parents=True, exist_ok=True)
        (legacy / "sequence.json").write_text('{"accounts": {}}', encoding="utf-8")
        return legacy

    def test_moves_the_legacy_directory(self, tmp_path, monkeypatch):
        legacy = self._legacy(tmp_path, monkeypatch)
        target = tmp_path / "new-store"
        assert paths.migrate_legacy_store(target) is True
        assert not legacy.exists()
        assert (target / "sequence.json").exists()

    def test_is_a_no_op_without_a_legacy_directory(self, tmp_path):
        assert paths.migrate_legacy_store(tmp_path / "new-store") is False

    def test_is_a_no_op_when_source_and_target_match(self, tmp_path, monkeypatch):
        legacy = self._legacy(tmp_path, monkeypatch)
        assert paths.migrate_legacy_store(legacy) is False
        assert legacy.exists()

    def test_refuses_a_genuine_collision(self, tmp_path, monkeypatch):
        self._legacy(tmp_path, monkeypatch)
        target = tmp_path / "new-store"
        target.mkdir()
        (target / "sequence.json").write_text("{}", encoding="utf-8")
        with pytest.raises(StoreError, match="Refusing to merge"):
            paths.migrate_legacy_store(target)

    def test_overwrites_a_target_holding_only_throwaway_artifacts(self, tmp_path, monkeypatch):
        """A log file from a prior run must not block the migration."""
        self._legacy(tmp_path, monkeypatch)
        target = tmp_path / "new-store"
        (target / "cache").mkdir(parents=True)
        (target / "codex-swap.log").write_text("noise", encoding="utf-8")
        assert paths.migrate_legacy_store(target) is True
        assert (target / "sequence.json").exists()

    def test_resumes_when_the_target_holds_nothing_of_value(self, tmp_path, monkeypatch):
        """Flag present + a target of throwaway artifacts means retry."""
        self._legacy(tmp_path, monkeypatch)
        target = tmp_path / "new-store"
        (target / "cache").mkdir(parents=True)
        (target / "codex-swap.log").write_text("noise", encoding="utf-8")
        flag = target.parent / f".{target.name}.migrating"
        flag.touch()

        assert paths.migrate_legacy_store(target) is True
        assert (target / "sequence.json").exists()
        assert not flag.exists()

    def test_refuses_to_discard_a_target_holding_data_even_with_the_flag(
        self, tmp_path, monkeypatch
    ):
        """The flag must not be a licence to delete credentials.

        An interrupted *cross-filesystem* move leaves the legacy copy partial and
        the target complete — the exact inverse of what the flag was taken to
        mean. Since the two cases are indistinguishable from disk, the only safe
        reading is to refuse and let the user look.
        """
        self._legacy(tmp_path, monkeypatch)
        target = tmp_path / "new-store"
        (target / "accounts").mkdir(parents=True)
        (target / "sequence.json").write_text('{"accounts": {}}', encoding="utf-8")
        (target / "accounts" / "auth-1-a@example.com.json").write_text("precious", encoding="utf-8")
        (target.parent / f".{target.name}.migrating").touch()

        with pytest.raises(StoreError, match="Refusing to merge"):
            paths.migrate_legacy_store(target)
        assert (target / "accounts" / "auth-1-a@example.com.json").exists()

    def test_renames_the_prototype_accounts_directory(self, tmp_path, monkeypatch):
        """The prototype used `auth/`; without the rename every account would
        migrate as "missing credentials"."""
        legacy = self._legacy(tmp_path, monkeypatch)
        (legacy / "auth").mkdir()
        (legacy / "auth" / "auth-1-a@example.com.json").write_text("{}", encoding="utf-8")

        target = tmp_path / "new-store"
        assert paths.migrate_legacy_store(target) is True
        assert (target / "accounts" / "auth-1-a@example.com.json").exists()
        assert not (target / "auth").exists()

    def test_rename_does_not_clobber_an_existing_accounts_dir(self, tmp_path, monkeypatch):
        legacy = self._legacy(tmp_path, monkeypatch)
        (legacy / "auth").mkdir()
        (legacy / "auth" / "old.json").write_text("{}", encoding="utf-8")
        (legacy / "accounts").mkdir()
        (legacy / "accounts" / "new.json").write_text("{}", encoding="utf-8")

        target = tmp_path / "new-store"
        paths.migrate_legacy_store(target)
        assert (target / "accounts" / "new.json").exists()
        assert (target / "auth" / "old.json").exists()

    def test_clears_a_stale_flag_when_legacy_is_gone(self, tmp_path):
        target = tmp_path / "new-store"
        target.mkdir()
        flag = target.parent / f".{target.name}.migrating"
        flag.touch()
        assert paths.migrate_legacy_store(target) is False
        assert not flag.exists()


def test_blob_names_are_readable_and_include_the_slot(tmp_path):
    """The store is meant to be inspectable with `ls` when something is wrong."""
    from codex_swap.store import AccountStore

    blob = AccountStore(tmp_path / "store").blob_path(3, "carol@example.com")
    assert blob.name == "auth-3-carol@example.com.json"
