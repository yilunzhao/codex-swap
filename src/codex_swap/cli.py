"""Command-line interface.

Presentation only: every command resolves its arguments, calls into
:class:`codex_swap.switcher.Switcher`, and renders the result. Business rules
live in the switcher and the store so they can be tested without stdout.

Both a human table and ``--json`` are produced from the same result objects, so
the two can never drift apart.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from codex_swap import __version__, paths, printer, process_detection
from codex_swap.exceptions import CodexSwapError, StoreError, ValidationError
from codex_swap.fsutil import is_private, read_text, write_secret
from codex_swap.identity import try_parse_auth
from codex_swap.models import Identity, Platform
from codex_swap.printer import bold, cyan, dim, green, red, yellow
from codex_swap.store import AccountStore
from codex_swap.switcher import Switcher

RESTART_HINT = "Restart Codex (or start a new session) to pick up the new account."


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _emit(args: argparse.Namespace, payload: dict) -> None:
    """Print a JSON document when --json is active. No-op otherwise."""
    if args.json:
        print(json.dumps(payload, indent=2, default=str))


def _confirm(args: argparse.Namespace, question: str) -> bool:
    if args.yes:
        return True
    if args.json or not sys.stdin.isatty():
        raise CodexSwapError(
            "refusing to prompt in a non-interactive session; pass --yes to confirm"
        )
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _token_state(identity: Identity | None) -> str:
    if identity is None:
        return dim("unreadable")
    if identity.is_api_key:
        return dim("api key")
    expiry = identity.expiry()
    if expiry is None:
        return dim("no expiry")
    if identity.is_expired():
        return yellow(f"expired {printer.format_age(expiry)}") + dim(" (auto-refresh)")
    import datetime as _dt

    remaining = (expiry - _dt.datetime.now(_dt.timezone.utc)).total_seconds()
    return dim(f"valid {printer.format_span(remaining)}")


def _account_payload(account, identity: Identity | None = None) -> dict:
    payload = account.to_dict()
    payload["slot"] = account.slot
    if identity is not None:
        payload["expiresAt"] = identity.expires_at
        payload["expired"] = identity.is_expired()
    return payload


def _stored_line(verb: str, account) -> str:
    plan = dim(f"[{account.plan or '?'}]")
    return f"{green(verb)} slot {bold(str(account.slot))}  {account.email}  {plan}"


def _report_processes(args: argparse.Namespace) -> dict:
    scan = process_detection.scan()
    payload = {
        "holders": [{"pid": p.pid, "label": p.label} for p in scan.holders],
        "helpers": len(scan.helpers),
        "unavailable": scan.unavailable,
    }
    if args.json:
        return payload
    if scan.unavailable:
        # Silence here would read as "nothing is running", which is the one
        # conclusion this check must never imply without having actually run.
        printer.warn("could not read the process list, so no running Codex check was made.")
        print(dim("  Quit any running Codex before using the new account."))
    elif scan.holders:
        printer.warn(f"{len(scan.holders)} Codex process(es) still running: {scan.summary()}")
        print(
            dim(
                "  Each holds the previous token in memory and rewrites auth.json when\n"
                "  it refreshes, which would silently undo this swap. Quit them (and any\n"
                "  editor or desktop Codex integration) before using the new account."
            )
        )
    return payload


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def cmd_add(args: argparse.Namespace, sw: Switcher) -> int:
    if args.source:
        result = sw.add_from_file(
            Path(args.source).expanduser(),
            slot=args.slot,
            label=args.label or "",
            force=args.force,
        )
        origin = str(Path(args.source).expanduser())
    else:
        result = sw.add_live(slot=args.slot, label=args.label or "", force=args.force)
        origin = str(sw.auth_file)

    if args.json:
        _emit(
            args,
            {
                "added": _account_payload(result.account),
                "source": origin,
                "replaced": result.replaced,
            },
        )
        return 0
    verb = "Updated" if result.replaced else "Added"
    print(_stored_line(verb, result.account))
    return 0


def cmd_list(args: argparse.Namespace, sw: Switcher) -> int:
    state = sw.store.load()
    live_identity = sw.live_identity()
    live_account = sw.store.find_by_key(state, live_identity.key) if live_identity else None

    rows = []
    for slot in sorted(state.accounts):
        account = state.accounts[slot]
        blob = sw.store.read_blob(account)
        identity = try_parse_auth(blob)
        rows.append((account, identity, blob is not None))

    if args.json:
        _emit(
            args,
            {
                "store": str(sw.store.root),
                "activeSlot": state.active_slot,
                "liveSlot": live_account.slot if live_account else None,
                "liveEmail": live_identity.email if live_identity else None,
                "liveManaged": live_account is not None,
                "accounts": [dict(_account_payload(a, i), stored=stored) for a, i, stored in rows],
            },
        )
        return 0

    if not rows:
        print(dim("No managed accounts yet. Run `codex-swap add` or `codex-swap login`."))
        return 0

    print(bold("Managed Codex accounts") + dim(f"  ({printer.abbreviate(sw.store.root)})"))
    width = max(len(a.email) for a, _, _ in rows)
    for account, identity, stored in rows:
        marker = green(" * ") if live_account and account.slot == live_account.slot else "   "
        state_text = _token_state(identity) if stored else red("missing credentials")
        label = f"  {dim('(' + account.label + ')')}" if account.label else ""
        print(
            f"{marker}{bold(str(account.slot))}  {account.email:<{width}}  "
            f"{dim((account.plan or '?').ljust(6))}  {state_text}{label}"
        )

    print()
    if live_identity is None:
        print(dim(f"Live auth.json: none — no Codex login at {printer.abbreviate(sw.auth_file)}"))
    elif live_account is None:
        printer.warn(f"the live login ({live_identity.email}) is not managed.")
        print(dim("  Run `codex-swap add` to capture it before switching away."))
    return 0


def cmd_status(args: argparse.Namespace, sw: Switcher) -> int:
    state = sw.store.load()
    identity = sw.live_identity()
    account = sw.store.find_by_key(state, identity.key) if identity else None

    if args.json:
        _emit(
            args,
            {
                "codexHome": str(paths.codex_home()),
                "authFile": str(sw.auth_file),
                "authFileExists": sw.auth_file.exists(),
                "store": str(sw.store.root),
                "managed": len(state.accounts),
                "activeSlot": state.active_slot,
                "rotation": state.sequence,
                "live": (
                    {
                        "email": identity.email,
                        "plan": identity.plan,
                        "authMode": identity.auth_mode,
                        "accountId": identity.account_id,
                        "expired": identity.is_expired(),
                        "slot": account.slot if account else None,
                    }
                    if identity
                    else None
                ),
            },
        )
        return 0

    def row(key: str, value: str) -> None:
        print(f"{key:<14} {value}")

    row("CODEX_HOME", printer.abbreviate(paths.codex_home()))
    row("auth.json", printer.abbreviate(sw.auth_file) if sw.auth_file.exists() else dim("absent"))
    row("store", printer.abbreviate(sw.store.root))
    if identity is None:
        print(dim("\nNo readable Codex login."))
    else:
        print()
        row("account", bold(identity.email))
        row("plan", identity.plan or "?")
        row("auth mode", identity.auth_mode)
        row("account id", identity.account_id or "?")
        row("last refresh", printer.format_age(printer.parse_iso(identity.last_refresh)))
        row("slot", str(account.slot) if account else yellow("unmanaged"))
    print()
    row("managed", f"{len(state.accounts)} account(s)")
    if state.sequence:
        row("rotation", " -> ".join(str(s) for s in state.sequence))
    return 0


def _render_switch(args: argparse.Namespace, result) -> int:
    if args.json:
        payload = {
            "switched": _account_payload(result.target),
            "previousSlot": result.previous.slot if result.previous else None,
            "backedUp": result.backed_up,
            "discarded": result.discarded.email if result.discarded else None,
            "discardedUnreadable": result.discarded_unreadable,
            "alreadyActive": result.already_active,
        }
        payload["processes"] = _report_processes(args)
        _emit(args, payload)
        return 0
    target = result.target
    if result.discarded is not None:
        printer.warn(
            f"the previous login ({result.discarded.email}) was not managed and has "
            f"been replaced. It is gone unless you can log in again."
        )
    if result.discarded_unreadable:
        printer.warn(
            "the previous auth.json could not be parsed and has been replaced. "
            "If that was a real login, it is gone unless you can log in again."
        )
    if result.already_active:
        print(
            f"{cyan('Already on')} slot {bold(str(target.slot))}  "
            f"{bold(target.email)}  {dim('[' + (target.plan or '?') + ']')}"
        )
        print(dim("  The stored copy was refreshed from the live file; nothing else changed."))
        return 0
    print(
        f"{cyan('Switched to')} slot {bold(str(target.slot))}  "
        f"{bold(target.email)}  {dim('[' + (target.plan or '?') + ']')}"
    )
    print()
    _report_processes(args)
    print(dim(RESTART_HINT))
    return 0


def cmd_switch(args: argparse.Namespace, sw: Switcher) -> int:
    return _render_switch(args, sw.rotate())


def cmd_use(args: argparse.Namespace, sw: Switcher) -> int:
    return _render_switch(args, sw.switch_to(args.account))


def cmd_login(args: argparse.Namespace, sw: Switcher) -> int:
    if not args.json:
        print(cyan("Running `codex login` ..."))
    result = sw.login(slot=args.slot, label=args.label or "", force=args.force)
    if args.json:
        _emit(args, {"added": _account_payload(result.account), "replaced": result.replaced})
        return 0
    verb = "Updated" if result.replaced else "Stored"
    print(_stored_line(verb, result.account))
    return 0


def cmd_remove(args: argparse.Namespace, sw: Switcher) -> int:
    state = sw.store.load()
    account = sw.store.resolve(state, args.account)
    if not _confirm(args, f"Remove slot {account.slot} ({account.email})?"):
        if not args.json:
            print(dim("Cancelled"))
        return 0
    removed = sw.remove(args.account)
    if args.json:
        _emit(args, {"removed": _account_payload(removed)})
        return 0
    print(f"{green('Removed')} slot {removed.slot} ({removed.email})")
    return 0


def cmd_export(args: argparse.Namespace, sw: Switcher) -> int:
    bundle = sw.export_bundle()
    missing = bundle.pop("missing", [])
    payload = json.dumps(bundle, indent=2) + "\n"

    if args.path == "-":
        if args.json:
            # Every other command emits the result envelope; writing the raw
            # bundle here would break `--json export - | jq .exported`.
            _emit(
                args,
                {
                    "exported": len(bundle["accounts"]),
                    "path": "-",
                    "missing": missing,
                    "bundle": bundle,
                },
            )
            return 0
        sys.stdout.write(payload)
        # To stderr, so it cannot corrupt a bundle being piped onward, but not
        # silent: dropping accounts with no warning at all reads as success.
        for item in missing:
            printer.error(f"skipped {item}: no stored credentials")
        return 0
    target = Path(args.path).expanduser()
    write_secret(target, payload)
    if args.json:
        _emit(args, {"exported": len(bundle["accounts"]), "path": str(target), "missing": missing})
        return 0
    print(f"{green('Exported')} {len(bundle['accounts'])} account(s) to {target}")
    for item in missing:
        printer.warn(f"skipped {item}: no stored credentials")
    printer.warn("that file contains live OAuth tokens — keep it out of git and cloud sync.")
    return 0


def cmd_import(args: argparse.Namespace, sw: Switcher) -> int:
    raw = sys.stdin.read() if args.path == "-" else read_text(Path(args.path).expanduser())
    if raw is None:
        raise StoreError(f"no such file: {args.path}")
    try:
        bundle = json.loads(raw)
    except ValueError as exc:
        raise StoreError(f"not a readable export bundle: {exc}") from exc

    result = sw.import_bundle(bundle, force=args.force)
    if args.json:
        _emit(
            args,
            {
                "imported": [_account_payload(a) for a in result.added],
                "skipped": result.skipped,
            },
        )
        return 0
    for account in result.added:
        print(f"  {green('+')} {account.email} -> slot {account.slot}")
    for reason in result.skipped:
        print(f"  {dim('- ' + reason)}")
    print(f"{green('Imported')} {len(result.added)} account(s)")
    return 0


def cmd_purge(args: argparse.Namespace, sw: Switcher) -> int:
    root = sw.store.root
    if not root.exists():
        if not args.json:
            print(dim("Nothing to purge."))
        _emit(args, {"purged": False, "path": str(root)})
        return 0
    # Refuse before the scary warning and before prompting: being told "this
    # deletes every stored account" and *then* that it was not a store at all
    # reads as though something was destroyed.
    if not sw.store.looks_like_store():
        raise StoreError(
            f"{root} does not look like a codex-swap store (no sequence.json and "
            f"no accounts directory); refusing to delete it"
        )
    if not args.json:
        printer.warn(f"This deletes every stored account: {root}")
        print(dim(f"Your live {sw.auth_file} is left untouched."))
    if not _confirm(args, "Delete the store?"):
        if not args.json:
            print(dim("Cancelled"))
        return 0
    sw.purge()
    if args.json:
        _emit(args, {"purged": True, "path": str(root)})
        return 0
    print(f"{green('Purged')} {root}")
    return 0


def cmd_doctor(args: argparse.Namespace, sw: Switcher) -> int:
    """Report everything needed to diagnose a swap that did not stick."""
    state = sw.store.load()
    scan = process_detection.scan()

    problems: list[str] = []
    # doctor is the command people reach for when something is already broken,
    # so it reports an unreadable auth.json as a finding rather than dying on it.
    try:
        identity = sw.live_identity()
    except CodexSwapError as exc:
        identity = None
        problems.append(str(exc))

    if not sw.auth_file.exists():
        problems.append("no live Codex login (run `codex login`)")
    elif identity is None and not problems:
        problems.append(f"{sw.auth_file} exists but could not be parsed")
    if sw.auth_file.exists() and is_private(sw.auth_file) is False:
        problems.append(f"{sw.auth_file} is readable by other users")
    if state.accounts and is_private(sw.store.accounts_dir) is False:
        problems.append(f"{sw.store.accounts_dir} is readable by other users")
    for slot in sorted(state.accounts):
        account = state.accounts[slot]
        if sw.store.read_blob(account) is None:
            problems.append(f"slot {slot} ({account.email}) has no stored credentials")
    live_is_unmanaged = (
        bool(state.accounts)
        and identity is not None
        and sw.store.find_by_key(state, identity.key) is None
    )
    if live_is_unmanaged:
        problems.append(f"the live login ({identity.email}) is not managed")
    if scan.unavailable:
        problems.append("the process list could not be read, so no running-Codex check was made")
    elif scan.holders:
        problems.append(
            f"{len(scan.holders)} Codex process(es) running — a swap may be overwritten"
        )

    if args.json:
        _emit(
            args,
            {
                "platform": Platform.detect().name,
                "python": sys.version.split()[0],
                "version": __version__,
                "codexHome": str(paths.codex_home()),
                "configToml": str(paths.config_path()) if paths.config_path().exists() else None,
                "store": str(sw.store.root),
                "managed": len(state.accounts),
                "processes": {
                    "holders": len(scan.holders),
                    "helpers": len(scan.helpers),
                    "unavailable": scan.unavailable,
                },
                "problems": problems,
            },
        )
        return 1 if problems else 0

    print(bold("codex-swap doctor"))
    print(f"  version      {__version__}")
    print(f"  platform     {Platform.detect().name.lower()}  python {sys.version.split()[0]}")
    print(f"  CODEX_HOME   {printer.abbreviate(paths.codex_home())}")
    print(f"  config.toml  {'present' if paths.config_path().exists() else dim('absent')}")
    print(f"  store        {printer.abbreviate(sw.store.root)}")
    print(f"  managed      {len(state.accounts)} account(s)")
    print(f"  live login   {identity.email if identity else dim('none')}")
    print(
        f"  processes    {len(scan.holders)} codex, {len(scan.helpers)} helper(s)"
        + (dim("  (process list unavailable)") if scan.unavailable else "")
    )
    print()
    if not problems:
        print(green("No problems found."))
        return 0
    print(yellow(f"{len(problems)} problem(s):"))
    for item in problems:
        print(f"  - {item}")
    return 1


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------


EPILOG = """\
examples:
  codex-swap add                     capture the current ~/.codex/auth.json
  codex-swap login                   snapshot, run `codex login`, store the new account
  codex-swap list                    show every managed account
  codex-swap switch                  rotate to the next account
  codex-swap use 2                   switch to a specific slot
  codex-swap use me@example.com      ... or to an email
  codex-swap doctor                  diagnose a swap that did not stick
