"""Account operations. No printing happens here.

Every command the CLI exposes is a method that returns a result object, so the
behaviour can be tested without capturing stdout and so a future non-terminal
front end does not have to scrape text.

The invariant that matters most is in :meth:`Switcher.switch_to`: the live
``auth.json`` is copied back into its own slot *before* being overwritten.
Codex rewrites that file every time it refreshes an access token, so the copy on
disk is routinely newer than whatever was captured when the account was added.
Skipping that step silently rots stored accounts until they need a fresh login.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from codex_account_switcher import paths
from codex_account_switcher.exceptions import (
    AccountExistsError,
    AuthParseError,
    CodexCliError,
    StoreError,
    SwitchError,
    ValidationError,
)
from codex_account_switcher.fsutil import read_text, write_secret
from codex_account_switcher.identity import parse_auth, try_parse_auth
from codex_account_switcher.locking import FileLock
from codex_account_switcher.models import Account, Identity
from codex_account_switcher.store import AccountStore, StoreState

BUNDLE_FORMAT = "codex-swap-export"
BUNDLE_VERSION = 1


@dataclass
class AddResult:
    account: Account
    replaced: bool = False


@dataclass
class SwitchResult:
    target: Account
    previous: Account | None = None
    backed_up: bool = False
    #: Set when the live login was not managed and got overwritten anyway.
    discarded: Identity | None = None
    #: The live file was present but unparseable, and has been replaced.
    discarded_unreadable: bool = False
    #: The requested account was already live; only the stored copy was refreshed.
    already_active: bool = False


@dataclass
class ImportResult:
    added: list[Account]
    skipped: list[str]


class Switcher:
    """Coordinates the live ``auth.json`` and the account store."""

    def __init__(
        self,
        store: AccountStore | None = None,
        auth_file: Path | None = None,
    ) -> None:
        self.store = store if store is not None else AccountStore()
        self._auth_file = auth_file

    # -- live state -------------------------------------------------------

    @property
    def auth_file(self) -> Path:
        return self._auth_file if self._auth_file is not None else paths.auth_path()

    def live_auth(self) -> str | None:
        return read_text(self.auth_file)

    def live_identity(self) -> Identity | None:
        return try_parse_auth(self.live_auth())

    def write_live(self, text: str) -> None:
        write_secret(self.auth_file, text)

    def current_account(self, state: StoreState) -> Account | None:
        """The managed slot the live file currently belongs to, if any."""
        identity = self.live_identity()
        if identity is None:
            return None
        return self.store.find_by_key(state, identity.key)

    def lock(self) -> FileLock:
        return FileLock(self.store.lock_path)

    # -- add / remove -----------------------------------------------------

    def add(
        self,
        text: str,
        *,
        slot: int | None = None,
        label: str = "",
        force: bool = False,
        from_live: bool = False,
        refresh_existing: bool = False,
    ) -> AddResult:
        """Capture an auth blob into the store.

        Args:
            text: raw ``auth.json`` contents.
            slot: explicit slot number; defaults to the lowest free one.
            label: free-form note shown in ``list``.
            force: replace a *different* account already sitting in the way.
            from_live: the blob came from the live file, so the resulting slot
                is by definition the active one.
            refresh_existing: update this same identity in place if it is already
                managed. Separate from ``force`` so that re-storing an account
                you already have never implies permission to destroy someone
                else's credentials. That distinction is what ``login`` depends on.

        Raises:
            ValidationError: the slot number is not a usable slot.
            AuthParseError: the blob carries no usable identity.
            AccountExistsError: the account or slot is taken and no permission
                to replace it was given.
        """
        if slot is not None and slot < 1:
            raise ValidationError(
                f"slot must be 1 or greater (got {slot}); a non-positive slot could "
                f"be stored but never selected again"
            )
        identity = parse_auth(text)

        with self.lock():
            state = self.store.load()
            existing = self.store.find_by_key(state, identity.key)

            if existing is not None and slot is None:
                if not (force or refresh_existing):
                    raise AccountExistsError(
                        f"{identity.email} is already managed as slot {existing.slot}; "
                        f"pass --force to refresh it"
                    )
                target_slot = existing.slot
            elif slot is not None:
                # An identity already parked in a different slot must not be
                # silently duplicated: two slots holding one account make it
                # unresolvable by email and rotate through the same login twice.
                if existing is not None and existing.slot != slot:
                    if not (force or refresh_existing):
                        raise AccountExistsError(
                            f"{identity.email} is already managed as slot {existing.slot}; "
                            f"pass --force to move it to slot {slot}"
                        )
                    self.store.delete_blob(existing)
                    del state.accounts[existing.slot]
                    state.sequence = [s for s in state.sequence if s in state.accounts]
                occupant = state.accounts.get(slot)
                if occupant is not None and occupant.key != identity.key and not force:
                    raise AccountExistsError(
                        f"slot {slot} is taken by {occupant.email}; pass --force to replace it"
                    )
                target_slot = slot
            else:
                target_slot = self.store.next_free_slot(state)

            replaced = target_slot in state.accounts
            old = state.accounts.get(target_slot)

            account = Account.from_identity(
                target_slot,
                identity,
                label=label or (old.label if old is not None and not label else ""),
                added=old.added if old is not None and old.added else None,
            )
            self.store.write_blob(account, text)

            state.accounts[target_slot] = account
            if target_slot not in state.sequence:
                state.sequence.append(target_slot)
                state.sequence.sort()
            if from_live or state.active_slot is None:
                state.active_slot = target_slot
            self.store.save(state)

            # Only once the new state is committed. Deleting first would, on a
            # failed save, leave sequence.json advertising an account whose
            # credentials no longer exist.
            if old is not None and old.email != identity.email:
                self.store.delete_blob(old)

        self.store.log(f"add slot={account.slot} email={account.email} replaced={replaced}")
        return AddResult(account=account, replaced=replaced)

    def add_from_file(self, path: Path, **kwargs) -> AddResult:
        text = read_text(path)
        if text is None:
            raise StoreError(f"no such file: {path}")
        return self.add(text, **kwargs)

    def add_live(self, **kwargs) -> AddResult:
        text = self.live_auth()
        if text is None:
            raise StoreError(f"no Codex login found at {self.auth_file}; run `codex login` first")
        kwargs.setdefault("from_live", True)
        return self.add(text, **kwargs)

    def remove(self, identifier: str) -> Account:
        with self.lock():
            state = self.store.load()
            account = self.store.resolve(state, identifier)
            self.store.delete_blob(account)
            del state.accounts[account.slot]
            state.sequence = [s for s in state.sequence if s in state.accounts]
            if state.active_slot == account.slot:
                state.active_slot = state.sequence[0] if state.sequence else None
            self.store.save(state)
        self.store.log(f"remove slot={account.slot} email={account.email}")
        return account

    # -- switching --------------------------------------------------------

    def switch_to(self, identifier: str | Account) -> SwitchResult:
        """Make the given account the live Codex login."""
        with self.lock():
            state = self.store.load()
            target = (
                identifier
                if isinstance(identifier, Account)
                else self.store.resolve(state, identifier)
            )
            if target.slot not in state.accounts:
                raise StoreError(f"slot {target.slot} is not in the store")
            target = state.accounts[target.slot]

            blob = self.store.read_blob(target)
            if not blob:
                raise SwitchError(
                    f"slot {target.slot} ({target.email}) has no stored credentials; "
                    f"re-add it with `xswap login`"
                )
            try:
                parse_auth(blob)
            except AuthParseError as exc:
                raise SwitchError(
                    f"stored credentials for slot {target.slot} are unusable: {exc}"
                ) from exc

            original = self.live_auth()
            original_identity = try_parse_auth(original)
            previous = (
                self.store.find_by_key(state, original_identity.key)
                if original_identity is not None
                else None
            )

            # Capture the (possibly token-refreshed) live blob before it is lost.
            backed_up = False
            discarded = None
            discarded_unreadable = False
            if original is not None and previous is not None:
                self.store.write_blob(previous, original)
                backed_up = True
            elif original is not None and original_identity is not None:
                discarded = original_identity
            elif original is not None:
                # Present but unparseable. Still being destroyed, so still worth
                # saying out loud: an unknown token shape is likelier than junk.
                discarded_unreadable = True

            if previous is not None and previous.slot == target.slot:
                # Already on this account. `blob` was read before the back-up
                # above rewrote that very file, so writing it now would push the
                # pre-refresh token back over a live one Codex has since
                # renewed, losing exactly what the back-up just preserved.
                state.active_slot = target.slot
                self.store.save(state)
                self.store.log(f"switch no-op slot={target.slot} (already active)")
                return SwitchResult(
                    target=target,
                    previous=previous,
                    backed_up=backed_up,
                    already_active=True,
                )

            self.write_live(blob)
            try:
                state.active_slot = target.slot
                self.store.save(state)
            except Exception as exc:  # pragma: no cover - defensive
                if original is not None:
                    # Best effort: the original error is the one worth raising.
                    with contextlib.suppress(Exception):
                        self.write_live(original)
                raise SwitchError(
                    f"could not record the new active slot, rolled back: {exc}"
                ) from exc

        self.store.log(
            f"switch to={target.slot} from={previous.slot if previous else None} "
            f"backed_up={backed_up}"
        )
        return SwitchResult(
            target=target,
            previous=previous,
            backed_up=backed_up,
            discarded=discarded,
            discarded_unreadable=discarded_unreadable,
        )

    def rotate(self) -> SwitchResult:
        """Switch to the next account in the rotation."""
        state = self.store.load()
        if not state.accounts:
            raise StoreError("no accounts are managed yet; run `xswap add`")
        if len(state.sequence) < 2:
            raise StoreError("only one account is managed; add another with `xswap login`")
        current = self.current_account(state)
        anchor = current.slot if current is not None else state.active_slot
        return self.switch_to(state.accounts[self.store.next_in_rotation(state, anchor)])

    # -- codex login ------------------------------------------------------

    def login(
        self,
        *,
        slot: int | None = None,
        label: str = "",
        force: bool = False,
        runner: Callable[[Sequence[str]], int] | None = None,
    ) -> AddResult:
        """Snapshot the current login, run ``codex login``, store the result.

        Args:
            runner: injected for tests; defaults to running the real CLI with
                inherited stdio so the browser flow is visible to the user.
        """
        run = runner or _run_codex_login
        if runner is None and shutil.which("codex") is None:
            raise CodexCliError("the `codex` executable was not found on PATH")

        original = self.live_auth()
        if original is not None:
            identity = try_parse_auth(original)
            if identity is not None:
                # Load and write under one lock: a concurrent `remove` between
                # the two would otherwise recreate a credential file for an
                # account no longer in sequence.json.
                with self.lock():
                    known = self.store.find_by_key(self.store.load(), identity.key)
                    if known is not None:
                        self.store.write_blob(known, original)

        code = run(["codex", "login"])
        if code != 0:
            if original is not None:
                self.write_live(original)
            raise CodexCliError(f"`codex login` exited with status {code}")

        new_text = self.live_auth()
        if new_text is None:
            raise CodexCliError(f"`codex login` reported success but {self.auth_file} is missing")
        # Whatever was just logged into should be stored, including a re-login
        # of an account that already has a slot, but via `refresh_existing`, not
        # `force`: re-storing your own account must never carry permission to
        # overwrite a *different* account that happens to occupy `slot`.
        return self.add(
            new_text,
            slot=slot,
            label=label,
            force=force,
            refresh_existing=True,
            from_live=True,
        )

    # -- transfer ---------------------------------------------------------

    def export_bundle(self) -> dict:
        # Snapshot live first. Codex rewrites auth.json on every token refresh,
        # so without this the bundle ships a pre-refresh credential for the one
        # account the user is most likely to still be using.
        with self.lock():
            state = self.store.load()
            original = self.live_auth()
            identity = try_parse_auth(original)
            if identity is not None:
                live_account = self.store.find_by_key(state, identity.key)
                if live_account is not None:
                    self.store.write_blob(live_account, original)

        state = self.store.load()
        accounts = []
        missing = []
        for slot in sorted(state.accounts):
            account = state.accounts[slot]
            blob = self.store.read_blob(account)
            if not blob:
                missing.append(f"slot {slot} ({account.email})")
                continue
            try:
                auth = json.loads(blob)
            except ValueError:
                missing.append(f"slot {slot} ({account.email})")
                continue
            accounts.append({"slot": slot, "meta": account.to_dict(), "auth": auth})
        return {
            "format": BUNDLE_FORMAT,
            "version": BUNDLE_VERSION,
            "accounts": accounts,
            "missing": missing,
        }

    def import_bundle(self, bundle: dict, *, force: bool = False) -> ImportResult:
        if not isinstance(bundle, dict) or bundle.get("format") != BUNDLE_FORMAT:
            raise StoreError("not a codex-swap export bundle")
        entries = bundle.get("accounts")
        if not isinstance(entries, list):
            raise StoreError("export bundle has no accounts list")

        added: list[Account] = []
        skipped: list[str] = []
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("auth"), dict):
                skipped.append("malformed entry")
                continue
            text = json.dumps(entry["auth"], indent=2)
            meta = entry.get("meta") if isinstance(entry.get("meta"), dict) else {}
            try:
                result = self.add(
                    text,
                    slot=None,
                    label=str(meta.get("label", "") or ""),
                    force=force,
                )
            except AccountExistsError as exc:
                skipped.append(str(exc))
                continue
            except AuthParseError as exc:
                skipped.append(f"unreadable credentials: {exc}")
                continue
            added.append(result.account)
        return ImportResult(added=added, skipped=skipped)

    # -- destructive ------------------------------------------------------

    def purge(self) -> Path:
        """Delete the whole store. The live ``auth.json`` is left alone.

        Refuses a directory that does not look like an xswap store. `--store`
        is a plain path, so a mistyped or unset variable would otherwise turn
        `purge --yes` into an unbounded recursive delete of whatever it pointed
        at.
        """
        root = self.store.root
        if not root.exists():
            return root
        if not self.store.looks_like_store():
            raise StoreError(
                f"{root} does not look like an xswap store (no sequence.json and "
                f"no accounts directory); refusing to delete it"
            )
        # The lock file itself is removed after the lock is released, so this
        # works on Windows too, where an open file cannot be unlinked.
        with self.lock():
            for entry in root.iterdir():
                if entry == self.store.lock_path:
                    continue
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
        self.store.lock_path.unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            root.rmdir()
        return root


def _run_codex_login(command: Sequence[str]) -> int:
    """Run ``codex login`` with inherited stdio so the browser flow shows."""
    try:
        return subprocess.call(list(command))
    except OSError as exc:
        raise CodexCliError(f"could not run {' '.join(command)}: {exc}") from exc
    except KeyboardInterrupt:
        return 130
