"""Regressions for defects found in review of the first release.

Every test here corresponds to a bug that the original 338-test suite passed
straight over. They are kept together, rather than scattered into the topical
files, because what they have in common is the reason they were missed: each one
lives at a seam between two pieces that were individually well covered — a value
read before a write that later invalidates it, a flag whose two meanings differ
only in intent, an argv the process lister cannot unambiguously split.
"""

from __future__ import annotations

import contextlib
import json

import pytest

from codex_swap import paths, printer
from codex_swap.cli import main
from codex_swap.exceptions import (
    AccountExistsError,
    StoreError,
    UnreadableAuthError,
    ValidationError,
)
from codex_swap.fsutil import read_text, write_secret
from codex_swap.paths import auth_path
from codex_swap.process_detection import ProcessScan, _classify, _scan_posix
from codex_swap.store import AccountStore
from codex_swap.switcher import Switcher
from tests.conftest import make_auth


class TestSelfSwitchDoesNotSwapBlobs:
    """`use` on the account you are already on must not undo a token refresh.

    The stored blob was read at the top of `switch_to`, *before* the back-up step
    rewrote that same file. When the target and the previous account are the same
    slot, writing that stale value back to the live file pushed the pre-refresh
    token over a credential Codex had since renewed — losing precisely what the
    back-up had just preserved, while printing "Switched to".
    """

    @pytest.fixture
    def refreshed(self, switcher, live_auth):
        live_auth(make_auth("alice@example.com", account_id="acct-a", access_token="v1"))
        switcher.add_live()
        # Codex refreshes the token in place, as it does on any long session.
        switcher.write_live(
            make_auth("alice@example.com", account_id="acct-a", access_token="v2-FRESH")
        )
        return switcher

    def test_live_file_keeps_the_fresh_token(self, refreshed):
        refreshed.switch_to("1")
        assert "v2-FRESH" in refreshed.live_auth()

    def test_stored_copy_is_refreshed_from_live(self, refreshed):
        refreshed.switch_to("1")
        account = refreshed.store.load().accounts[1]
        assert "v2-FRESH" in refreshed.store.read_blob(account)

    def test_result_reports_it_as_a_no_op(self, refreshed):
        result = refreshed.switch_to("1")
        assert result.already_active is True
        assert result.backed_up is True

    def test_repeating_it_does_not_oscillate(self, refreshed):
        for _ in range(4):
            refreshed.switch_to("1")
        assert "v2-FRESH" in refreshed.live_auth()

    def test_cli_says_already_on_rather_than_switched(self, refreshed, capsys, tmp_path):
        capsys.readouterr()
        assert main(["--store", str(refreshed.store.root), "use", "1"]) == 0
        out = capsys.readouterr().out
        assert "Already on" in out and "Switched to" not in out


class TestLoginSlotPermission:
    """`login --slot N` used to pass force=True unconditionally.

    Re-storing your own account and destroying somebody else's are different
    permissions; they were the same boolean.
    """

    @pytest.fixture
    def occupied(self, switcher):
        switcher.add(make_auth("bob@example.com", account_id="acct-b"), slot=2)
        return switcher

    def _login_as(self, switcher, email, account_id):
        def runner(command):
            switcher.write_live(make_auth(email, account_id=account_id))
            return 0

        return runner

    def test_refuses_to_replace_a_different_account(self, occupied):
        with pytest.raises(AccountExistsError, match=r"slot 2 is taken by bob@example\.com"):
            occupied.login(slot=2, runner=self._login_as(occupied, "dave@e.com", "acct-d"))
        assert occupied.store.load().accounts[2].email == "bob@example.com"

    def test_the_occupants_credentials_survive(self, occupied):
        blob_before = occupied.store.read_blob(occupied.store.load().accounts[2])
        with contextlib.suppress(AccountExistsError):
            occupied.login(slot=2, runner=self._login_as(occupied, "dave@e.com", "acct-d"))
        assert occupied.store.read_blob(occupied.store.load().accounts[2]) == blob_before

    def test_force_still_replaces(self, occupied):
        result = occupied.login(
            slot=2, force=True, runner=self._login_as(occupied, "dave@e.com", "acct-d")
        )
        assert result.account.email == "dave@e.com"

    def test_relogin_of_the_same_account_needs_no_force(self, switcher, live_auth):
        live_auth(make_auth("a@e.com", account_id="acct-a", access_token="first"))
        switcher.add_live()

        def runner(command):
            switcher.write_live(make_auth("a@e.com", account_id="acct-a", access_token="second"))
            return 0

        result = switcher.login(runner=runner)
        assert result.account.slot == 1
        assert len(switcher.store.load().accounts) == 1
        assert "second" in switcher.store.read_blob(result.account)


