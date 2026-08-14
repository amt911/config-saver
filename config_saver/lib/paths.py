"""Where configurations live, and in which order they win.

Three levels, borrowed from how systemd and tmpfiles.d layer their drop-ins,
because that is the precedence people already expect:

    <prefix>/share/config-saver/configs   examples shipped with the package
    /etc/config-saver/configs             system policy (an installer owns it)
    ~/.config/config-saver/configs.d      the user's own configurations

The two lower ones are *merged*: a configuration is identified by its name
without extension, so ``~/.config/…/zsh.yaml`` replaces ``/etc/…/zsh.json``
instead of both running and racing for the same archive name.

The examples are never used on their own. A package that drops files in /etc
(or that gets picked up because nothing else exists) decides what a machine
backs up, which is not a package's decision to make — the shipped default
configuration reaches ``~/.ssh`` and ``~/.config/rclone``.
"""

from __future__ import annotations

import os
import sys

# System policy. An installer (dasik) writes JSON here; leave it to root.
SYSTEM_CONFIG_DIR = "/etc/config-saver/configs"


def user_config_dir() -> str:
    """The user's own configurations. Inside $HOME, so backups carry them."""
    return os.path.expanduser("~/.config/config-saver/configs.d")


def example_config_dir() -> str:
    """Examples shipped with the distribution (never active by themselves)."""
    return os.path.join(sys.prefix, "share", "config-saver", "configs")
