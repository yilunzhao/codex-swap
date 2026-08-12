"""Entry point for ``python -m codex_swap``."""

from __future__ import annotations

import sys

from codex_swap.cli import main

if __name__ == "__main__":
    sys.exit(main())