class TestAddSlotIntegrity:
    def test_an_explicit_slot_cannot_duplicate_a_managed_identity(self, switcher):
        """Two slots holding one account make it unresolvable by email and
        rotate through the same login twice."""
        switcher.add(make_auth("bob@e.com", account_id="acct-b"))
        with pytest.raises(AccountExistsError, match="already managed as slot 1"):
            switcher.add(make_auth("bob@e.com", account_id="acct-b"), slot=5)
        assert len(switcher.store.load().accounts) == 1

    def test_force_moves_the_account_rather_than_copying_it(self, switcher):
        switcher.add(make_auth("bob@e.com", account_id="acct-b"))
        result = switcher.add(make_auth("bob@e.com", account_id="acct-b"), slot=5, force=True)
        state = switcher.store.load()
        assert result.account.slot == 5
        assert set(state.accounts) == {5}
        assert AccountStore.resolve(state, "bob@e.com").slot == 5

    @pytest.mark.parametrize("slot", [0, -1, -99])
    def test_a_non_positive_slot_is_rejected(self, switcher, slot):
        """It could be stored, but `matches_identifier` uses str.isdigit, so it
        could never be selected again."""
        with pytest.raises(ValidationError, match="1 or greater"):
            switcher.add(make_auth("z@e.com", account_id="acct-z"), slot=slot)

    def test_a_failed_save_does_not_orphan_the_previous_credential(self, switcher, monkeypatch):
        """The outgoing blob used to be deleted before the new state committed."""
        switcher.add(make_auth("bob@e.com", account_id="acct-b"), slot=1)
        before = switcher.store.read_blob(switcher.store.load().accounts[1])

        def full_disk(state):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(switcher.store, "save", full_disk)
        with pytest.raises(OSError):
            switcher.add(make_auth("dave@e.com", account_id="acct-d"), slot=1, force=True)

        state = switcher.store.load()
        assert state.accounts[1].email == "bob@e.com"
        assert switcher.store.read_blob(state.accounts[1]) == before


class TestExportSnapshotsLive:
    def test_bundle_carries_the_refreshed_token(self, switcher, live_auth):
        """Every other write path snapshots live first; export did not, so the
        bundle shipped a pre-refresh credential for the active account."""
        live_auth(make_auth("alice@e.com", account_id="acct-a", access_token="v1"))
        switcher.add_live()
        switcher.write_live(make_auth("alice@e.com", account_id="acct-a", access_token="v2-FRESH"))

        bundle = switcher.export_bundle()
        exported = json.dumps(bundle["accounts"][0]["auth"])
        assert "v2-FRESH" in exported


