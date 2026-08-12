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

from codex_swap import paths
from codex_swap.exceptions import (
    AccountExistsError,
    AuthParseError,
    CodexCliError,
    StoreError,
    SwitchError,
)
from codex_swap.fsutil import read_text, write_secret
from codex_swap.identity import parse_auth, try_parse_auth
from codex_swap.locking import FileLock
from codex_swap.models import Account, Identity
from codex_swap.store import AccountStore, StoreState

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
    ) -> AddResult:
        """Capture an auth blob into the store.

        Args:
            text: raw ``auth.json`` contents.
            slot: explicit slot number; defaults to the lowest free one.
            label: free-form note shown in ``list``.
            force: overwrite an existing account or an occupied slot.
            from_live: the blob came from the live file, so the resulting slot
                is by definition the active one.

        Raises:
            AuthParseError: the blob carries no usable identity.
            AccountExistsError: the account or slot is taken and force is unset.
        """
        identity = parse_auth(text)

        with self.lock():
            state = self.store.load()
            existing = self.store.find_by_key(state, identity.key)

            if existing is not None and slot is None:
                if not force:
                    raise AccountExistsError(
                        f"{identity.email} is already managed as slot {existing.slot}; "
                        f"pass --force to refresh it"
                    )
                target_slot = existing.slot
            elif slot is not None:
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
            if old is not None and old.email != identity.email:
                # The blob file is named after the email, so a slot changing
                # owner would otherwise strand the previous file.
                self.store.delete_blob(old)

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
            raise StoreError(f"no Codex login found at {self.auth_file} — run `codex login` first")
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
                    f"slot {target.slot} ({target.email}) has no stored credentials — "
                    f"re-add it with `codex-swap login`"
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
            if original is not None and previous is not None:
                self.store.write_blob(previous, original)
                backed_up = True
            elif original is not None and original_identity is not None:
                discarded = original_identity

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
            target=target, previous=previous, backed_up=backed_up, discarded=discarded
        )

    def rotate(self) -> SwitchResult:
        """Switch to the next account in the rotation."""
        state = self.store.load()
        if not state.accounts:
            raise StoreError("no accounts are managed yet — run `codex-swap add`")
        if len(state.sequence) < 2:
            raise StoreError("only one account is managed — add another with `codex-swap login`")
        current = self.current_account(state)
        anchor = current.slot if current is not None else state.active_slot
        return self.switch_to(state.accounts[self.store.next_in_rotation(state, anchor)])

    # -- codex login ------------------------------------------------------

    def login(
        self,
        *,
        slot: int | None = None,
        label: str = "",
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
                state = self.store.load()
                known = self.store.find_by_key(state, identity.key)
                if known is not None:
                    # Refresh the stored copy first; it may be stale.
                    with self.lock():
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
        # of an account that already has a slot.
        return self.add(new_text, slot=slot, label=label, force=True, from_live=True)

    # -- transfer ---------------------------------------------------------

    def export_bundle(self) -> dict:
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
        """Delete the whole store. The live ``auth.json`` is left alone."""
        root = self.store.root
        if root.exists():
            shutil.rmtree(root)
        return root


def _run_codex_login(command: Sequence[str]) -> int:
    """Run ``codex login`` with inherited stdio so the browser flow shows."""
    try:
        return subprocess.call(list(command))
    except OSError as exc:
        raise CodexCliError(f"could not run {' '.join(command)}: {exc}") from exc
    except KeyboardInterrupt:
        return 130
