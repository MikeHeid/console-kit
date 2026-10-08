#!/usr/bin/env python3
"""Command-line entry for `overture.fold`: see that module for `export` and `fold`."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from overture.fold import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
