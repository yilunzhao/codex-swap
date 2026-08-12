"""The on-disk account store.

Layout (see :mod:`codex_swap.paths` for where the root lives)::

    <store_root>/
        sequence.json                       rotation order, active slot, metadata
        accounts/auth-<slot>-<email>.json   one captured auth.json per account
        .lock                               advisory lock for mutations
        codex-swap.log                       append-only operation log

``sequence.json`` holds only metadata — email, plan, label — so it can be read
for ``list`` without ever opening a credential file. The credential blobs are
byte-for-byte copies of what Codex itself wrote, which is what lets a restored
account work without Codex noticing anything happened.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from codex_swap import paths
from codex_swap.exceptions import AccountNotFoundError, StoreError
from codex_swap.fsutil import ensure_private_dir, read_text, write_secret
from codex_swap.models import Account, timestamp

SCHEMA_VERSION = 1


@dataclass
class StoreState:
    """Parsed ``sequence.json``."""

    active_slot: int | None = None
    sequence: list[int] = field(default_factory=list)
    accounts: dict[int, Account] = field(default_factory=dict)
    last_updated: str = ""
    version: int = SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "activeSlot": self.active_slot,
            "lastUpdated": self.last_updated or timestamp(),
            "sequence": list(self.sequence),
            "accounts": {str(s): a.to_dict() for s, a in sorted(self.accounts.items())},
        }

    @classmethod
    def from_dict(cls, data: dict) -> StoreState:
        accounts: dict[int, Account] = {}
        raw_accounts = data.get("accounts")
        if isinstance(raw_accounts, dict):
            for key, value in raw_accounts.items():
                try:
                    slot = int(key)
                except (TypeError, ValueError):
                    continue
                if isinstance(value, dict):
                    accounts[slot] = Account.from_dict(slot, value)

        raw_active = data.get("activeSlot", data.get("activeAccountNumber"))
        try:
            active = int(raw_active) if raw_active is not None else None
        except (TypeError, ValueError):
            active = None
        if active is not None and active not in accounts:
            active = None

        sequence: list[int] = []
        for item in data.get("sequence") or []:
            try:
                slot = int(item)
            except (TypeError, ValueError):
                continue
            if slot in accounts and slot not in sequence:
                sequence.append(slot)
        # Any account missing from `sequence` still has to be reachable by
        # rotation, so it is appended rather than silently skipped.
        for slot in sorted(accounts):
            if slot not in sequence:
                sequence.append(slot)

        version = data.get("version")
        return cls(
            active_slot=active,
            sequence=sequence,
            accounts=accounts,
            last_updated=data.get("lastUpdated", "") or "",
            version=version if isinstance(version, int) else SCHEMA_VERSION,
        )


class AccountStore:
    """Read/write access to the store. Mutations are the caller's to lock."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root is not None else paths.store_root()

    # -- locations -------------------------------------------------------

    @property
    def sequence_path(self) -> Path:
        return self.root / "sequence.json"

    @property
    def accounts_dir(self) -> Path:
        return self.root / "accounts"

    @property
    def lock_path(self) -> Path:
        return self.root / ".lock"

    @property
    def log_path(self) -> Path:
        return self.root / "codex-swap.log"

    def blob_path(self, slot: int, email: str) -> Path:
        return self.accounts_dir / f"auth-{slot}-{paths.slugify(email)}.json"

    def exists(self) -> bool:
        return self.sequence_path.exists()

    def looks_like_store(self) -> bool:
        """Whether ``root`` plausibly holds a codex-swap store.

        Deliberately lenient — a store whose sequence.json was lost is still a
        store — but never true for an arbitrary directory of unrelated files.
        """
        if self.sequence_path.exists() or self.accounts_dir.is_dir():
            return True
        if not self.root.exists():
            return False
        return not any(self.root.iterdir())

    # -- state -----------------------------------------------------------

    def load(self) -> StoreState:
        text = read_text(self.sequence_path)
        if text is None:
            return StoreState()
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise StoreError(
                f"{self.sequence_path} is not valid JSON ({exc}). "
                f"Move it aside and re-add your accounts."
            ) from exc
        if not isinstance(data, dict):
            raise StoreError(f"{self.sequence_path} does not contain a JSON object")
        return StoreState.from_dict(data)

    def save(self, state: StoreState) -> None:
        ensure_private_dir(self.root)
        state.last_updated = timestamp()
        payload = json.dumps(state.to_dict(), indent=2) + "\n"
        # Validate before it lands: a store that will not parse is worse than a
        # failed write, because the failure surfaces on the *next* command.
        json.loads(payload)
        write_secret(self.sequence_path, payload)

    # -- credential blobs -------------------------------------------------

    def read_blob(self, account: Account) -> str | None:
        return read_text(self.blob_path(account.slot, account.email))

    def write_blob(self, account: Account, text: str) -> None:
        ensure_private_dir(self.accounts_dir)
        write_secret(self.blob_path(account.slot, account.email), text)

    def delete_blob(self, account: Account) -> None:
        path = self.blob_path(account.slot, account.email)
        with contextlib.suppress(FileNotFoundError):
            path.unlink()

    # -- lookups ----------------------------------------------------------

    @staticmethod
    def next_free_slot(state: StoreState) -> int:
        slot = 1
        while slot in state.accounts:
            slot += 1
        return slot

    @staticmethod
    def find_by_key(state: StoreState, key: tuple[str, str]) -> Account | None:
        for account in state.accounts.values():
            if account.key == key:
                return account
        return None

    @staticmethod
    def matches_identifier(state: StoreState, identifier: str) -> list[Account]:
        """Accounts matching a slot number or an email (exact, case-insensitive)."""
        identifier = identifier.strip()
        if not identifier:
            return []
        if identifier.isdigit():
            account = state.accounts.get(int(identifier))
            return [account] if account else []
        wanted = identifier.lower()
        return [a for a in state.accounts.values() if a.email.lower() == wanted]

    @classmethod
    def resolve(cls, state: StoreState, identifier: str) -> Account:
        """Resolve to exactly one account or explain why it could not be done."""
        found = cls.matches_identifier(state, identifier)
        if not found:
            raise AccountNotFoundError(f"no managed account matches {identifier!r}")
        if len(found) > 1:
            slots = ", ".join(str(a.slot) for a in sorted(found, key=lambda a: a.slot))
            raise AccountNotFoundError(
                f"{identifier!r} matches several accounts (slots {slots}); "
                f"use the slot number instead"
            )
        return found[0]

    @staticmethod
    def next_in_rotation(state: StoreState, current: int | None) -> int:
        """The slot after ``current``, wrapping around."""
        sequence = [s for s in state.sequence if s in state.accounts]
        if not sequence:
            raise StoreError("no accounts are managed yet")
        if current is None or current not in sequence:
            return sequence[0]
        return sequence[(sequence.index(current) + 1) % len(sequence)]

    # -- logging ----------------------------------------------------------

    def log(self, message: str) -> None:
        """Append one line to the operation log. Never raises."""
        try:
            ensure_private_dir(self.root)
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(f"{timestamp()} {message}\n")
        except OSError:
            pass
