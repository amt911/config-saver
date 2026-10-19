"""How archive files are named: ``<cfg>-YYYYMMDD-HHMMSS.tar.gz``, optionally ``.age``/``.gpg``."""

from __future__ import annotations

import re

# Group 1 is the config name (hyphens allowed), group 2 the timestamp.
FILENAME_PATTERN = re.compile(r"(.+)-(\d{8}-\d{6})\.tar\.gz(?:\.age|\.gpg)?$")
