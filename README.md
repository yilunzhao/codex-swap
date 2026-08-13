# xswap

[![CI](https://github.com/yilunzhao/codex-swap/actions/workflows/ci.yml/badge.svg)](https://github.com/yilunzhao/codex-swap/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Multi-account switcher for the [Codex CLI](https://github.com/openai/codex).

Codex stores its entire login in one file, `~/.codex/auth.json`, and `codex
login` simply overwrites it. There is no built-in way to keep a second account
around, so switching means logging out, logging back in, and doing it again in
reverse an hour later.

`xswap` keeps a numbered store of logins and rotates the live file between them:

```console
$ xswap list
Managed Codex accounts  (~/.xswap)
 * 1  alice@example.com    pro     valid 47m
   2  bob@work.example     team    expired 2h ago (auto-refresh)   (work)
   3  apikey-badc0f        apikey  api key

$ xswap switch
Switched to slot 2  bob@work.example  [team]

Restart Codex (or start a new session) to pick up the new account.
```

## Install

```bash
pipx install codex-account-switcher        # recommended
# or
pip install --user codex-account-switcher
```

The distribution is `codex-account-switcher`; the command it installs is
`xswap`. See [Not to be confused with](#not-to-be-confused-with) for why the two
names differ.

Zero runtime dependencies. It is pure standard library, so nothing can break
because a transitive dependency did. Python 3.10+.

## Usage

| Command | What it does |
| --- | --- |
| `xswap add` | Capture the current `auth.json` into the store |
| `xswap add --from PATH` | Capture a login you saved somewhere else |
| `xswap login` | Snapshot the current account, run `codex login`, store the new one |
| `xswap list` | Show every managed account and its token freshness |
| `xswap status` | Show the live login and which slot it belongs to |
| `xswap switch` | Rotate to the next account |
| `xswap use 2` / `use bob@work.example` | Switch to a specific account |
| `xswap remove 2` | Forget an account |
| `xswap export FILE` / `import FILE` | Move accounts between machines |
| `xswap doctor` | Diagnose a swap that did not stick |
| `xswap purge` | Delete the store (the live `auth.json` is left alone) |

Every command takes `--json` for scripting, before or after the sub-command:

```console
$ xswap status --json | jq -r '.live.email'
alice@example.com
```

### Adding a second account

```bash
xswap add        # store the account you are logged into now
xswap login      # log into the second one; it is stored automatically
xswap switch     # rotate between them from here on
```

`login` snapshots the account you are currently on *before* running `codex
login`, so the overwrite that `codex login` performs cannot lose it.

## How it works

```
~/.codex/auth.json  <──swap──>  ~/.xswap/
                                    sequence.json                     rotation order, active slot
                                    accounts/auth-1-alice@example.com.json
                                    accounts/auth-2-bob@work.example.json
```

* **Identity is read locally.** Email, plan and account id come from the
  unverified payload of the `id_token` JWT that Codex already wrote to disk.
  `list` and `status` never touch the network and work offline.
* **The live file is backed up before it is replaced.** Codex rewrites
  `auth.json` every time it refreshes an access token, so the copy on disk is
  routinely newer than whatever was captured when the account was added. A swap
  that skipped this step would silently rot stored accounts until each one
  needed a fresh login.
* **Writes are atomic.** Every write goes to a temp file in the destination
  directory and then `os.replace`, created 0600 from the start so the token is
  never briefly world-readable. A crash mid-swap leaves either the old file or
  the new one, never half of either.
* **Mutations take an advisory lock**, so two terminals cannot interleave a
  backup with an overwrite.

### Store location

| Platform | Path |
| --- | --- |
| Linux / WSL | `$XDG_DATA_HOME/xswap` (default `~/.local/share/xswap`) |
| macOS / Windows | `~/.xswap` |
| Any | `$XSWAP_HOME` when set to an absolute path |

`$CODEX_HOME` is honoured for locating `auth.json`, exactly as the Codex CLI
does. Stores written by earlier versions (`~/.codex-swap`, and the pre-1.0
`~/.codex-swap-backup`) are migrated automatically on first run.

## Quit Codex before you switch

A running Codex holds its tokens in memory and writes a refreshed copy back to
`auth.json` when they near expiry. If one is alive across a swap it can
overwrite the account you just activated: the swap appears to work and then
undoes itself minutes later.

`xswap` warns when it finds one:

```console
$ xswap switch
Switched to slot 2  bob@work.example  [team]

Warning: 3 Codex process(es) still running: 2 x codex app-server, 1 x codex tui
  Each holds the previous token in memory and rewrites auth.json when
  it refreshes, which would silently undo this swap. Quit them (and any
  editor or desktop Codex integration) before using the new account.
```

Editor integrations are the usual culprit; they keep an `app-server` alive in
the background long after you have closed the panel. `xswap doctor` lists what
is running.

## Not to be confused with

[`aneym/codex-swap`](https://github.com/aneym/codex-swap) is a different project
with a similar goal. It tracks each account's 5-hour and 7-day rate-limit usage
and auto-picks the least-used one every time you launch Codex through its `cx`
wrapper. If what you want is *never think about which account to use*, that one
probably suits you better.

This project is the manual counterpart: explicit rotation, a scriptable `--json`
interface on every command, `export`/`import` for moving accounts between
machines, and a `doctor` for when a swap does not stick. It does not measure
usage and does not launch Codex for you.

The two are not installable side by side under one Python environment without
care: both used to keep accounts in `~/.codex-swap`. This project has moved to
`~/.xswap`, migrates its own old store forward, and leaves a `~/.codex-swap`
belonging to the other project strictly alone (the two layouts are
distinguishable on disk, and `purge` refuses anything it does not recognise).

## Security

Stored logins are plain 0600 files, which is the same protection Codex itself
gives `~/.codex/auth.json`. Copying a token from one 0600 file to another adds
no new exposure. They are *not* encrypted at rest, so:

* `xswap export` writes live OAuth tokens in the clear. Treat that file like a
  password; it is in `.gitignore` here for a reason.
* Anything with read access to your home directory can already read
  `~/.codex/auth.json`, with or without this tool.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest              # full suite
.venv/bin/ruff check .        # lint
```

The test suite never touches real Codex data. Two layers enforce that: an
autouse fixture repoints `CODEX_HOME`/`XSWAP_HOME` at a temp directory, and a
process-global `sys.addaudithook` refuses any write landing under the real
roots, including through byte-string paths, `dir_fd`-relative names, symlinks
and subprocesses. The second layer exists because the first one unwinds at
teardown. See `tests/conftest.py` and the control tests in
`tests/test_real_store_guard.py`.

## License

MIT. See [LICENSE](LICENSE).
