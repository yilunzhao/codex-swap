"""Store state: serialisation, lookups, and rotation order."""

from __future__ import annotations

import json

import pytest

from codex_account_switcher.exceptions import AccountNotFoundError, StoreError
from codex_account_switcher.models import Account
from codex_account_switcher.store import AccountStore, StoreState


def _account(slot: int, email: str, account_id: str = "") -> Account:
    return Account(slot=slot, email=email, account_id=account_id or f"acct-{slot}")


def _state(*accounts: Account) -> StoreState:
    return StoreState(
        active_slot=accounts[0].slot if accounts else None,
        sequence=[a.slot for a in accounts],
        accounts={a.slot: a for a in accounts},
    )


class TestSerialisation:
    def test_round_trip(self, store):
        state = _state(_account(1, "a@example.com"), _account(2, "b@example.com"))
        store.save(state)
        loaded = store.load()
        assert loaded.active_slot == 1
        assert loaded.sequence == [1, 2]
        assert loaded.accounts[2].email == "b@example.com"
        assert loaded.last_updated

    def test_missing_file_yields_an_empty_state(self, store):
        state = store.load()
        assert state.accounts == {}
        assert state.active_slot is None
        assert store.exists() is False

    def test_corrupt_json_is_reported_clearly(self, store):
        store.sequence_path.parent.mkdir(parents=True, exist_ok=True)
        store.sequence_path.write_text("{not json", encoding="utf-8")
        with pytest.raises(StoreError, match="not valid JSON"):
            store.load()

    def test_non_object_json_is_rejected(self, store):
        store.sequence_path.parent.mkdir(parents=True, exist_ok=True)
        store.sequence_path.write_text("[1, 2, 3]", encoding="utf-8")
        with pytest.raises(StoreError, match="JSON object"):
            store.load()

    def test_saved_file_is_valid_json(self, store):
        store.save(_state(_account(1, "a@example.com")))
        json.loads(store.sequence_path.read_text(encoding="utf-8"))

    def test_legacy_active_account_number_key_is_honoured(self, store):
        """The prototype wrote `activeAccountNumber`; imported stores must work."""
        store.sequence_path.parent.mkdir(parents=True, exist_ok=True)
        store.sequence_path.write_text(
            json.dumps(
                {
                    "activeAccountNumber": 2,
                    "sequence": [1, 2],
                    "accounts": {
                        "1": {"email": "a@example.com"},
                        "2": {"email": "b@example.com"},
                    },
                }
            ),
            encoding="utf-8",
        )
        assert store.load().active_slot == 2


class TestStateRepair:
    def test_active_slot_pointing_at_a_missing_account_is_dropped(self):
        state = StoreState.from_dict(
            {"activeSlot": 9, "sequence": [1], "accounts": {"1": {"email": "a@example.com"}}}
        )
        assert state.active_slot is None

    def test_accounts_missing_from_sequence_are_appended(self):
        """An account absent from `sequence` must stay reachable by rotation."""
        state = StoreState.from_dict(
            {
                "sequence": [2],
                "accounts": {"1": {"email": "a@e.com"}, "2": {"email": "b@e.com"}},
            }
        )
        assert set(state.sequence) == {1, 2}

    def test_sequence_entries_without_an_account_are_dropped(self):
        state = StoreState.from_dict({"sequence": [1, 7], "accounts": {"1": {"email": "a@e.com"}}})
        assert state.sequence == [1]

    def test_duplicate_sequence_entries_are_collapsed(self):
        state = StoreState.from_dict(
            {"sequence": [1, 1, 1], "accounts": {"1": {"email": "a@e.com"}}}
        )
        assert state.sequence == [1]

    @pytest.mark.parametrize("junk", ["abc", None, 1.5, [], {}])
    def test_non_integer_slot_keys_are_ignored(self, junk):
        state = StoreState.from_dict(
            {"accounts": {"1": {"email": "a@e.com"}, str(junk): {"email": "b@e.com"}}}
        )
        assert 1 in state.accounts

    @pytest.mark.parametrize(
        "payload", [{"accounts": "not a dict"}, {"sequence": "abc"}, {}, {"accounts": None}]
    )
    def test_garbage_shapes_degrade_to_an_empty_store(self, payload):
        """Not merely "does not crash": a malformed file must read as no
        accounts, never as a partially-populated store that a swap would act on."""
        state = StoreState.from_dict(payload)
        assert state.accounts == {}
        assert state.sequence == []
        assert state.active_slot is None


