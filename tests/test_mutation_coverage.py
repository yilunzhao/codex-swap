"""Tests that exist because a mutation survived.

Every test in this file was written after deleting or inverting a specific piece
of production code and finding that the suite still passed. The comment on each
one names the mutation it now catches, so that a future reader can tell what the
test is load-bearing for rather than guessing from its name.

The pattern behind most of them: the *logic* was well covered in isolation while
its *use* was not. `locking.py` had thorough tests and nothing asserted that any
caller took the lock; process detection was exhaustively tested and the warning
it exists to print was rendered in no test at all.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

from codex_account_switcher import identity as identity_mod
from codex_account_switcher import process_detection
from codex_account_switcher.cli import main
from codex_account_switcher.fsutil import write_secret
from codex_account_switcher.identity import identity_from_dict
from codex_account_switcher.models import Platform
from codex_account_switcher.process_detection import CodexProcess, ProcessScan
from codex_account_switcher.store import AccountStore, StoreState
from tests.conftest import make_auth, make_id_token

# ---------------------------------------------------------------------------
# Locking: `with self.lock():` -> `if True:` used to pass the whole suite
# ---------------------------------------------------------------------------


class TestMutatingOperationsTakeTheLock:
    """MUTATION: replacing `with self.lock():` with `if True:` in `switch_to`
    and `add` passed 338/338.

    `locking.py` was tested exhaustively on its own, but nothing asserted that a
    caller ever used it, leaving the interleaving the module docstring warns
    about (back up the live blob / overwrite the live blob) entirely unguarded.
    """

    @pytest.fixture
    def recording_lock(self, monkeypatch):
        from codex_account_switcher import switcher as switcher_mod

        taken: list[str] = []
        real = switcher_mod.FileLock

        class Recording(real):
            def acquire(self):
                taken.append(str(self.path))
                return super().acquire()

        monkeypatch.setattr(switcher_mod, "FileLock", Recording)
        return taken

    def test_add_locks(self, switcher, recording_lock):
        switcher.add(make_auth("a@e.com", account_id="acct-a"))
        assert recording_lock, "add() did not take the store lock"

    def test_switch_locks(self, switcher, recording_lock):
        switcher.add(make_auth("a@e.com", account_id="acct-a"))
        recording_lock.clear()
        switcher.switch_to("1")
        assert recording_lock, "switch_to() did not take the store lock"

    def test_remove_locks(self, switcher, recording_lock):
        switcher.add(make_auth("a@e.com", account_id="acct-a"))
        recording_lock.clear()
        switcher.remove("1")
        assert recording_lock, "remove() did not take the store lock"

    def test_export_locks(self, switcher, recording_lock):
        switcher.add(make_auth("a@e.com", account_id="acct-a"))
        recording_lock.clear()
        switcher.export_bundle()
        assert recording_lock, "export_bundle() did not take the store lock"

    def test_purge_locks(self, switcher, recording_lock):
        switcher.add(make_auth("a@e.com", account_id="acct-a"))
        recording_lock.clear()
        switcher.purge()
        assert recording_lock, "purge() did not take the store lock"

    def test_the_lock_is_the_stores_own(self, switcher, recording_lock):
        switcher.add(make_auth("a@e.com", account_id="acct-a"))
        assert recording_lock[0] == str(switcher.store.lock_path)

    def test_the_lock_actually_blocks(self, switcher):
        """The recording fixture proves the call happens; this proves the call
        means something. (Cross-process blocking itself is covered in
        test_locking.py; this is the store-lock path specifically.)"""
        from codex_account_switcher.exceptions import LockTimeout
        from codex_account_switcher.locking import FileLock

        switcher.add(make_auth("a@e.com", account_id="acct-a"))
        with FileLock(switcher.store.lock_path), pytest.raises(LockTimeout):
            FileLock(switcher.store.lock_path, timeout=0.2).acquire()


# ---------------------------------------------------------------------------
# The running-Codex warning: rendered in no test at all
# ---------------------------------------------------------------------------


class TestRunningCodexWarningIsRendered:
    """MUTATION: `if scan.holders:` -> `if False:` in `_report_processes`
    survived, because every CLI test stubs the scan to an empty result.

    Detection was well covered; the warning it exists to produce was not.
    """

    @pytest.fixture
    def two_accounts(self, switcher, live_auth):
        live_auth(make_auth("a@e.com", account_id="acct-a"))
        switcher.add_live()
        switcher.add(make_auth("b@e.com", account_id="acct-b"))
        return switcher

    @pytest.fixture
    def busy(self, monkeypatch):
        scan = ProcessScan(
            holders=[
                CodexProcess(pid=1, name="codex", role="app-server"),
                CodexProcess(pid=2, name="codex", role="app-server"),
                CodexProcess(pid=3, name="codex", role="tui"),
            ],
            helpers=[CodexProcess(pid=4, name="codex-code-mode-host")],
        )
        monkeypatch.setattr(process_detection, "scan", lambda *a, **k: scan)
        return scan

    def test_switch_warns_with_a_count(self, two_accounts, busy, capsys):
        capsys.readouterr()
        main(["--store", str(two_accounts.store.root), "switch"])
        out = capsys.readouterr().out
        assert "3 Codex process(es) still running" in out

    def test_switch_names_the_kinds(self, two_accounts, busy, capsys):
        capsys.readouterr()
        main(["--store", str(two_accounts.store.root), "switch"])
        assert "2 x codex app-server" in capsys.readouterr().out

    def test_switch_explains_the_consequence(self, two_accounts, busy, capsys):
        capsys.readouterr()
        main(["--store", str(two_accounts.store.root), "switch"])
        assert "silently undo this swap" in capsys.readouterr().out

    def test_json_carries_the_holders(self, two_accounts, busy, capsys):
        capsys.readouterr()
        main(["--store", str(two_accounts.store.root), "--json", "switch"])
        doc = json.loads(capsys.readouterr().out)
        assert len(doc["processes"]["holders"]) == 3
        assert doc["processes"]["helpers"] == 1

    def test_no_warning_when_nothing_is_running(self, two_accounts, capsys, monkeypatch):
        monkeypatch.setattr(process_detection, "scan", lambda *a, **k: ProcessScan())
        capsys.readouterr()
        main(["--store", str(two_accounts.store.root), "switch"])
        assert "still running" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# doctor: four of seven diagnostics could be deleted silently
# ---------------------------------------------------------------------------


class TestDoctorDiagnosticsIndividually:
    """MUTATION: `if …:` -> `if False:` on three separate `doctor` checks all
    survived, because the tests asserted only that *some* problem was reported.
    One unrelated finding satisfied them forever.
    """

    @pytest.fixture
    def quiet(self, monkeypatch):
        monkeypatch.setattr(process_detection, "scan", lambda *a, **k: ProcessScan())

    def _problems(self, store_root, capsys) -> list[str]:
        capsys.readouterr()
        main(["--store", str(store_root), "--json", "doctor"])
        return json.loads(capsys.readouterr().out)["problems"]

    def test_reports_a_missing_login(self, switcher, quiet, capsys):
        problems = self._problems(switcher.store.root, capsys)
        assert any("no live Codex login" in p for p in problems)

    def test_reports_an_unmanaged_live_login(self, switcher, live_auth, quiet, capsys):
        switcher.add(make_auth("b@e.com", account_id="acct-b"))
        live_auth(make_auth("stranger@e.com", account_id="acct-s"))
        problems = self._problems(switcher.store.root, capsys)
        assert any("not managed" in p for p in problems)

    def test_reports_a_missing_credential_blob(self, switcher, live_auth, quiet, capsys):
        live_auth(make_auth("a@e.com", account_id="acct-a"))
        switcher.add_live()
        switcher.store.delete_blob(switcher.store.load().accounts[1])
        problems = self._problems(switcher.store.root, capsys)
        assert any("no stored credentials" in p for p in problems)

    def test_reports_running_processes(self, switcher, live_auth, monkeypatch, capsys):
        live_auth(make_auth("a@e.com", account_id="acct-a"))
        switcher.add_live()
        monkeypatch.setattr(
            process_detection,
            "scan",
            lambda *a, **k: ProcessScan(holders=[CodexProcess(pid=1, name="codex")]),
        )
        problems = self._problems(switcher.store.root, capsys)
        assert any("process(es) running" in p for p in problems)

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
    def test_reports_a_world_readable_auth_file(self, switcher, live_auth, quiet, capsys):
        live_auth(make_auth("a@e.com", account_id="acct-a"))
        switcher.add_live()
        os.chmod(switcher.auth_file, 0o644)
        problems = self._problems(switcher.store.root, capsys)
        assert any("readable by other users" in p for p in problems)

    def test_a_healthy_setup_reports_nothing(self, switcher, live_auth, quiet, capsys):
        live_auth(make_auth("a@e.com", account_id="acct-a"))
        switcher.add_live()
        assert self._problems(switcher.store.root, capsys) == []


# ---------------------------------------------------------------------------
# The interactive confirmation prompt
# ---------------------------------------------------------------------------


class TestConfirmationPrompt:
    """MUTATION: inverting `… in ("y", "yes")` survived, so typing `n` at
    "Delete the store?" would have purged it. Only the `--yes` and
    non-interactive branches were covered.
    """

    @pytest.fixture
    def interactive(self, monkeypatch):
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    def _answer(self, monkeypatch, reply: str) -> None:
        monkeypatch.setattr("builtins.input", lambda prompt="": reply)

    @pytest.mark.parametrize("reply", ["n", "no", "", "  ", "maybe"])
    def test_declining_keeps_the_account(self, switcher, interactive, monkeypatch, reply):
        switcher.add(make_auth("a@e.com", account_id="acct-a"))
        self._answer(monkeypatch, reply)
        assert main(["--store", str(switcher.store.root), "remove", "1"]) == 0
        assert 1 in switcher.store.load().accounts

    @pytest.mark.parametrize("reply", ["y", "Y", "yes", " YES "])
    def test_accepting_removes_the_account(self, switcher, interactive, monkeypatch, reply):
        switcher.add(make_auth("a@e.com", account_id="acct-a"))
        self._answer(monkeypatch, reply)
        assert main(["--store", str(switcher.store.root), "remove", "1"]) == 0
        assert switcher.store.load().accounts == {}

    def test_declining_keeps_the_store(self, switcher, interactive, monkeypatch):
        switcher.add(make_auth("a@e.com", account_id="acct-a"))
        self._answer(monkeypatch, "n")
        assert main(["--store", str(switcher.store.root), "purge"]) == 0
        assert switcher.store.root.exists()

    def test_end_of_input_declines(self, switcher, interactive, monkeypatch):
        """A closed stdin must not read as consent."""
        switcher.add(make_auth("a@e.com", account_id="acct-a"))

        def eof(prompt=""):
            raise EOFError

        monkeypatch.setattr("builtins.input", eof)
        assert main(["--store", str(switcher.store.root), "purge"]) == 0
        assert switcher.store.root.exists()


# ---------------------------------------------------------------------------
# `codex login` at the CLI level: previously 0% covered
# ---------------------------------------------------------------------------


class TestLoginCommand:
    """GAP: `cmd_login` and `_run_codex_login` had no test at any level: the
    one command that spawns a subprocess and rewrites live credentials.

    The runner is injected here; the audit hook in conftest refuses to spawn the
    real `codex` binary, so a test that forgot to inject one would fail loudly
    rather than overwrite the developer's login.
    """

    @pytest.fixture
    def fake_codex(self, monkeypatch, switcher):
        calls: list[list[str]] = []

        def runner(command):
            calls.append(list(command))
            write_secret(switcher.auth_file, make_auth("new@e.com", account_id="acct-new"))
            return 0

        monkeypatch.setattr("codex_account_switcher.switcher._run_codex_login", runner)
        monkeypatch.setattr("shutil.which", lambda name: f"/usr/local/bin/{name}")
        return calls

    def test_stores_the_new_account(self, switcher, fake_codex, capsys):
        capsys.readouterr()
        assert main(["--store", str(switcher.store.root), "login"]) == 0
        assert "new@e.com" in capsys.readouterr().out
        assert switcher.store.load().accounts[1].email == "new@e.com"

    def test_invokes_codex_login(self, switcher, fake_codex):
        main(["--store", str(switcher.store.root), "login"])
        assert fake_codex == [["codex", "login"]]

    def test_json_output(self, switcher, fake_codex, capsys):
        capsys.readouterr()
        assert main(["--store", str(switcher.store.root), "--json", "login"]) == 0
        doc = json.loads(capsys.readouterr().out)
        assert doc["added"]["email"] == "new@e.com"

    def test_a_missing_codex_binary_is_reported(self, switcher, monkeypatch, capsys):
        monkeypatch.setattr("shutil.which", lambda name: None)
        capsys.readouterr()
        assert main(["--store", str(switcher.store.root), "login"]) == 4
        assert "not found on PATH" in capsys.readouterr().err

    def test_a_failed_login_is_reported(self, switcher, monkeypatch, capsys):
        monkeypatch.setattr("shutil.which", lambda name: "/usr/local/bin/codex")
        monkeypatch.setattr("codex_account_switcher.switcher._run_codex_login", lambda command: 1)
        capsys.readouterr()
        assert main(["--store", str(switcher.store.root), "login"]) == 4
        assert "exited with status 1" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Smaller survivors
# ---------------------------------------------------------------------------


class TestAtomicWriteStaging:
    def test_the_temp_file_is_staged_in_the_destination_directory(self, tmp_path, monkeypatch):
        """MUTATION: dropping `dir=` from `mkstemp` survived.

        Staging in the system temp dir makes `os.replace` cross-filesystem (an
        unconditional OSError whenever $CODEX_HOME is on another mount), and
        leaves the credential in a shared directory if the process dies.
        """
        import tempfile as tempfile_mod

        workdir = tmp_path / "dest"
        workdir.mkdir()
        seen: list[str | None] = []
        real = tempfile_mod.mkstemp

        def spy(*args, **kwargs):
            seen.append(kwargs.get("dir"))
            return real(*args, **kwargs)

        monkeypatch.setattr(tempfile_mod, "mkstemp", spy)
        write_secret(workdir / "auth.json", "{}")
        assert seen == [str(workdir)]


class TestIdentityCoercions:
    """MUTATION: deleting the `isinstance` guards for `email`, `plan` and
    `last_refresh` all survived. The test that claimed to cover this fed
    `tokens: "not a dict"`, so no claim was ever read.
    """

    def _blob(self, claims: dict) -> dict:
        import base64

        header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
        body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
        return {"auth_mode": "chatgpt", "tokens": {"id_token": f"{header}.{body}.sig"}}

    def test_a_non_string_email_is_not_accepted_as_an_identity(self):
        from codex_account_switcher.exceptions import AuthParseError

        blob = self._blob({"email": {"nested": "object"}})
        with pytest.raises(AuthParseError, match="no account identity"):
            identity_from_dict(blob)

    def test_a_non_string_plan_becomes_empty(self):
        claims = {
            "email": "a@e.com",
            "https://api.openai.com/auth": {"chatgpt_plan_type": ["pro"]},
        }
        assert identity_from_dict(self._blob(claims)).plan == ""

    def test_a_non_string_account_id_becomes_empty(self):
        claims = {
            "email": "a@e.com",
            "https://api.openai.com/auth": {"chatgpt_account_id": 12345},
        }
        assert identity_from_dict(self._blob(claims)).account_id == ""

    def test_a_non_string_last_refresh_becomes_empty(self):
        blob = self._blob({"email": "a@e.com"})
        blob["last_refresh"] = 1234567890
        assert identity_from_dict(blob).last_refresh == ""

    def test_the_account_id_falls_back_to_the_tokens_field(self):
        """MUTATION: dropping `or tokens.get("account_id")` survived, because the
        old test asserted `account_id in ("", "fallback-acct")`, accepting both
        the right answer and the broken one.

        Real impact: a pre-JWT-claim blob keys as ("bob@…", ""), so the same
        account is added twice and backup-matching on switch stops working.
        """
        token = make_id_token("bob@e.com", account_id="")
        blob = {
            "auth_mode": "chatgpt",
            "tokens": {"id_token": token, "account_id": "fallback-acct"},
        }
        # Strip the namespaced claim entirely so only the fallback can supply it.
        assert identity_mod.decode_jwt_payload(token)["https://api.openai.com/auth"] == {
            "chatgpt_plan_type": "pro",
            "chatgpt_account_id": "",
        }
        assert identity_from_dict(blob).account_id == "fallback-acct"


class TestStoreRepairAndValidation:
    def test_a_non_integer_active_slot_does_not_crash(self):
        """MUTATION: removing the try/except around `int(raw_active)` survived."""
        state = StoreState.from_dict(
            {"activeSlot": "not-a-number", "accounts": {"1": {"email": "a@e.com"}}}
        )
        assert state.active_slot is None

    def test_the_rotation_order_is_sorted(self, switcher):
        """MUTATION: dropping `state.sequence.sort()` survived, leaving rotation
        to follow insertion order instead of slot order."""
        switcher.add(make_auth("c@e.com", account_id="acct-c"), slot=3)
        switcher.add(make_auth("a@e.com", account_id="acct-a"), slot=1)
        switcher.add(make_auth("b@e.com", account_id="acct-b"), slot=2)
        assert switcher.store.load().sequence == [1, 2, 3]

    def test_rotation_visits_slots_in_order(self, switcher, live_auth):
        live_auth(make_auth("c@e.com", account_id="acct-c"))
        switcher.add_live(slot=3)
        switcher.add(make_auth("a@e.com", account_id="acct-a"), slot=1)
        switcher.add(make_auth("b@e.com", account_id="acct-b"), slot=2)
        assert switcher.rotate().target.slot == 1
        assert switcher.rotate().target.slot == 2

    def test_export_survives_a_corrupt_stored_blob(self, switcher):
        """MUTATION: removing the `except ValueError` around `json.loads`
        survived; the existing test only deleted a blob, never corrupted one."""
        switcher.add(make_auth("a@e.com", account_id="acct-a"))
        switcher.add(make_auth("b@e.com", account_id="acct-b"))
        switcher.store.write_blob(switcher.store.load().accounts[2], "{ not json")

        bundle = switcher.export_bundle()
        assert [entry["slot"] for entry in bundle["accounts"]] == [1]
        assert bundle["missing"] == ["slot 2 (b@e.com)"]


class TestJsonFlagIsHonoured:
    def test_emit_is_silent_without_json(self, switcher, capsys):
        """MUTATION: `_emit` ignoring `args.json` survived, so a JSON document
        leaking into human output would have gone unnoticed."""
        switcher.add(make_auth("a@e.com", account_id="acct-a"))
        capsys.readouterr()
        main(["--store", str(switcher.store.root), "status"])
        out = capsys.readouterr().out
        assert not out.lstrip().startswith("{")

    def test_token_state_column_reflects_expiry(self, switcher, capsys):
        """MUTATION: `_token_state` always returning "unreadable" survived: the
        freshness column in `list` was asserted nowhere."""
        switcher.add(make_auth("fresh@e.com", account_id="acct-f", expires_in=3600))
        switcher.add(make_auth("stale@e.com", account_id="acct-s", expires_in=-3600))
        capsys.readouterr()
        main(["--store", str(switcher.store.root), "list"])
        out = capsys.readouterr().out
        fresh_line = next(ln for ln in out.splitlines() if "fresh@e.com" in ln)
        stale_line = next(ln for ln in out.splitlines() if "stale@e.com" in ln)
        assert "valid" in fresh_line
        assert "expired" in stale_line


class TestPlatformDetection:
    """MUTATION: breaking WSL detection survived, because every store-root test
    monkeypatches `Platform.detect` and nothing exercises the real one."""

    def test_wsl_is_detected_from_the_distro_variable(self, monkeypatch):
        monkeypatch.setattr("platform.system", lambda: "Linux")
        monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
        assert Platform.detect() is Platform.WSL

    def test_wsl_is_detected_from_the_interop_variable(self, monkeypatch):
        monkeypatch.setattr("platform.system", lambda: "Linux")
        monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
        monkeypatch.setenv("WSL_INTEROP", "/run/WSL/1_interop")
        assert Platform.detect() is Platform.WSL

    def test_plain_linux_is_not_wsl(self, monkeypatch):
        monkeypatch.setattr("platform.system", lambda: "Linux")
        monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
        monkeypatch.delenv("WSL_INTEROP", raising=False)
        assert Platform.detect() is Platform.LINUX

    @pytest.mark.parametrize(
        ("system", "expected"),
        [("Darwin", Platform.MACOS), ("Windows", Platform.WINDOWS), ("Haiku", Platform.UNKNOWN)],
    )
    def test_other_platforms(self, monkeypatch, system, expected):
        monkeypatch.setattr("platform.system", lambda: system)
        assert Platform.detect() is expected

    def test_wsl_stores_under_xdg_not_the_home_dotfile(self, monkeypatch, tmp_path):
        """The consequence of getting WSL wrong: the store lands elsewhere."""
        from codex_account_switcher import paths

        monkeypatch.setattr("platform.system", lambda: "Linux")
        monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
        monkeypatch.delenv("XSWAP_HOME", raising=False)
        monkeypatch.delenv("CODEX_SWAP_HOME", raising=False)
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
        assert paths.store_root() == tmp_path / "xdg" / "xswap"


class TestDiscardedWarningIsRendered:
    def test_the_human_warning_appears(self, switcher, live_auth, capsys, monkeypatch):
        """GAP: the switcher-level `discarded` flag was tested; the warning the
        user actually sees was only ever asserted in JSON."""
        monkeypatch.setattr(process_detection, "scan", lambda *a, **k: ProcessScan())
        switcher.add(make_auth("b@e.com", account_id="acct-b"))
        live_auth(make_auth("stranger@e.com", account_id="acct-s"))
        capsys.readouterr()
        main(["--store", str(switcher.store.root), "use", "1"])
        out = capsys.readouterr().out
        assert "stranger@e.com" in out
        assert "was not managed" in out


def test_store_save_rejects_unserialisable_state(store, monkeypatch):
    """MUTATION: dropping the pre-write `json.loads` validation survived.

    The point of validating before `write_secret` is that a store which will not
    parse fails on the *next* command, far from the cause.
    """
    state = StoreState()
    monkeypatch.setattr(
        "codex_account_switcher.store.json.dumps", lambda *a, **k: "{definitely not json"
    )
    with pytest.raises(ValueError):
        store.save(state)
    assert not store.sequence_path.exists()


def test_written_credentials_are_flushed_to_disk(tmp_path, monkeypatch):
    """MUTATION: dropping `os.fsync` survived. A crash between write and flush
    loses the only copy of a refresh token."""
    synced: list[int] = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (synced.append(fd), real(fd))[1])
    write_secret(tmp_path / "auth.json", "{}")
    assert synced, "write_secret did not fsync before renaming into place"


def test_blob_paths_are_confined_to_the_accounts_directory(tmp_path):
    """A hostile email must not be able to name a file outside the store.

    `AccountStore.blob_path` is now the single implementation of this rule; a
    duplicate in `paths` was removed because two copies can drift apart.
    """
    store = AccountStore(tmp_path / "store")
    for email in ("a b@example.com", "../../../etc/passwd", "x/y@e.com", "", "..", "a\\b"):
        blob = store.blob_path(2, email)
        assert blob.parent == store.accounts_dir
        # The name may *contain* dots; what matters is that it stays one path
        # component, so it can never climb out of the accounts directory.
        assert "/" not in blob.name and "\\" not in blob.name
        assert store.accounts_dir.resolve() in blob.resolve().parents