class TestUnreadableLiveFile:
    def test_replacing_an_unparseable_login_is_reported(self, switcher):
        """An unknown token shape is likelier than junk, so destroying it
        silently is the wrong default."""
        switcher.add(make_auth("bob@e.com", account_id="acct-b"))
        switcher.write_live('{"auth_mode": "chatgpt", "tokens": {"refresh_token": "REAL"}}')
        result = switcher.switch_to("1")
        assert result.discarded_unreadable is True

    def test_non_utf8_auth_is_an_error_not_absence(self, tmp_path):
        """Reporting it as "no login" would send the user to `codex login` and
        quietly discard a file that may still hold a usable refresh token."""
        bad = tmp_path / "auth.json"
        bad.write_bytes(b"\xff\xfe\x00\x01{}")
        with pytest.raises(UnreadableAuthError, match="not valid UTF-8"):
            read_text(bad)

    def test_doctor_survives_an_unreadable_auth_file(self, tmp_path, capsys):
        write_secret(auth_path(), "{}")
        auth_path().write_bytes(b"\xff\xfe\x00\x01")
        capsys.readouterr()
        code = main(["--store", str(tmp_path / "s"), "doctor", "--json"])
        doc = json.loads(capsys.readouterr().out)
        assert code == 1
        assert any("UTF-8" in problem for problem in doc["problems"])


class TestPurgeScope:
    def test_refuses_a_directory_that_is_not_a_store(self, switcher, tmp_path):
        """`--store` is a plain path; one unset variable plus `--yes` was an
        unbounded recursive delete."""
        victim = tmp_path / "not-a-store"
        (victim / "important").mkdir(parents=True)
        (victim / "important" / "thesis.tex").write_text("my thesis", encoding="utf-8")

        with pytest.raises(StoreError, match="does not look like a codex-swap store"):
            Switcher(store=AccountStore(victim)).purge()
        assert (victim / "important" / "thesis.tex").exists()

    def test_still_purges_a_real_store(self, switcher):
        switcher.add(make_auth("a@e.com", account_id="acct-a"))
        switcher.purge()
        assert not switcher.store.root.exists()

    def test_purges_a_store_whose_sequence_file_was_lost(self, switcher):
        switcher.add(make_auth("a@e.com", account_id="acct-a"))
        switcher.store.sequence_path.unlink()
        switcher.purge()
        assert not switcher.store.root.exists()


class TestEmptyStoreOption:
    def test_an_empty_store_path_is_an_error(self, capsys):
        """It used to fall through to the real default store, so
        `codex-swap --store "$UNSET" purge --yes` deleted the user's accounts."""
        capsys.readouterr()
        assert main(["--store", "", "status"]) == 2
        assert "empty path" in capsys.readouterr().err

    def test_a_whitespace_store_path_is_an_error(self, capsys):
        assert main(["--store", "   ", "status"]) == 2


class TestProcessListingWithSpaces:
    """A path containing a space is the dangerous direction: the user is told
    nothing is running while a live Codex is about to overwrite the swap."""

    SPACEY = "/Users/me/Library/Application Support/Code/User/globalStorage/oai/codex"

    def test_core_binary_at_a_path_with_spaces(self):
        probe = ProcessScan()
        _classify(1, f"{self.SPACEY} app-server".split(), probe)
        assert probe.holders and probe.holders[0].role == "app-server"

    def test_node_shim_pointing_at_a_path_with_spaces(self):
        probe = ProcessScan()
        _classify(1, f"node {self.SPACEY} app-server".split(), probe)
        assert probe.holders and probe.holders[0].role == "app-server"

    def test_helper_at_a_path_with_spaces(self):
        probe = ProcessScan()
        spacey_helper = "/Users/me/Library/Application Support/x/codex-linux-sandbox"
        _classify(1, spacey_helper.split(), probe)
        assert probe.helpers and not probe.holders

    def test_a_codex_path_passed_as_an_argument_is_not_a_process(self):
        """The rejoin must not turn `vim /some/dir/codex` into the binary."""
        probe = ProcessScan()
        _classify(1, ["vim", "/some/dir/codex"], probe)
        assert not probe.holders and not probe.helpers

    def test_scan_finds_it_end_to_end(self, monkeypatch):
        import subprocess

        listing = f"  742     1 {self.SPACEY} app-server\n"
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=listing, stderr=""),
        )
        assert {p.pid for p in _scan_posix(set()).holders} == {742}


