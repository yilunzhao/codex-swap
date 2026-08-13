"""Account operations, including the invariant a swap exists to protect."""

from __future__ import annotations

import json

import pytest

from codex_account_switcher.exceptions import (
    AccountExistsError,
    AccountNotFoundError,
    AuthParseError,
    StoreError,
    SwitchError,
)
from codex_account_switcher.identity import parse_auth
from tests.conftest import make_api_key_auth, make_auth


@pytest.fixture
def seeded(switcher, live_auth):
    """Three managed accounts; alice is live and active."""
    live_auth(make_auth("alice@example.com", plan="pro", account_id="acct-alice"))
    switcher.add_live()
    switcher.add(make_auth("bob@example.com", plan="plus", account_id="acct-bob"))
    switcher.add(make_auth("carol@example.com", plan="team", account_id="acct-carol"))
    return switcher


class TestAdd:
    def test_add_live_captures_the_current_login(self, switcher, live_auth):
        live_auth(make_auth("alice@example.com"))
        result = switcher.add_live()
        assert result.account.slot == 1
        assert result.account.email == "alice@example.com"
        assert result.account.plan == "pro"
        assert switcher.store.read_blob(result.account) is not None

    def test_add_live_marks_the_slot_active(self, switcher, live_auth):
        live_auth(make_auth("alice@example.com"))
        switcher.add_live()
        assert switcher.store.load().active_slot == 1

    def test_add_live_without_a_login_explains_itself(self, switcher):
        with pytest.raises(StoreError, match="run `codex login`"):
            switcher.add_live()

    def test_slots_are_assigned_in_order(self, switcher):
        for i, email in enumerate(["a@e.com", "b@e.com", "c@e.com"], start=1):
            result = switcher.add(make_auth(email, account_id=f"acct-{email}"))
            assert result.account.slot == i

    def test_an_explicit_slot_is_honoured(self, switcher):
        result = switcher.add(make_auth("a@e.com"), slot=7)
        assert result.account.slot == 7
        assert switcher.store.load().sequence == [7]

    def test_a_freed_slot_is_reused(self, switcher):
        switcher.add(make_auth("a@e.com", account_id="a"))
        switcher.add(make_auth("b@e.com", account_id="b"))
        switcher.remove("1")
        assert switcher.add(make_auth("c@e.com", account_id="c")).account.slot == 1

    def test_duplicate_account_is_refused(self, switcher):
        switcher.add(make_auth("a@e.com", account_id="acct-a"))
        with pytest.raises(AccountExistsError, match="already managed as slot 1"):
            switcher.add(make_auth("a@e.com", account_id="acct-a"))

    def test_force_refreshes_an_existing_account_in_place(self, switcher):
        switcher.add(make_auth("a@e.com", account_id="acct-a", access_token="old"))
        result = switcher.add(
            make_auth("a@e.com", account_id="acct-a", access_token="new"), force=True
        )
        assert result.account.slot == 1
        assert result.replaced is True
        assert "new" in switcher.store.read_blob(result.account)

    def test_same_email_different_workspace_is_a_separate_account(self, switcher):
        """One ChatGPT login can back several workspaces; they are not the same."""
        switcher.add(make_auth("a@e.com", account_id="acct-personal"))
        result = switcher.add(make_auth("a@e.com", account_id="acct-work"))
        assert result.account.slot == 2
        assert len(switcher.store.load().accounts) == 2

    def test_occupied_slot_is_refused(self, switcher):
        switcher.add(make_auth("a@e.com", account_id="a"), slot=1)
        with pytest.raises(AccountExistsError, match="slot 1 is taken"):
            switcher.add(make_auth("b@e.com", account_id="b"), slot=1)

    def test_forced_slot_replacement_removes_the_old_blob(self, switcher):
        """A slot changing owner must not strand the previous credential file."""
        first = switcher.add(make_auth("a@e.com", account_id="a"), slot=1).account
        old_path = switcher.store.blob_path(1, "a@e.com")
        assert old_path.exists()
        switcher.add(make_auth("b@e.com", account_id="b"), slot=1, force=True)
        assert not old_path.exists()
        assert switcher.store.blob_path(1, "b@e.com").exists()
        assert first.email == "a@e.com"

    def test_label_is_stored(self, switcher):
        result = switcher.add(make_auth("a@e.com"), label="work laptop")
        assert result.account.label == "work laptop"

    def test_unparseable_blob_is_refused(self, switcher):
        with pytest.raises(AuthParseError):
            switcher.add("not json at all")

    def test_add_from_a_missing_file(self, switcher, tmp_path):
        with pytest.raises(StoreError, match="no such file"):
            switcher.add_from_file(tmp_path / "nope.json")

    def test_api_key_logins_can_be_managed(self, switcher):
        result = switcher.add(make_api_key_auth("sk-proj-XXXXXXbadc0f"))
        assert result.account.email == "apikey-badc0f"
        assert result.account.auth_mode == "apikey"

    def test_stored_blob_is_byte_identical(self, switcher):
        """Codex must not be able to tell a restored account from a fresh login."""
        text = make_auth("a@e.com")
        result = switcher.add(text)
        assert switcher.store.read_blob(result.account) == text


