# codex-swap

Multi-account switcher for the [Codex CLI](https://github.com/openai/codex).

Codex stores its entire login in one file — `~/.codex/auth.json` — and
`codex login` simply overwrites it. There is no built-in way to keep a second
account around, so switching means logging out, logging back in, and doing it
again in reverse an hour later.

`codex-swap` keeps a numbered store of logins and rotates the live file between
them:

```console
$ codex-swap list
Managed Codex accounts  (~/.codex-swap)
 * 1  alice@example.com    pro     valid 47m
   2  bob@work.example     team    expired 2h ago (auto-refresh)   (work)
   3  apikey-badc0f        apikey  api key

$ codex-swap switch
Switched to slot 2  bob@work.example  [team]

Restart Codex (or start a new session) to pick up the new account.
```

## Install

```bash
pipx install codex-swap        # recommended
# or
pip install --user codex-swap
```

Zero runtime dependencies — it is pure standard library, so nothing can break
because a transitive dependency did. Python 3.10+.

Both `codex-swap` and the shorter `xswap` are installed.

## Usage

| Command | What it does |
| --- | --- |
| `codex-swap add` | Capture the current `auth.json` into the store |
| `codex-swap add --from PATH` | Capture a login you saved somewhere else |
| `codex-swap login` | Snapshot the current account, run `codex login`, store the new one |
| `codex-swap list` | Show every managed account and its token freshness |
| `codex-swap status` | Show the live login and which slot it belongs to |
| `codex-swap switch` | Rotate to the next account |
| `codex-swap use 2` / `use bob@work.example` | Switch to a specific account |
| `codex-swap remove 2` | Forget an account |
| `codex-swap export FILE` / `import FILE` | Move accounts between machines |
| `codex-swap doctor` | Diagnose a swap that did not stick |
| `codex-swap purge` | Delete the store (the live `auth.json` is left alone) |

Every command takes `--json` for scripting:

```console
$ codex-swap status --json | jq -r '.live.email'
alice@example.com
```

### Adding a second account

```bash
codex-swap add        # store the account you are logged into now
codex-swap login      # log into the second one; it is stored automatically
codex-swap switch     # rotate between them from here on
```

`login` snapshots the account you are currently on *before* running
`codex login`, so the overwrite that `codex login` performs cannot lose it.

## How it works

```
~/.codex/auth.json  <──swap──>  ~/.codex-swap/
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
| Linux / WSL | `$XDG_DATA_HOME/codex-swap` (default `~/.local/share/codex-swap`) |
| macOS / Windows | `~/.codex-swap` |
| Any | `$CODEX_SWAP_HOME` when set to an absolute path |

`$CODEX_HOME` is honoured for locating `auth.json`, exactly as the Codex CLI
does.

## Quit Codex before you switch

A running Codex holds its tokens in memory and writes a refreshed copy back to
`auth.json` when they near expiry. If one is alive across a swap it can
overwrite the account you just activated — the swap appears to work and then
undoes itself minutes later.

`codex-swap` warns when it finds one:

```console
$ codex-swap switch
Switched to slot 2  bob@work.example  [team]

Warning: 3 Codex process(es) still running: 2 x codex app-server, 1 x codex tui
  Each holds the previous token in memory and rewrites auth.json when
  it refreshes, which would silently undo this swap. Quit them (and any
  editor or desktop Codex integration) before using the new account.
```

Editor integrations are the usual culprit — they keep an `app-server` alive in
the background long after you have closed the panel. `codex-swap doctor` lists
what is running.

## Security

Stored logins are plain 0600 files, which is the same protection Codex itself
gives `~/.codex/auth.json` — copying a token from one 0600 file to another adds
no new exposure. They are *not* encrypted at rest, so:

* `codex-swap export` writes live OAuth tokens in the clear. Treat that file
  like a password; it is in `.gitignore` here for a reason.
* Anything with read access to your home directory can already read
  `~/.codex/auth.json`, with or without this tool.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest              # full suite
.venv/bin/ruff check .        # lint
```

The test suite never touches real Codex data. Two layers enforce that: an
autouse fixture repoints `CODEX_HOME`/`CODEX_SWAP_HOME` at a temp directory,
and a process-global `sys.addaudithook` refuses any write landing under the
real roots. The second layer exists because the first one unwinds at teardown —
see `tests/conftest.py` and the control tests in `tests/test_real_store_guard.py`.

## License

MIT — see [LICENSE](LICENSE).