class TestUnavailableProcessListing:
    def test_human_output_says_the_check_did_not_run(self, switcher, live_auth, capsys, tmp_path):
        """Silence reads as "nothing is running" — the one conclusion this check
        must never imply without having run."""
        from codex_swap import process_detection

        live_auth(make_auth("a@e.com", account_id="acct-a"))
        switcher.add_live()
        switcher.add(make_auth("b@e.com", account_id="acct-b"))

        unavailable = ProcessScan(unavailable=True)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(process_detection, "scan", lambda *a, **k: unavailable)
            capsys.readouterr()
            main(["--store", str(switcher.store.root), "switch"])
        out = capsys.readouterr().out
        assert "could not read the process list" in out

    def test_doctor_reports_it_as_a_problem(self, capsys, tmp_path):
        from codex_swap import process_detection

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(process_detection, "scan", lambda *a, **k: ProcessScan(unavailable=True))
            capsys.readouterr()
            code = main(["--store", str(tmp_path / "s"), "doctor", "--json"])
        doc = json.loads(capsys.readouterr().out)
        assert code == 1
        assert doc["processes"]["unavailable"] is True
        assert any("process list" in problem for problem in doc["problems"])


class TestOutputContracts:
    def test_export_to_stdout_warns_about_dropped_accounts(self, switcher, capsys):
        """A silently short bundle looks like a complete one."""
        switcher.add(make_auth("a@e.com", account_id="acct-a"))
        switcher.add(make_auth("b@e.com", account_id="acct-b"))
        switcher.store.delete_blob(switcher.store.load().accounts[2])

        capsys.readouterr()
        main(["--store", str(switcher.store.root), "export", "-"])
        captured = capsys.readouterr()
        # The warning goes to stderr so it cannot corrupt a piped bundle.
        assert "b@e.com" in captured.err
        assert json.loads(captured.out)["accounts"]

    def test_json_export_to_stdout_keeps_the_envelope(self, switcher, capsys):
        switcher.add(make_auth("a@e.com", account_id="acct-a"))
        capsys.readouterr()
        main(["--store", str(switcher.store.root), "--json", "export", "-"])
        doc = json.loads(capsys.readouterr().out)
        assert doc["exported"] == 1
        assert doc["path"] == "-"
        assert doc["bundle"]["format"] == "codex-swap-export"

    def test_list_json_distinguishes_absent_from_unmanaged(self, switcher, live_auth, capsys):
        switcher.add(make_auth("b@e.com", account_id="acct-b"))

        capsys.readouterr()
        main(["--store", str(switcher.store.root), "--json", "list"])
        absent = json.loads(capsys.readouterr().out)
        assert absent["liveEmail"] is None and absent["liveManaged"] is False

        live_auth(make_auth("stranger@e.com", account_id="acct-s"))
        capsys.readouterr()
        main(["--store", str(switcher.store.root), "--json", "list"])
        unmanaged = json.loads(capsys.readouterr().out)
        assert unmanaged["liveEmail"] == "stranger@e.com"
        assert unmanaged["liveManaged"] is False


class TestAbbreviateSeparator:
    @pytest.mark.parametrize(
        ("path", "home", "expected"),
        [
            ("/home/bobby/store", "/home/bob", "/home/bobby/store"),
            ("/home/bob/x", "/home/bob", "~/x"),
            ("/home/bob", "/home/bob", "~"),
            ("/etc/codex", "/", "~/etc/codex"),
        ],
    )
    def test_prefix_must_end_at_a_separator(self, path, home, expected):
        """A raw string prefix rendered /home/bobby/store as ~by/store."""
        import os

        if os.sep != "/":  # pragma: no cover - POSIX-shaped fixtures
            pytest.skip("POSIX separators")
        assert printer.abbreviate(path, home=home) == expected


