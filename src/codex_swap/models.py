"""Value types shared across codex-swap."""

from __future__ import annotations

import datetime as _dt
import os
import platform as _platform
from dataclasses import dataclass, field
from enum import Enum, auto


class Platform(Enum):
    """Host platform, resolved once per call site."""

    MACOS = auto()
    LINUX = auto()
    WSL = auto()
    WINDOWS = auto()
    UNKNOWN = auto()

    @classmethod
    def detect(cls) -> Platform:
        system = _platform.system()
        if system == "Darwin":
            return cls.MACOS
        if system == "Windows":
            return cls.WINDOWS
        if system == "Linux":
            # WSL is Linux but stores data where the Linux rules say, and is
            # worth distinguishing in `doctor` output.
            if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
                return cls.WSL
            return cls.LINUX
        return cls.UNKNOWN


def utc_now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def timestamp() -> str:
    """Current UTC time as a stable, sortable ISO-8601 string."""
    return utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Identity:
    """Who an auth.json blob belongs to.

    Decoded locally from the ``id_token`` JWT payload; codex-swap never calls
    the network to work out an account's identity.
    """

    email: str
    account_id: str = ""
    plan: str = ""
    auth_mode: str = "chatgpt"
    expires_at: int | None = None
    last_refresh: str = ""

    @property
    def key(self) -> tuple[str, str]:
        """Identity key used to match a blob against a managed slot.

        Email alone is not enough: the same ChatGPT login can back several
        workspaces, and an API-key blob has no email at all.
        """
        return (self.email, self.account_id)

    @property
    def is_api_key(self) -> bool:
        return self.auth_mode == "apikey"

    def expiry(self) -> _dt.datetime | None:
        if self.expires_at is None:
            return None
        try:
            return _dt.datetime.fromtimestamp(self.expires_at, _dt.timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None

    def is_expired(self, *, now: _dt.datetime | None = None) -> bool:
        """Whether the *id token* has passed its expiry.

        An expired id token is normal and not fatal: Codex silently exchanges
        the refresh token on next use. It is surfaced only as a freshness hint.
        """
        exp = self.expiry()
        if exp is None:
            return False
        return exp <= (now or utc_now())


@dataclass
class Account:
    """A managed account: one slot in the store."""

    slot: int
    email: str
    account_id: str = ""
    plan: str = ""
    auth_mode: str = "chatgpt"
    label: str = ""
    added: str = field(default_factory=timestamp)

    @property
    def key(self) -> tuple[str, str]:
        return (self.email, self.account_id)

    @property
    def display(self) -> str:
        tag = self.plan or "?"
        return f"{self.email} [{tag}]"

    @classmethod
    def from_identity(
        cls, slot: int, identity: Identity, *, label: str = "", added: str | None = None
    ) -> Account:
        return cls(
            slot=slot,
            email=identity.email,
            account_id=identity.account_id,
            plan=identity.plan,
            auth_mode=identity.auth_mode,
            label=label,
            added=added or timestamp(),
        )

    @classmethod
    def from_dict(cls, slot: int, data: dict) -> Account:
        return cls(
            slot=slot,
            email=data.get("email", ""),
            account_id=data.get("accountId", "") or "",
            plan=data.get("plan", "") or "",
            auth_mode=data.get("authMode", "chatgpt") or "chatgpt",
            label=data.get("label", "") or "",
            added=data.get("added", "") or "",
        )

    def to_dict(self) -> dict:
        return {
            "email": self.email,
            "accountId": self.account_id,
            "plan": self.plan,
            "authMode": self.auth_mode,
            "label": self.label,
            "added": self.added,
        }
