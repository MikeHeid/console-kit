#!/usr/bin/env python3
"""Command-line entry for `console_kit.server`: see that module for the two doors and the Access check."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

if __name__ == "__main__":
    if "--all" in sys.argv[1:]:   # K3: the one server for every project in server.json
        from console_kit.multiserver import main as multi_main  # noqa: E402
        sys.exit(multi_main())
    from console_kit.server import main  # noqa: E402  (the single-project server, unchanged)
    sys.exit(main())