class TestFilesystemErrorsAreClean:
    def test_an_os_error_does_not_traceback(self, switcher, monkeypatch, capsys):
        """exceptions.py promises nothing escapes as a bare Exception."""
        switcher.add(make_auth("a@e.com", account_id="acct-a"))

        def full_disk(*args, **kwargs):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr("codex_swap.cli.AccountStore.load", full_disk)
        capsys.readouterr()
        code = main(["--store", str(switcher.store.root), "list"])
        assert code == 1
        assert "filesystem error" in capsys.readouterr().err


def test_migration_still_renames_the_prototype_accounts_dir(tmp_path, monkeypatch):
    """Guard against the lock refactor having skipped the rename."""
    legacy = tmp_path / "home" / paths.LEGACY_STORE_DIRNAME
    (legacy / "auth").mkdir(parents=True)
    (legacy / "sequence.json").write_text('{"accounts": {}}', encoding="utf-8")
    (legacy / "auth" / "auth-1-a@example.com.json").write_text("{}", encoding="utf-8")

    target = tmp_path / "store"
    assert paths.migrate_legacy_store(target) is True
    assert (target / "accounts" / "auth-1-a@example.com.json").exists()


def test_purge_refuses_before_warning_or_prompting(tmp_path, capsys):
    """Being told "this deletes every stored account" and only then that it was
    not a store reads as though something was already destroyed."""
    victim = tmp_path / "not-a-store"
    (victim / "keep").mkdir(parents=True)
    capsys.readouterr()

    assert main(["--store", str(victim), "purge", "--yes"]) == 1
    captured = capsys.readouterr()
    assert "does not look like a codex-swap store" in captured.err
    assert "deletes every stored account" not in captured.out
    assert (victim / "keep").exists()


class TestHostileAuthShapes:
    """Both of these escaped `parse_auth`'s contract of raising only
    `AuthParseError`, so they reached the CLI as a traceback or as a non-string
    in the JSON output."""

    def test_deeply_nested_json_is_a_parse_error(self):
        from codex_swap.exceptions import AuthParseError
        from codex_swap.identity import parse_auth

        with pytest.raises(AuthParseError, match="nesting is too deep"):
            parse_auth("[" * 200_000 + "]" * 200_000)

    def test_a_deeply_nested_id_token_payload_degrades(self):
        import base64

        from codex_swap.identity import decode_jwt_payload

        payload = base64.urlsafe_b64encode(b"[" * 200_000).decode().rstrip("=")
        assert decode_jwt_payload(f"a.{payload}.c") == {}

    def test_a_non_string_auth_mode_is_not_carried_into_the_store(self):
        from codex_swap.identity import identity_from_dict

        identity = identity_from_dict({"auth_mode": {"x": 1}, "OPENAI_API_KEY": "sk-abcdefgh"})
        assert isinstance(identity.auth_mode, str)
        assert identity.auth_mode == "apikey"

    def test_the_cli_reports_a_hostile_auth_file_cleanly(self, tmp_path, capsys):
        from codex_swap.fsutil import write_secret
        from codex_swap.paths import auth_path

        write_secret(auth_path(), "[" * 200_000 + "]" * 200_000)
        capsys.readouterr()
        assert main(["--store", str(tmp_path / "s"), "status"]) == 0
        assert "No readable Codex login" in capsys.readouterr().out


def test_the_migration_failure_path_reports_cleanly(tmp_path, monkeypatch):
    """GAP: `raise StoreError("Migration … failed")` was never taken, so nothing
    verified that a half-moved store surfaces as a message rather than a crash."""
    import shutil

    legacy = tmp_path / "home" / paths.LEGACY_STORE_DIRNAME
    legacy.mkdir(parents=True)
    (legacy / "sequence.json").write_text('{"accounts": {}}', encoding="utf-8")

    def failing_move(src, dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(shutil, "move", failing_move)
    with pytest.raises(StoreError, match=r"Migration .* failed"):
        paths.migrate_legacy_store(tmp_path / "store")
