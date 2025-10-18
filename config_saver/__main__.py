#!/usr/bin/env python3

"""Entry point: delegate to the CLI implementation in the lib package."""
from .lib.cli import CLI


def main():
    CLI().run()


if __name__ == "__main__":
    main()
