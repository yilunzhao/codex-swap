"""End-to-end CLI behaviour: exit codes, rendering, and the JSON contract."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from codex_swap.cli import build_parser, main
from codex_swap.paths import auth_path
from tests.conftest import make_auth


@pytest.fixture(autouse=True)
def no_running_codex(monkeypatch):
    """Keep CLI tests hermetic.

    The process scan shells out to `ps`, so without this the assertions would
    depend on whatever the developer (or the CI runner) happens to be running.
    Detection itself is covered in test_process_detection.py.
    """
    from codex_swap import process_detection

    monkeypatch.setattr(process_detection, "scan", lambda *a, **k: process_detection.ProcessScan())


@pytest.fixture
def store_dir(tmp_path):
    return str(tmp_path / "cli-store")


@pytest.fixture
def run(store_dir):
    """Invoke the CLI against an isolated store, returning the exit code."""

    def _run(*argv: str) -> int:
        return main(["--store", store_dir, *argv])

    return _run


@pytest.fixture
def run_json(run, capsys):
    """Invoke the CLI with --json and return the parsed document.

    Output from any set-up commands is discarded first, so the parse sees only
    this invocation's document.
    """

    def _run(*argv: str):
        capsys.readouterr()
        code = run("--json", *argv)
        out = capsys.readouterr().out
        return code, json.loads(out) if out.strip() else None

    return _run


@pytest.fixture
def logged_in():
    from codex_swap.fsutil import write_secret

    def _write(email="alice@example.com", **kwargs):
        text = make_auth(email, **kwargs)
        write_secret(auth_path(), text)
        return text

    return _write


class TestParser:
    def test_no_command_prints_help(self, capsys):
        assert main([]) == 0
        assert "usage:" in capsys.readouterr().out

    def test_version(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["--version"])
        assert exc.value.code == 0
        assert "codex-swap" in capsys.readouterr().out

    def test_unknown_command_exits_two(self):
        with pytest.raises(SystemExit) as exc:
            main(["frobnicate"])
        assert exc.value.code == 2

    @pytest.mark.parametrize(
        ("alias", "canonical"), [("ls", "list"), ("next", "switch"), ("rm", "remove")]
    )
    def test_aliases_are_wired(self, alias, canonical):
        parser = build_parser()
        args = parser.parse_args([alias] + (["1"] if alias == "rm" else []))
        assert args.func.__name__ == f"cmd_{canonical}"


class TestGlobalFlagPlacement:
    """Global flags must work on either side of the sub-command.

    `codex-swap status --json` is what people type (and what the README shows),
    but argparse only accepts flags declared on the top-level parser *before*
    the sub-command unless each sub-parser repeats them. Getting that wrong is
    silent: the command runs and prints a human table instead of JSON.
    """

    @pytest.mark.parametrize("command", ["status", "list", "ls", "doctor", "switch"])
    @pytest.mark.parametrize("flag", ["--json", "--no-color", "--yes"])
    def test_boolean_flags_work_in_both_positions(self, command, flag):
        parser = build_parser()
        dest = flag.lstrip("-").replace("-", "_")
        before = parser.parse_args([flag, command])
        after = parser.parse_args([command, flag])
        assert getattr(before, dest) is True, f"{flag} before {command}"
        assert getattr(after, dest) is True, f"{flag} after {command}"

    @pytest.mark.parametrize("command", ["status", "list", "doctor"])
    def test_store_works_in_both_positions(self, command, tmp_path):
        parser = build_parser()
        target = str(tmp_path / "store")
        assert parser.parse_args(["--store", target, command]).store == target
        assert parser.parse_args([command, "--store", target]).store == target

    def test_flags_default_to_off(self):
        args = build_parser().parse_args(["status"])
        assert args.json is False
        assert args.no_color is False
        assert args.yes is False
        assert args.store is None

    def test_a_flag_after_a_positional_argument_still_binds(self):
        args = build_parser().parse_args(["use", "2", "--json"])
        assert args.json is True
        assert args.account == "2"

    def test_json_after_the_subcommand_actually_emits_json(self, store_dir, logged_in, capsys):
        """The parser-level test above cannot catch a wiring mistake in main()."""
        logged_in()
        capsys.readouterr()
        assert main(["--store", store_dir, "status", "--json"]) == 0
        json.loads(capsys.readouterr().out)

    def test_every_subcommand_sets_a_handler(self):
        parser = build_parser()
        actions = [a for a in parser._actions if hasattr(a, "choices") and a.choices]
        for name, sub in actions[0].choices.items():
            assert sub.get_default("func") is not None, f"{name} has no handler"


class TestAddAndList:
    def test_add_captures_the_live_login(self, run, logged_in, capsys):
        logged_in()
        assert run("add") == 0
        assert "Added" in capsys.readouterr().out

    def test_add_without_a_login_fails_cleanly(self, run, capsys):
        assert run("add") == 1
        assert "codex login" in capsys.readouterr().err

    def test_add_from_a_file(self, run, tmp_path, capsys):
        blob = tmp_path / "saved.json"
        blob.write_text(make_auth("bob@example.com", account_id="acct-bob"), encoding="utf-8")
        assert run("add", "--from", str(blob)) == 0
        assert "bob@example.com" in capsys.readouterr().out

    def test_add_duplicate_exits_two(self, run, logged_in, capsys):
        logged_in()
        run("add")
        assert run("add") == 2
        assert "already managed" in capsys.readouterr().err

    def test_add_force_updates_in_place(self, run, logged_in, capsys):
        logged_in()
        run("add")
        assert run("add", "--force") == 0
        assert "Updated" in capsys.readouterr().out

    def test_empty_list(self, run, capsys):
        assert run("list") == 0
        assert "No managed accounts" in capsys.readouterr().out

    def test_list_marks_the_live_account(self, run, logged_in, tmp_path, capsys):
        logged_in()
        run("add")
        blob = tmp_path / "bob.json"
        blob.write_text(make_auth("bob@example.com", account_id="acct-bob"), encoding="utf-8")
        run("add", "--from", str(blob))

        capsys.readouterr()  # drop the set-up output
        run("list")
        out = capsys.readouterr().out
        alice_line = next(ln for ln in out.splitlines() if "alice@example.com" in ln)
        bob_line = next(ln for ln in out.splitlines() if "bob@example.com" in ln)
        assert alice_line.strip().startswith("*")
        assert not bob_line.strip().startswith("*")

    def test_list_warns_when_the_live_login_is_unmanaged(self, run, logged_in, tmp_path, capsys):
        blob = tmp_path / "bob.json"
        blob.write_text(make_auth("bob@example.com", account_id="acct-bob"), encoding="utf-8")
        run("add", "--from", str(blob))
        logged_in("stranger@example.com", account_id="acct-stranger")
        run("list")
        assert "not managed" in capsys.readouterr().out

    def test_list_flags_a_missing_credential_file(self, run, logged_in, store_dir, capsys):
        from pathlib import Path

        logged_in()
        run("add")
        for blob in (Path(store_dir) / "accounts").iterdir():
            blob.unlink()
        capsys.readouterr()
        run("list")
        assert "missing credentials" in capsys.readouterr().out


class TestSwitching:
    @pytest.fixture
    def two_accounts(self, run, logged_in, tmp_path):
        logged_in("alice@example.com", account_id="acct-alice")
        run("add")
        blob = tmp_path / "bob.json"
        blob.write_text(make_auth("bob@example.com", account_id="acct-bob"), encoding="utf-8")
        run("add", "--from", str(blob))
        return run

    def test_switch_rotates(self, two_accounts, capsys):
        assert two_accounts("switch") == 0
        out = capsys.readouterr().out
        assert "Switched to" in out and "bob@example.com" in out

    def test_switch_replaces_the_live_file(self, two_accounts):
        two_accounts("switch")
        from codex_swap.identity import parse_auth

        assert parse_auth(auth_path().read_text(encoding="utf-8")).email == "bob@example.com"

    def test_use_by_slot(self, two_accounts, capsys):
        assert two_accounts("use", "2") == 0
        assert "bob@example.com" in capsys.readouterr().out

    def test_use_by_email(self, two_accounts, capsys):
        assert two_accounts("use", "bob@example.com") == 0
        assert "bob@example.com" in capsys.readouterr().out

    def test_use_unknown_exits_two(self, two_accounts, capsys):
        assert two_accounts("use", "nobody@example.com") == 2
        assert "no managed account" in capsys.readouterr().err

    def test_switch_with_one_account_fails_cleanly(self, run, logged_in, capsys):
        logged_in()
        run("add")
        assert run("switch") == 1
        assert "only one account" in capsys.readouterr().err

    def test_switch_with_no_accounts_fails_cleanly(self, run, capsys):
        assert run("switch") == 1
        assert "no accounts are managed" in capsys.readouterr().err

    def test_restart_hint_is_shown(self, two_accounts, capsys):
        two_accounts("switch")
        assert "Restart Codex" in capsys.readouterr().out


class TestStatusAndDoctor:
    def test_status_without_a_login(self, run, capsys):
        assert run("status") == 0
        assert "No readable Codex login" in capsys.readouterr().out

    def test_status_shows_the_live_account(self, run, logged_in, capsys):
        logged_in()
        run("add")
        assert run("status") == 0
        out = capsys.readouterr().out
        assert "alice@example.com" in out
        assert "unmanaged" not in out

    def test_status_flags_an_unmanaged_login(self, run, logged_in, capsys):
        logged_in()
        assert run("status") == 0
        assert "unmanaged" in capsys.readouterr().out

    def test_doctor_is_clean_on_a_healthy_setup(self, run, logged_in, capsys, monkeypatch):
        monkeypatch.setattr(
            "codex_swap.process_detection.scan",
            lambda *a, **k: __import__(
                "codex_swap.process_detection", fromlist=["ProcessScan"]
            ).ProcessScan(),
        )
        logged_in()
        run("add")
        assert run("doctor") == 0
        assert "No problems found" in capsys.readouterr().out

    def test_doctor_reports_problems_and_exits_one(self, run, capsys):
        assert run("doctor") == 1
        assert "problem" in capsys.readouterr().out

    def test_doctor_reports_a_missing_credential_file(self, run, logged_in, capsys, store_dir):
        logged_in()
        run("add")
        from pathlib import Path

        for blob in (Path(store_dir) / "accounts").iterdir():
            blob.unlink()
        assert run("doctor") == 1
        assert "no stored credentials" in capsys.readouterr().out


class TestRemoveAndPurge:
    def test_remove_needs_confirmation(self, run, logged_in, capsys):
        logged_in()
        run("add")
        assert run("remove", "1") == 1
        assert "--yes" in capsys.readouterr().err

    def test_remove_with_yes(self, run, logged_in, capsys):
        logged_in()
        run("add")
        assert run("--yes", "remove", "1") == 0
        assert "Removed" in capsys.readouterr().out

    def test_remove_unknown_exits_two(self, run, capsys):
        assert run("--yes", "remove", "9") == 2

    def test_purge_on_an_empty_store(self, run, capsys):
        assert run("purge") == 0
        assert "Nothing to purge" in capsys.readouterr().out

    def test_purge_removes_the_store_but_not_the_login(self, run, logged_in, store_dir, capsys):
        from pathlib import Path

        logged_in()
        run("add")
        assert run("--yes", "purge") == 0
        assert not Path(store_dir).exists()
        assert auth_path().exists()


class TestTransfer:
    def test_export_import_round_trip(self, run, logged_in, tmp_path, capsys):
        logged_in()
        run("add")
        bundle = tmp_path / "bundle.json"
        assert run("export", str(bundle)) == 0
        assert bundle.exists()

        assert run("--yes", "purge") == 0
        capsys.readouterr()
        assert run("import", str(bundle)) == 0
        assert "Imported 1 account" in capsys.readouterr().out

    def test_export_to_stdout(self, run, logged_in, capsys):
        logged_in()
        run("add")
        capsys.readouterr()
        assert run("export", "-") == 0
        doc = json.loads(capsys.readouterr().out)
        assert doc["format"] == "codex-swap-export"

    def test_import_a_missing_file_exits_one(self, run, tmp_path, capsys):
        assert run("import", str(tmp_path / "nope.json")) == 1
        assert "no such file" in capsys.readouterr().err

    def test_import_garbage_exits_one(self, run, tmp_path, capsys):
        bad = tmp_path / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        assert run("import", str(bad)) == 1
        assert "not a readable export bundle" in capsys.readouterr().err

    def test_export_warns_about_plaintext_tokens(self, run, logged_in, tmp_path, capsys):
        logged_in()
        run("add")
        run("export", str(tmp_path / "bundle.json"))
        assert "keep it out of git" in capsys.readouterr().out


class TestJsonContract:
    def test_status_json(self, run_json, logged_in):
        logged_in()
        code, doc = run_json("status")
        assert code == 0
        assert doc["live"]["email"] == "alice@example.com"
        assert doc["live"]["plan"] == "pro"
        assert doc["managed"] == 0

    def test_list_json(self, run, run_json, logged_in):
        logged_in()
        run("add")
        code, doc = run_json("list")
        assert code == 0
        assert doc["accounts"][0]["email"] == "alice@example.com"
        assert doc["accounts"][0]["slot"] == 1
        assert doc["accounts"][0]["stored"] is True
        assert doc["liveSlot"] == 1

    def test_list_json_reports_a_missing_blob(self, run, run_json, logged_in, store_dir):
        from pathlib import Path

        logged_in()
        run("add")
        for blob in (Path(store_dir) / "accounts").iterdir():
            blob.unlink()
        _, doc = run_json("list")
        assert doc["accounts"][0]["stored"] is False

    def test_errors_are_json_too(self, run_json):
        code, doc = run_json("use", "nobody@example.com")
        assert code == 2
        assert doc["type"] == "AccountNotFoundError"
        assert "no managed account" in doc["error"]

    def test_json_output_is_never_colored(self, run, run_json, logged_in, monkeypatch):
        monkeypatch.setattr("sys.stdout.isatty", lambda: True)
        logged_in()
        run("add")
        _, doc = run_json("list")
        assert "\033[" not in json.dumps(doc)

    def test_json_refuses_to_prompt(self, run, run_json, logged_in):
        logged_in()
        run("add")
        code, doc = run_json("remove", "1")
        assert code == 1
        assert "--yes" in doc["error"]

    def test_doctor_json_lists_problems(self, run_json):
        code, doc = run_json("doctor")
        assert code == 1
        assert isinstance(doc["problems"], list) and doc["problems"]
        assert doc["version"]

    def test_add_json(self, run_json, logged_in):
        logged_in()
        code, doc = run_json("add")
        assert code == 0
        assert doc["added"]["email"] == "alice@example.com"
        assert doc["replaced"] is False

    def test_switch_json(self, run, run_json, logged_in, tmp_path):
        logged_in()
        run("add")
        blob = tmp_path / "bob.json"
        blob.write_text(make_auth("bob@example.com", account_id="acct-bob"), encoding="utf-8")
        run("add", "--from", str(blob))
        code, doc = run_json("switch")
        assert code == 0
        assert doc["switched"]["email"] == "bob@example.com"
        assert doc["previousSlot"] == 1
        assert doc["backedUp"] is True
        assert "processes" in doc


def test_no_color_flag_strips_escapes(run, logged_in, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    logged_in()
    run("--no-color", "add")
    assert "\033[" not in capsys.readouterr().out


def test_store_override_is_honoured(run, logged_in, store_dir):
    from pathlib import Path

    logged_in()
    run("add")
    assert (Path(store_dir) / "sequence.json").exists()


def test_credentials_never_reach_stdout(run, run_json, logged_in, capsys):
    """A token in terminal scrollback is a token in the shell history file."""
    logged_in()
    run("add")
    run("list")
    run("status")
    out = capsys.readouterr().out
    for needle in ("id_token", "access_token", "refresh_token", "rt.alice", "at.alice"):
        assert needle not in out


def test_module_entry_point_runs(tmp_path):
    """`python -m codex_swap` is a documented way in; it had no test."""
    import subprocess
    import sys

    env = {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).resolve().parent.parent / "src"),
        "CODEX_SWAP_HOME": str(tmp_path / "store"),
        "CODEX_HOME": str(tmp_path / "codex"),
    }
    result = subprocess.run(
        [sys.executable, "-m", "codex_swap", "--version"],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert result.returncode == 0
    assert "codex-swap" in result.stdout
