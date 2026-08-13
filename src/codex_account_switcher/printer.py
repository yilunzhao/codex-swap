"""Terminal presentation helpers.

Colour is opt-out in three ways, checked in order: an explicit
``--no-color``/``--json`` from the CLI, the ``NO_COLOR`` convention
(https://no-color.org), and finally whether stdout is a TTY. That last check is
what keeps ``xswap list | grep`` free of escape sequences.
"""

from __future__ import annotations

import datetime as _dt
import os
import sys

_FORCED: bool | None = None


def set_color(enabled: bool | None) -> None:
    """Force colour on/off, or pass None to fall back to auto-detection."""
    global _FORCED
    _FORCED = enabled


def color_enabled() -> bool:
    if _FORCED is not None:
        return _FORCED
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    return sys.stdout.isatty()


def _wrap(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if color_enabled() else text


def bold(text: str) -> str:
    return _wrap("1", text)


def dim(text: str) -> str:
    return _wrap("2", text)


def red(text: str) -> str:
    return _wrap("31", text)


def green(text: str) -> str:
    return _wrap("32", text)


def yellow(text: str) -> str:
    return _wrap("33", text)


def cyan(text: str) -> str:
    return _wrap("36", text)


def warn(message: str) -> None:
    print(f"{yellow('Warning:')} {message}")


def error(message: str) -> None:
    print(f"{red('Error:')} {message}", file=sys.stderr)


def format_span(seconds: float) -> str:
    """Compact duration: 45s, 12m, 3h, 5d."""
    seconds = int(abs(seconds))
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    if seconds < 172800:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def format_age(when: _dt.datetime | None, *, now: _dt.datetime | None = None) -> str:
    """Render a timestamp relative to now: '3h ago' / 'in 20m'."""
    if when is None:
        return "unknown"
    if when.tzinfo is None:
        when = when.replace(tzinfo=_dt.timezone.utc)
    now = now or _dt.datetime.now(_dt.timezone.utc)
    delta = (now - when).total_seconds()
    if delta < 0:
        return f"in {format_span(delta)}"
    return f"{format_span(delta)} ago"


def parse_iso(value: str | None) -> _dt.datetime | None:
    """Parse an ISO-8601 timestamp, tolerating a trailing ``Z``."""
    if not value:
        return None
    try:
        parsed = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return parsed


def abbreviate(path, *, home=None) -> str:
    """Render a path with ``~`` for the home directory."""
    import pathlib

    text = str(path)
    root = str(home or pathlib.Path.home())
    if not root:
        return text
    if text == root:
        return "~"
    # The separator check is what stops /home/bobby being rendered as ~by under
    # a home of /home/bob. A root that already ends in a separator (HOME=/ in a
    # container) must not have a second one demanded.
    prefix = root if root.endswith(os.sep) else root + os.sep
    if text.startswith(prefix):
        return "~" + os.sep + text[len(prefix) :]
    return text
