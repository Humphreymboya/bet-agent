#!/usr/bin/env python
"""Entry point: py main.py <command>"""

import sys

from betbot.cli import main

if __name__ == "__main__":
    sys.exit(main())