class TestRemove:
    def test_removes_account_and_blob(self, seeded):
        path = seeded.store.blob_path(2, "bob@example.com")
        assert path.exists()
        seeded.remove("2")
        assert not path.exists()
        assert 2 not in seeded.store.load().accounts

    def test_removes_by_email(self, seeded):
        seeded.remove("carol@example.com")
        assert 3 not in seeded.store.load().accounts

    def test_unknown_identifier_raises(self, seeded):
        with pytest.raises(AccountNotFoundError):
            seeded.remove("nobody@example.com")

    def test_removing_the_active_slot_moves_active_on(self, seeded):
        assert seeded.store.load().active_slot == 1
        seeded.remove("1")
        assert seeded.store.load().active_slot == 2

    def test_removing_the_last_account_clears_active(self, switcher):
        switcher.add(make_auth("a@e.com"))
        switcher.remove("1")
        state = switcher.store.load()
        assert state.active_slot is None
        assert state.sequence == []


class TestSwitch:
    def test_switch_replaces_the_live_file(self, seeded):
        seeded.switch_to("2")
        assert seeded.live_identity().email == "bob@example.com"

    def test_switch_records_the_new_active_slot(self, seeded):
        seeded.switch_to("3")
        assert seeded.store.load().active_slot == 3

    def test_switch_by_email(self, seeded):
        seeded.switch_to("carol@example.com")
        assert seeded.live_identity().email == "carol@example.com"

    def test_result_reports_the_previous_account(self, seeded):
        result = seeded.switch_to("2")
        assert result.previous.slot == 1
        assert result.target.slot == 2
        assert result.backed_up is True

    def test_refreshed_live_token_is_captured_before_the_swap(self, seeded):
        """THE invariant.

        Codex rewrites auth.json on every token refresh, so the live file is
        routinely newer than the stored copy. If a swap overwrote it without
        backing it up first, that account would quietly rot until it needed a
        fresh login.
        """
        refreshed = make_auth(
            "alice@example.com", account_id="acct-alice", access_token="at.REFRESHED"
        )
        seeded.write_live(refreshed)

        seeded.switch_to("2")

        stored = seeded.store.read_blob(seeded.store.load().accounts[1])
        assert "at.REFRESHED" in stored, "the refreshed token was lost"

    def test_switching_back_restores_the_refreshed_token(self, seeded):
        refreshed = make_auth(
            "alice@example.com", account_id="acct-alice", access_token="at.REFRESHED"
        )
        seeded.write_live(refreshed)
        seeded.switch_to("2")
        seeded.switch_to("1")
        assert "at.REFRESHED" in seeded.live_auth()

    def test_switching_to_an_unknown_account_raises(self, seeded):
        with pytest.raises(AccountNotFoundError):
            seeded.switch_to("99")

    def test_missing_stored_credentials_are_reported(self, seeded):
        seeded.store.delete_blob(seeded.store.load().accounts[2])
        with pytest.raises(SwitchError, match="no stored credentials"):
            seeded.switch_to("2")

    def test_unusable_stored_credentials_are_reported(self, seeded):
        account = seeded.store.load().accounts[2]
        seeded.store.write_blob(account, "{corrupt")
        with pytest.raises(SwitchError, match="unusable"):
            seeded.switch_to("2")

    def test_a_failed_switch_leaves_the_live_file_intact(self, seeded):
        """The live login must survive a switch that could not complete."""
        before = seeded.live_auth()
        seeded.store.delete_blob(seeded.store.load().accounts[2])
        with pytest.raises(SwitchError):
            seeded.switch_to("2")
        assert seeded.live_auth() == before
        assert seeded.store.load().active_slot == 1

    def test_switching_from_an_unmanaged_login_reports_the_loss(self, switcher, live_auth):
        switcher.add(make_auth("bob@example.com", account_id="acct-bob"))
        live_auth(make_auth("stranger@example.com", account_id="acct-stranger"))
        result = switcher.switch_to("1")
        assert result.discarded is not None
        assert result.discarded.email == "stranger@example.com"
        assert result.backed_up is False

    def test_switching_with_no_live_file_at_all(self, switcher):
        switcher.add(make_auth("bob@example.com"))
        result = switcher.switch_to("1")
        assert result.previous is None
        assert result.discarded is None
        assert switcher.live_identity().email == "bob@example.com"

    def test_switch_writes_an_owner_only_file(self, seeded):
        import sys

        if sys.platform == "win32":
            pytest.skip("POSIX modes only")
        seeded.switch_to("2")
        assert seeded.auth_file.stat().st_mode & 0o777 == 0o600


