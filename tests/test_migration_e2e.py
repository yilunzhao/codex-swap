"""A store written by the pre-release prototype must keep working.

The prototype (a single-file script) shipped a different layout: credential
blobs under ``auth/`` instead of ``accounts/``, and ``activeAccountNumber``
instead of ``activeSlot`` in ``sequence.json``. Anyone who used it has real
logins in there, so the first run of the packaged tool has to pick them up
without a re-login.
"""

from __future__ import annotations

import json

import pytest

from codex_swap import paths
from codex_swap.cli import main
from codex_swap.store import AccountStore
from codex_swap.switcher import Switcher
from tests.conftest import make_auth


@pytest.fixture
def prototype_store(tmp_path, monkeypatch):
    """Build a store in the exact shape the prototype wrote."""
    monkeypatch.delenv("CODEX_SWAP_HOME", raising=False)
    legacy = tmp_path / "home" / paths.LEGACY_STORE_DIRNAME
    (legacy / "auth").mkdir(parents=True)

    (legacy / "sequence.json").write_text(
        json.dumps(
            {
                "activeAccountNumber": 2,
                "lastUpdated": "2026-08-12T14:38:00Z",
                "sequence": [1, 2],
                "accounts": {
                    "1": {
                        "email": "alice@example.com",
                        "accountId": "acct-alice",
                        "plan": "pro",
                        "authMode": "chatgpt",
                        "label": "personal",
                        "added": "2026-08-12T14:38:00Z",
                    },
                    "2": {
                        "email": "bob@example.com",
                        "accountId": "acct-bob",
                        "plan": "team",
                        "authMode": "chatgpt",
                        "label": "",
                        "added": "2026-08-12T14:39:00Z",
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    blobs = ((1, "alice@example.com", "acct-alice"), (2, "bob@example.com", "acct-bob"))
    for slot, email, account_id in blobs:
        (legacy / "auth" / f"auth-{slot}-{email}.json").write_text(
            make_auth(email, account_id=account_id), encoding="utf-8"
        )
    return legacy


def test_migration_preserves_accounts_and_credentials(prototype_store):
    assert paths.migrate_legacy_store() is True

    state = AccountStore().load()
    assert set(state.accounts) == {1, 2}
    assert state.accounts[1].email == "alice@example.com"
    assert state.accounts[1].label == "personal"
    assert state.active_slot == 2
    assert state.sequence == [1, 2]

    store = AccountStore()
    for account in state.accounts.values():
        blob = store.read_blob(account)
        assert blob is not None, f"slot {account.slot} lost its credentials"
        assert json.loads(blob)["tokens"]["refresh_token"] == f"rt.{account.email}"


def test_a_migrated_store_can_still_switch(prototype_store):
    paths.migrate_legacy_store()
    switcher = Switcher()
    result = switcher.switch_to("1")
    assert result.target.email == "alice@example.com"
    assert switcher.live_identity().email == "alice@example.com"


def test_the_cli_migrates_on_first_run(prototype_store, capsys):
    """The user should never have to run a migration command by hand."""
    assert main(["list"]) == 0
    out = capsys.readouterr().out
    assert "alice@example.com" in out
    assert "bob@example.com" in out
    assert "missing credentials" not in out


def test_migration_is_idempotent(prototype_store):
    assert paths.migrate_legacy_store() is True
    assert paths.migrate_legacy_store() is False
    assert len(AccountStore().load().accounts) == 2
