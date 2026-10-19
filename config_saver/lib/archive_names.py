"""How archive files are named: ``<cfg>-YYYYMMDD-HHMMSS.tar.gz``, optionally ``.age``/``.gpg``."""

from __future__ import annotations

import os
import re

# Group 1 is the config name (hyphens allowed), group 2 the timestamp.
FILENAME_PATTERN = re.compile(r"(.+)-(\d{8}-\d{6})\.tar\.gz(?:\.age|\.gpg)?$")

# Archives written straight into the saves directory (no config name) are called "config-saver-<ts>…";
# everywhere they are shown, exported or migrated they are the configuration LEGACY_NAME.
LEGACY_NAME = "legacy"
LEGACY_STEM = "config-saver"
NO_TIMESTAMP = "00000000-000000"


def is_top_level(path: str, saves_dir: str) -> bool:
    return os.path.dirname(os.path.abspath(path)) == os.path.abspath(saves_dir)


def archive_identity(path: str, saves_dir: str) -> tuple[str, str]:
    """(config name, ``YYYYMMDD-HHMMSS``) of an archive; an odd name stands for itself, oldest possible."""
    name = os.path.basename(path)
    match = FILENAME_PATTERN.match(name)
    if not match:
        return os.path.splitext(name)[0], NO_TIMESTAMP
    cfg = match.group(1)
    if cfg == LEGACY_STEM and is_top_level(path, saves_dir):
        cfg = LEGACY_NAME
    return cfg, match.group(2)
