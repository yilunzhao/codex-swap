"""Detecting Codex processes that would fight a swap.

A running Codex holds its OAuth tokens in memory and writes a refreshed copy
back to ``auth.json`` when they near expiry. If one is alive across a swap it
can overwrite the account that was just activated — the swap appears to work and
then silently undoes itself minutes later. Every mutating command therefore
reports what is running.

Three things make this harder than grepping for "codex":

* The npm distribution launches the real binary through a Node shim, so the
  process listing shows ``node /path/bin/codex app-server`` — argv[0] is
  ``node``. Matching only argv[0] misses it.
* That shim then spawns the native binary, so one logical session shows up
  twice. A holder whose parent is also a holder is a wrapper and is dropped,
  which keeps the reported count equal to the number of real sessions.
* Sandbox and exec helpers (``codex-code-mode-host`` and friends) run under a
  session but never touch credentials, so they are counted separately and never
  raise the alarm. Unrelated binaries that merely start with "codex" must not be
  counted at all.
"""

from __future__ import annotations

import os
import subprocess
from collections import Counter
from dataclasses import dataclass, field

#: Helper binaries that live under a Codex session but hold no credentials.
HELPER_NAMES = frozenset(
    {
        "codex-code-mode-host",
        "codex-linux-sandbox",
        "codex-responses-api-proxy",
        "codex-execpolicy",
    }
)

#: Sub-commands of the `codex` binary, as of Codex CLI 0.147. Used as an
#: allowlist so that a prompt (`codex "fix the bug"`) or a flag value
#: (`codex -c model=o3`) is never mistaken for a sub-command.
SUBCOMMANDS = frozenset(
    {
        "exec",
        "e",
        "review",
        "login",
        "logout",
        "mcp",
        "plugin",
        "mcp-server",
        "app-server",
        "remote-control",
        "app",
        "completion",
        "update",
        "doctor",
        "sandbox",
        "debug",
        "apply",
        "a",
        "resume",
        "archive",
        "delete",
        "unarchive",
        "fork",
        "cloud",
        "exec-server",
        "features",
        "help",
    }
)

#: Interpreters that may front the real binary in the process listing.
_LAUNCHERS = frozenset({"node", "npm", "npx", "bun", "deno", "python", "python3"})

#: How far into argv to look for the real binary. argv[0] covers a direct
#: launch, argv[1] covers `node /path/to/codex`; beyond that a match would more
#: likely be a filename the user passed as an argument.
_MAX_LAUNCHER_DEPTH = 2

_PS_TIMEOUT = 5


def _basename(token: str) -> str:
    name = os.path.basename(token.replace("\\", "/"))
    if name.lower().endswith(".exe"):
        name = name[:-4]
    return name


@dataclass
class CodexProcess:
    pid: int
    name: str
    #: Sub-command for the core binary: "app-server", "exec", "tui", ...
    role: str = ""
    ppid: int | None = None

    @property
    def label(self) -> str:
        return f"{self.name} {self.role}".strip()


@dataclass
class ProcessScan:
    """Result of one scan. ``holders`` are the ones that can undo a swap."""

    holders: list[CodexProcess] = field(default_factory=list)
    helpers: list[CodexProcess] = field(default_factory=list)
    #: True when the platform's process listing could not be read at all.
    unavailable: bool = False

    def __bool__(self) -> bool:
        return bool(self.holders)

    def summary(self) -> str:
        counts = Counter(p.label for p in self.holders)
        return ", ".join(f"{n} x {label}" for label, n in counts.most_common())


def _run(command: list[str]) -> str | None:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=_PS_TIMEOUT,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout


def _role_of(args: list[str]) -> str:
    """The sub-command a `codex` invocation is running, or "tui"."""
    for token in args:
        if token in SUBCOMMANDS:
            return token
    return "tui"


def _classify(pid: int, argv: list[str], scan: ProcessScan, ppid: int | None = None) -> None:
    """Add ``argv`` to ``scan`` if it is a Codex process."""
    if not argv:
        return
    for index, token in enumerate(argv[:_MAX_LAUNCHER_DEPTH]):
        name = _basename(token)
        if name in HELPER_NAMES:
            scan.helpers.append(CodexProcess(pid=pid, name=name, ppid=ppid))
            return
        if name == "codex":
            scan.holders.append(
                CodexProcess(pid=pid, name="codex", role=_role_of(argv[index + 1 :]), ppid=ppid)
            )
            return
        # Only keep looking past argv[0] when it is an interpreter that could be
        # fronting the real binary.
        if index == 0 and name not in _LAUNCHERS:
            return


def _drop_wrapper_processes(scan: ProcessScan) -> None:
    """Collapse `node -> native` pairs so one session counts once."""
    holder_pids = {p.pid for p in scan.holders}
    scan.holders = [p for p in scan.holders if p.ppid is None or p.ppid not in holder_pids]


def _scan_posix(exclude: set[int]) -> ProcessScan:
    scan = ProcessScan()
    out = _run(["ps", "-axo", "pid=,ppid=,command="])
    if out is None:
        scan.unavailable = True
        return scan
    for line in out.splitlines():
        fields = line.strip().split(None, 2)
        if len(fields) < 3 or not fields[0].isdigit():
            continue
        pid = int(fields[0])
        if pid in exclude:
            continue
        ppid = int(fields[1]) if fields[1].isdigit() else None
        _classify(pid, fields[2].split(), scan, ppid)
    _drop_wrapper_processes(scan)
    return scan


def _scan_windows(exclude: set[int]) -> ProcessScan:  # pragma: no cover - CI job only
    scan = ProcessScan()
    # `tasklist` gives no command line, so the sub-command role and the parent
    # pid are both unavailable; the image name is enough to warn about a live
    # Codex, which is what the caller acts on.
    out = _run(["tasklist", "/FO", "CSV", "/NH"])
    if out is None:
        scan.unavailable = True
        return scan
    for line in out.splitlines():
        fields = [f.strip('"') for f in line.split('","')]
        if len(fields) < 2:
            continue
        image = fields[0].strip('"')
        try:
            pid = int(fields[1].strip('"'))
        except ValueError:
            continue
        if pid in exclude:
            continue
        _classify(pid, [image], scan)
    return scan


def scan(exclude_self: bool = True) -> ProcessScan:
    """List running Codex processes, split into token holders and helpers."""
    exclude = {os.getpid()} if exclude_self else set()
    if os.name == "nt":  # pragma: no cover - CI job only
        return _scan_windows(exclude)
    return _scan_posix(exclude)
