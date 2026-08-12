# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-08-12

A review of 0.1.0 turned up several ways to lose a credential. Everything in
"Fixed" below was reproduced before being changed and has a regression test in
`tests/test_review_regressions.py`.

### Fixed

- **`use` on the account you were already on reverted your live token.** The
  stored blob was read before the back-up step rewrote that same file, so the
  pre-refresh copy was written back over a credential Codex had since renewed.
  It is now a no-op that only refreshes the stored copy, reported as
  "Already on slot N".
- **`login --slot N` destroyed whatever occupied slot N**, with no `--force` and
  no prompt. Re-storing your own account and replacing someone else's are now
  separate permissions; `--force` was added to `login` for the latter.
- **`purge` recursively deleted whatever `--store` pointed at**, store or not.
  It now refuses a directory that does not look like a codex-swap store.
- **`--store ""` silently resolved to the default store**, so an unset variable
  in a wrapper plus `purge --yes` deleted the real one. It is now an error.
- **The legacy-store migration could delete a complete store.** The `.migrating`
  flag was treated as licence to discard the target; an interrupted
  cross-filesystem move leaves exactly the opposite arrangement. A target
  holding data is now never discarded, and the migration takes a lock.
- **`add --slot N` could duplicate an already-managed account** across two
  slots, making it unresolvable by email and rotating through it twice.
- **`add` deleted the outgoing credential before committing the new state**, so
  a failed write left the store advertising an account whose blob was gone.
- **`export` shipped a pre-refresh credential for the active account**; it now
  snapshots the live file first, as every other write path already did.
- **Process detection missed any Codex whose path contains a space** — including
  `~/Library/Application Support/...`, a standard editor-extension location.
  A false negative here means the user is told nothing is running while a live
  Codex is about to overwrite the swap.
- **A non-UTF-8 or unreadable `auth.json` produced a traceback** from every
  command, `doctor` included. It is now a clean error, and reported as a finding
  rather than a crash by `doctor`.
- An unreadable process list is now stated rather than rendered as silence,
  which read as "nothing is running".
- Replacing a present-but-unparseable `auth.json` is now reported.
- `printer.abbreviate` compared a raw string prefix, rendering `/home/bobby` as
  `~by` under a home of `/home/bob`.
- A non-positive `--slot` could be stored but never selected again.
- `OSError` from a store write escaped as a traceback instead of a message.
- `export -` dropped accounts with missing credentials silently, and
  `--json export -` emitted the bundle instead of the usual result envelope.
- `list --json` could not distinguish "no live login" from "unmanaged live
  login"; it now carries `liveEmail` and `liveManaged`.
- Deeply nested JSON raised `RecursionError` out of `parse_auth` instead of
  `AuthParseError`, so a pathological `auth.json` tracebacked out of the CLI.
- A non-string `auth_mode` was carried unvalidated into `sequence.json` and the
  `--json` output, where consumers expect a string.

### Security

- The test suite's audit-hook guard, which is what makes it impossible for a
  test run to touch the developer's real `~/.codex`, had five bypasses: byte
  strings (`os.fspath` raises `TypeError` on them, which was swallowed as
  "safe"), relative names under a `dir_fd` (how `shutil.rmtree` deletes every
  child, so a whole directory could be emptied while only the final `rmdir` was
  refused), symlinked paths (`abspath` does not follow links, and a `~/.codex`
  synced through Dropbox is one), fd-based calls, and subprocesses — which
  matters here because `login` shells out to the real `codex`. All are now
  closed, and spawning the real `codex` binary from a test is refused outright.
- The guard's own control tests aimed their write and delete probes at the
  developer's real `~/.codex`; one of them called `shutil.rmtree` on it. They
  now attack a sentinel root that is protected but names nothing on disk, so a
  guard regression fails the test instead of destroying credentials.

### Changed

- `SwitchResult` gained `already_active` and `discarded_unreadable`; the
  `switch`/`use` JSON payload gained `alreadyActive` and `discardedUnreadable`.
- `doctor --json` reports `processes.unavailable` and `configToml`.
- Removed `Account.display` and the duplicate path helpers in `paths`
  (`sequence_path`, `lock_path`, `log_path`, `accounts_dir`,
  `account_blob_path`), all unreferenced by the tool itself. `AccountStore` is
  now the single implementation of where a blob lives.

### Tests

Mutation testing of 0.1.0 found that 22 of 36 deliberate defects passed the
suite. The gaps are closed and each new test names the mutation it catches:

- Nothing asserted that any mutating operation took the store lock — the lock
  could be removed from `add` and `switch_to` with the suite still green.
- The running-Codex warning, the entire point of process detection, was
  rendered in no test: every CLI test stubbed the scan to an empty result.
- Four of `doctor`'s seven diagnostics could be deleted silently, because the
  tests asserted only that *some* problem was reported.
- The interactive confirmation prompt could be inverted — answering `n` to
  "Delete the store?" would have purged it.
- `cmd_login`, the only command that spawns a subprocess and rewrites live
  credentials, had no test at any level.
- Also now covered: atomic-write staging directory, `fsync`, identity type
  coercions, the `account_id` fallback for pre-JWT-claim blobs, rotation
  ordering, corrupt stored blobs during export, WSL detection, and
  `python -m codex_swap`.

Three assertions that could never fail were replaced with ones that can.

## [0.1.0] - 2026-08-12

First release.

### Added

- `add`, `login`, `list`, `status`, `switch`, `use`, `remove`, `export`,
  `import`, `purge` and `doctor` commands, installed as both `codex-swap` and
  `xswap`.
- `--json` on every command, so the tool can be scripted without parsing the
  human table.
- Identity (email, plan, account id, expiry) decoded locally from the `id_token`
  JWT payload — no network access on any read path.
- Support for both ChatGPT sign-in and API-key logins, and for one ChatGPT login
  backing several workspaces (accounts are keyed on email *and* account id).
- Warning about running Codex processes before a swap, since they hold the
  previous token in memory and rewrite `auth.json` when it refreshes. Node shims
  (`node /path/bin/codex …`) are detected, wrapper/native pairs are collapsed so
  one session counts once, and sandbox helpers are counted separately.
- Store locations following the XDG Base Directory Specification on Linux/WSL,
  with `$CODEX_SWAP_HOME` as an override; `$CODEX_HOME` is honoured for locating
  `auth.json`.
- One-time migration from the pre-release `~/.codex-swap-backup` layout.

[Unreleased]: https://github.com/yilunzhao/codex-swap/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/yilunzhao/codex-swap/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/yilunzhao/codex-swap/releases/tag/v0.1.0