class TestLookups:
    def test_next_free_slot_fills_gaps(self):
        state = _state(_account(1, "a@e.com"), _account(3, "c@e.com"))
        assert AccountStore.next_free_slot(state) == 2

    def test_next_free_slot_on_an_empty_store(self):
        assert AccountStore.next_free_slot(StoreState()) == 1

    def test_find_by_key_matches_email_and_account_id(self):
        state = _state(_account(1, "a@e.com", "acct-x"))
        assert AccountStore.find_by_key(state, ("a@e.com", "acct-x")).slot == 1
        # Same email, different workspace: a different account.
        assert AccountStore.find_by_key(state, ("a@e.com", "acct-y")) is None

    def test_resolve_by_slot_number(self):
        state = _state(_account(1, "a@e.com"), _account(2, "b@e.com"))
        assert AccountStore.resolve(state, "2").email == "b@e.com"

    def test_resolve_by_email_is_case_insensitive(self):
        state = _state(_account(1, "Alice@Example.com"))
        assert AccountStore.resolve(state, "alice@example.com").slot == 1

    def test_resolve_reports_an_unknown_identifier(self):
        with pytest.raises(AccountNotFoundError, match="no managed account"):
            AccountStore.resolve(_state(_account(1, "a@e.com")), "nobody@e.com")

    def test_resolve_refuses_an_ambiguous_email(self):
        """Same email in two workspaces: the user must disambiguate by slot."""
        state = _state(_account(1, "a@e.com", "acct-1"), _account(2, "a@e.com", "acct-2"))
        with pytest.raises(AccountNotFoundError, match="slots 1, 2"):
            AccountStore.resolve(state, "a@e.com")

    def test_resolve_by_slot_is_never_ambiguous(self):
        state = _state(_account(1, "a@e.com", "acct-1"), _account(2, "a@e.com", "acct-2"))
        assert AccountStore.resolve(state, "2").account_id == "acct-2"

    @pytest.mark.parametrize("identifier", ["", "   ", "0"])
    def test_empty_and_zero_identifiers_find_nothing(self, identifier):
        state = _state(_account(1, "a@e.com"))
        assert AccountStore.matches_identifier(state, identifier) == []


class TestRotation:
    def test_wraps_around(self):
        state = _state(*(_account(i, f"{i}@e.com") for i in (1, 2, 3)))
        assert AccountStore.next_in_rotation(state, 1) == 2
        assert AccountStore.next_in_rotation(state, 3) == 1

    def test_starts_at_the_first_slot_when_current_is_unknown(self):
        state = _state(_account(2, "b@e.com"), _account(5, "e@e.com"))
        assert AccountStore.next_in_rotation(state, None) == 2
        assert AccountStore.next_in_rotation(state, 99) == 2

    def test_single_account_rotates_to_itself(self):
        state = _state(_account(1, "a@e.com"))
        assert AccountStore.next_in_rotation(state, 1) == 1

    def test_empty_store_raises(self):
        with pytest.raises(StoreError, match="no accounts"):
            AccountStore.next_in_rotation(StoreState(), None)


class TestBlobs:
    def test_write_read_delete(self, store):
        account = _account(1, "a@example.com")
        store.write_blob(account, '{"x": 1}')
        assert store.read_blob(account) == '{"x": 1}'
        store.delete_blob(account)
        assert store.read_blob(account) is None

    def test_delete_is_idempotent(self, store):
        store.delete_blob(_account(1, "a@example.com"))

    def test_blob_filename_includes_slot_and_email(self, store):
        path = store.blob_path(2, "carol@example.com")
        assert path.name == "auth-2-carol@example.com.json"
        assert path.parent == store.accounts_dir


def test_log_never_raises(store, monkeypatch):
    """Logging is best-effort; a read-only store must not break a swap."""

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("builtins.open", boom)
    store.log("this must not raise")