class TestRotate:
    def test_rotates_forward(self, seeded):
        assert seeded.rotate().target.slot == 2
        assert seeded.rotate().target.slot == 3

    def test_wraps_around(self, seeded):
        seeded.switch_to("3")
        assert seeded.rotate().target.slot == 1

    def test_anchors_on_the_live_file_not_the_recorded_slot(self, seeded):
        """If Codex was re-logged-in behind our back, follow reality."""
        seeded.write_live(make_auth("carol@example.com", account_id="acct-carol"))
        assert seeded.rotate().target.slot == 1  # carol is slot 3, so next wraps to 1

    def test_refuses_with_a_single_account(self, switcher, live_auth):
        live_auth(make_auth("a@e.com"))
        switcher.add_live()
        with pytest.raises(StoreError, match="only one account"):
            switcher.rotate()

    def test_refuses_with_an_empty_store(self, switcher):
        with pytest.raises(StoreError, match="no accounts are managed"):
            switcher.rotate()

    def test_a_full_cycle_returns_every_account_unchanged(self, seeded):
        """Rotating all the way round must be lossless."""
        originals = {
            slot: seeded.store.read_blob(account)
            for slot, account in seeded.store.load().accounts.items()
        }
        for _ in range(len(originals)):
            seeded.rotate()
        for slot, account in seeded.store.load().accounts.items():
            assert seeded.store.read_blob(account) == originals[slot]
        assert seeded.live_identity().email == "alice@example.com"


class TestPurge:
    def test_removes_the_store_but_not_the_live_login(self, seeded):
        live_before = seeded.live_auth()
        seeded.purge()
        assert not seeded.store.root.exists()
        assert seeded.live_auth() == live_before

    def test_is_safe_on_a_missing_store(self, switcher):
        switcher.purge()


class TestLogin:
    def test_stores_the_account_codex_logged_into(self, switcher, live_auth):
        live_auth(make_auth("old@example.com", account_id="acct-old"))

        def fake_login(command):
            assert command == ["codex", "login"]
            switcher.write_live(make_auth("new@example.com", account_id="acct-new"))
            return 0

        result = switcher.login(runner=fake_login)
        assert result.account.email == "new@example.com"
        assert switcher.store.load().active_slot == result.account.slot

    def test_snapshots_a_managed_account_before_logging_in(self, switcher, live_auth):
        """`codex login` overwrites auth.json; a refreshed token must survive it."""
        live_auth(make_auth("old@example.com", account_id="acct-old"))
        switcher.add_live()
        switcher.write_live(
            make_auth("old@example.com", account_id="acct-old", access_token="at.REFRESHED")
        )

        def fake_login(command):
            switcher.write_live(make_auth("new@example.com", account_id="acct-new"))
            return 0

        switcher.login(runner=fake_login)
        stored = switcher.store.read_blob(switcher.store.load().accounts[1])
        assert "at.REFRESHED" in stored

    def test_a_failed_login_restores_the_previous_file(self, switcher, live_auth):
        original = live_auth(make_auth("old@example.com", account_id="acct-old"))

        def failing_login(command):
            switcher.write_live("{}")  # codex half-wrote something
            return 1

        from codex_account_switcher.exceptions import CodexCliError

        with pytest.raises(CodexCliError, match="status 1"):
            switcher.login(runner=failing_login)
        assert switcher.live_auth() == original

    def test_relogin_into_the_same_account_updates_in_place(self, switcher, live_auth):
        live_auth(make_auth("a@e.com", account_id="acct-a", access_token="first"))
        switcher.add_live()

        def fake_login(command):
            switcher.write_live(make_auth("a@e.com", account_id="acct-a", access_token="second"))
            return 0

        result = switcher.login(runner=fake_login)
        assert result.account.slot == 1
        assert len(switcher.store.load().accounts) == 1
        assert "second" in switcher.store.read_blob(result.account)

    def test_missing_auth_after_login_is_reported(self, switcher):
        from codex_account_switcher.exceptions import CodexCliError

        def fake_login(command):
            return 0  # claims success, writes nothing

        with pytest.raises(CodexCliError, match="is missing"):
            switcher.login(runner=fake_login)


def test_current_account_matches_on_the_composite_key(switcher, live_auth):
    switcher.add(make_auth("a@e.com", account_id="acct-personal"))
    live_auth(make_auth("a@e.com", account_id="acct-work"))
    state = switcher.store.load()
    assert switcher.current_account(state) is None


def test_add_preserves_the_original_added_timestamp_on_refresh(switcher):
    first = switcher.add(make_auth("a@e.com", account_id="a")).account
    second = switcher.add(make_auth("a@e.com", account_id="a"), force=True).account
    assert second.added == first.added


def test_identity_key_is_what_the_store_indexes_on():
    identity = parse_auth(make_auth("a@e.com", account_id="acct-1"))
    assert identity.key == ("a@e.com", "acct-1")


def test_store_json_never_contains_a_token(seeded):
    """Only metadata belongs in sequence.json; credentials live in accounts/."""
    text = seeded.store.sequence_path.read_text(encoding="utf-8")
    payload = json.dumps(json.loads(text))
    for needle in ("id_token", "access_token", "refresh_token", "rt.", "at."):
        assert needle not in payload