"""


def _add_global_flags(target: argparse.ArgumentParser, *, suppress: bool) -> None:
    """Declare the flags that work both before and after the sub-command.

    ``codex-swap status --json`` is what people actually type, so these are
    attached to every sub-parser as well as to the top level.

    The sub-parser copies use ``default=argparse.SUPPRESS`` so that an unused
    copy does not write ``False`` over a value the top-level parser already set
    — otherwise ``codex-swap --json status`` would silently print a human table,
    because a sub-parser parses into a fresh namespace which is then copied
    wholesale onto the real one.

    Note this builds *fresh action objects* for each parser rather than sharing
    one via ``parents=``. ``ArgumentParser.set_defaults`` mutates the ``default``
    attribute of matching actions in place, so a shared action would have its
    ``SUPPRESS`` quietly rewritten by whichever parser called ``set_defaults``
    first — which is exactly the bug this arrangement avoids.
    """
    default = argparse.SUPPRESS if suppress else None

    def opt(*names, **kwargs):
        kwargs["default"] = default if not suppress else argparse.SUPPRESS
        target.add_argument(*names, **kwargs)

    opt("--json", action="store_true", help="emit machine-readable JSON")
    opt("--no-color", action="store_true", help="disable ANSI colour")
    opt("--yes", "-y", action="store_true", help="assume yes for prompts")
    opt("--store", metavar="DIR", help="override the account store directory")


def _sub_flag_parent() -> argparse.ArgumentParser:
    parent = argparse.ArgumentParser(add_help=False)
    _add_global_flags(parent, suppress=True)
    return parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codex-swap",
        description="Multi-account switcher for the Codex CLI.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"codex-swap {__version__}")
    # The top level owns the real defaults; the sub-parsers only override.
    _add_global_flags(parser, suppress=False)
    parser.set_defaults(json=False, no_color=False, yes=False, store=None)

    common = _sub_flag_parent()
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    def add_parser(name: str, **kwargs) -> argparse.ArgumentParser:
        return sub.add_parser(name, parents=[common], **kwargs)

    p_add = add_parser("add", help="capture a Codex login into the store")
    p_add.add_argument(
        "--from", dest="source", metavar="PATH", help="read an auth.json instead of the live one"
    )
    p_add.add_argument("--slot", type=int, help="slot to store it in")
    p_add.add_argument("--label", help="free-form note shown in `list`")
    p_add.add_argument("--force", action="store_true", help="overwrite an existing account or slot")
    p_add.set_defaults(func=cmd_add)

    p_list = add_parser("list", aliases=["ls"], help="list managed accounts")
    p_list.set_defaults(func=cmd_list)

    p_status = add_parser("status", help="show the live Codex login")
    p_status.set_defaults(func=cmd_status)

    p_switch = add_parser("switch", aliases=["next"], help="rotate to the next account")
    p_switch.set_defaults(func=cmd_switch)

    p_use = add_parser("use", help="switch to a specific account")
    p_use.add_argument("account", metavar="SLOT|EMAIL")
    p_use.set_defaults(func=cmd_use)

    p_login = add_parser("login", help="run `codex login` and store the result")
    p_login.add_argument("--slot", type=int, help="slot to store it in")
    p_login.add_argument("--label", help="free-form note shown in `list`")
    p_login.add_argument(
        "--force", action="store_true", help="replace a different account already in --slot"
    )
    p_login.set_defaults(func=cmd_login)

    p_remove = add_parser("remove", aliases=["rm"], help="forget an account")
    p_remove.add_argument("account", metavar="SLOT|EMAIL")
    p_remove.set_defaults(func=cmd_remove)

    p_export = add_parser("export", help="write every account to a bundle")
    p_export.add_argument("path", metavar="PATH", help="destination, or '-' for stdout")
    p_export.set_defaults(func=cmd_export)

    p_import = add_parser("import", help="read accounts from a bundle")
    p_import.add_argument("path", metavar="PATH", help="source, or '-' for stdin")
    p_import.add_argument("--force", action="store_true", help="overwrite matching accounts")
    p_import.set_defaults(func=cmd_import)

    p_purge = add_parser("purge", help="delete the whole store")
    p_purge.set_defaults(func=cmd_purge)

    p_doctor = add_parser("doctor", help="diagnose the local setup")
    p_doctor.set_defaults(func=cmd_doctor)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    printer.set_color(False if (args.no_color or args.json) else None)

    try:
        if args.store is not None and not args.store.strip():
            # `--store ""` from an unset shell variable previously fell through
            # to the default root, so `purge --yes` could delete the real store.
            raise ValidationError("--store was given an empty path")
        # A prototype-era store is migrated once, before anything reads it.
        if args.store is None:
            paths.migrate_legacy_store()
        store = AccountStore(Path(args.store).expanduser() if args.store else None)
        return args.func(args, Switcher(store=store))
    except CodexSwapError as exc:
        if args.json:
            print(json.dumps({"error": str(exc), "type": type(exc).__name__}, indent=2))
        else:
            printer.error(str(exc))
        return exc.exit_code
    except OSError as exc:
        # ENOSPC/EROFS/EPERM on a store write would otherwise escape as a
        # traceback, which exceptions.py promises never happens.
        message = f"filesystem error: {exc}"
        if args.json:
            print(json.dumps({"error": message, "type": type(exc).__name__}, indent=2))
        else:
            printer.error(message)
        return 1
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        print()
        print(dim("Interrupted"))
        return 130
    except BrokenPipeError:  # pragma: no cover - `| head` and friends
        return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
