"""Entry point for ``python -m codex_account_switcher``."""

from __future__ import annotations

import sys

from codex_account_switcher.cli import main

if __name__ == "__main__":
    sys.exit(main())
