"""Exception hierarchy for xswap.

Every failure the CLI is expected to hit derives from :class:`CodexSwapError`,
so ``cli.main`` can turn it into a one-line message and a non-zero exit status
instead of a traceback. Anything that escapes as a bare ``Exception`` is a bug.
"""

from __future__ import annotations


class CodexSwapError(Exception):
    """Base class for every expected xswap failure."""

    exit_code = 1


class AuthParseError(CodexSwapError):
    """An auth.json blob could not be parsed as Codex authentication state."""


class AccountNotFoundError(CodexSwapError):
    """No managed account matched the given slot number or email."""

    exit_code = 2


class AccountExistsError(CodexSwapError):
    """The account (or slot) is already managed and ``--force`` was not given."""

    exit_code = 2


class ValidationError(CodexSwapError):
    """An argument was well-formed for argparse but meaningless to the store."""

    exit_code = 2


class UnreadableAuthError(CodexSwapError):
    """A credential file exists but its bytes could not be read or decoded.

    Distinct from "absent" on purpose: reporting an unreadable auth.json as "no
    login" would send the user to `codex login`, silently discarding a file that
    may hold a recoverable refresh token.
    """


class StoreError(CodexSwapError):
    """The on-disk account store is missing, unreadable, or inconsistent."""


class SwitchError(CodexSwapError):
    """A switch could not be completed; any partial change has been rolled back."""


class LockTimeout(CodexSwapError):
    """Another xswap process holds the store lock."""

    exit_code = 3


class CodexCliError(CodexSwapError):
    """The ``codex`` executable is missing or exited non-zero."""

    exit_code = 4
