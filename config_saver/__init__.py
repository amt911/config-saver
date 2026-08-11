"""
Config Saver package initialization.

For versioning, the best practice is Semantic Versioning (SemVer):
Format: MAJOR.MINOR.PATCH

MAJOR: Breaking changes
MINOR: New features, backward compatible
PATCH: Bug fixes, backward compatible
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("config-saver")
except PackageNotFoundError:  # running from a source tree without an installed dist
    __version__ = "unknown"

__all__ = ["__version__"]
