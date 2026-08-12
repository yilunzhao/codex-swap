# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/yilunzhao/codex-swap/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/yilunzhao/codex-swap/releases/tag/v0.1.0
