#!/usr/bin/env python3

"""Entry point: delegate to the CLI implementation in the lib package."""

import sys

from config_saver.lib.cli.cli import CLI


def main() -> int:
    """Run the CLI and exit with its status code."""
    code = CLI().run()
    sys.exit(code)


if __name__ == "__main__":
    main()
