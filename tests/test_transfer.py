"""Export/import round trips between machines."""

from __future__ import annotations

import json

import pytest

from codex_account_switcher.exceptions import StoreError
from codex_account_switcher.store import AccountStore
from codex_account_switcher.switcher import BUNDLE_FORMAT, Switcher
from tests.conftest import make_auth


@pytest.fixture
def populated(switcher):
    switcher.add(make_auth("alice@example.com", account_id="acct-alice"), label="home")
    switcher.add(make_auth("bob@example.com", account_id="acct-bob", plan="team"))
    return switcher


def test_export_shape(populated):
    bundle = populated.export_bundle()
    assert bundle["format"] == BUNDLE_FORMAT
    assert bundle["version"] == 1
    assert [entry["slot"] for entry in bundle["accounts"]] == [1, 2]
    assert bundle["accounts"][0]["meta"]["email"] == "alice@example.com"


def test_export_is_json_serialisable(populated):
    json.dumps(populated.export_bundle())


def test_round_trip_into_a_fresh_store(populated, tmp_path):
    bundle = populated.export_bundle()
    bundle.pop("missing", None)

    fresh = Switcher(store=AccountStore(tmp_path / "other-machine"))
    result = fresh.import_bundle(bundle)

    assert len(result.added) == 2
    assert result.skipped == []
    emails = {a.email for a in fresh.store.load().accounts.values()}
    assert emails == {"alice@example.com", "bob@example.com"}


def test_round_trip_preserves_credentials_byte_for_byte(populated, tmp_path):
    """An imported account must work without a re-login."""
    original = {
        account.email: populated.store.read_blob(account)
        for account in populated.store.load().accounts.values()
    }
    fresh = Switcher(store=AccountStore(tmp_path / "other-machine"))
    fresh.import_bundle(populated.export_bundle())

    for account in fresh.store.load().accounts.values():
        restored = json.loads(fresh.store.read_blob(account))
        assert restored == json.loads(original[account.email])


def test_round_trip_preserves_labels(populated, tmp_path):
    fresh = Switcher(store=AccountStore(tmp_path / "other-machine"))
    fresh.import_bundle(populated.export_bundle())
    labels = {a.email: a.label for a in fresh.store.load().accounts.values()}
    assert labels["alice@example.com"] == "home"


def test_import_skips_accounts_already_present(populated):
    result = populated.import_bundle(populated.export_bundle())
    assert result.added == []
    assert len(result.skipped) == 2
    assert "already managed" in result.skipped[0]


def test_import_force_refreshes_existing_accounts(populated):
    result = populated.import_bundle(populated.export_bundle(), force=True)
    assert len(result.added) == 2
    assert len(populated.store.load().accounts) == 2


def test_export_reports_accounts_with_missing_credentials(populated):
    populated.store.delete_blob(populated.store.load().accounts[2])
    bundle = populated.export_bundle()
    assert len(bundle["accounts"]) == 1
    assert bundle["missing"] == ["slot 2 (bob@example.com)"]


def test_import_rejects_a_foreign_document(switcher):
    with pytest.raises(StoreError, match="not a codex-swap export bundle"):
        switcher.import_bundle({"format": "something-else", "accounts": []})


def test_import_rejects_a_non_mapping(switcher):
    with pytest.raises(StoreError, match="not a codex-swap export bundle"):
        switcher.import_bundle(["not", "a", "bundle"])


def test_import_rejects_a_bundle_without_accounts(switcher):
    with pytest.raises(StoreError, match="no accounts list"):
        switcher.import_bundle({"format": BUNDLE_FORMAT})


def test_import_skips_malformed_entries_and_keeps_going(switcher):
    good = json.loads(make_auth("good@example.com", account_id="acct-good"))
    bundle = {
        "format": BUNDLE_FORMAT,
        "version": 1,
        "accounts": [
            {"slot": 1, "auth": "not a dict"},
            {"slot": 2, "auth": {"garbage": True}},
            {"slot": 3, "auth": good},
        ],
    }
    result = switcher.import_bundle(bundle)
    assert [a.email for a in result.added] == ["good@example.com"]
    assert len(result.skipped) == 2


def test_import_assigns_fresh_slots_and_does_not_trust_the_bundle(switcher):
    """A bundle from another machine must not dictate local slot numbers."""
    switcher.add(make_auth("local@example.com", account_id="acct-local"))
    bundle = {
        "format": BUNDLE_FORMAT,
        "version": 1,
        "accounts": [
            {
                "slot": 1,
                "meta": {"email": "remote@example.com"},
                "auth": json.loads(make_auth("remote@example.com", account_id="acct-remote")),
            }
        ],
    }
    result = switcher.import_bundle(bundle)
    assert result.added[0].slot == 2
    assert switcher.store.load().accounts[1].email == "local@example.com"


def test_export_of_an_empty_store(switcher):
    bundle = switcher.export_bundle()
    assert bundle["accounts"] == []
