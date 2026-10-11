"""Executable module entrypoint for python3 -m openrank."""
from __future__ import annotations

import sys
from openrank.cli import main

if __name__ == "__main__":
    sys.exit(main())
