"""
Config Saver package initialization.

For versioning, the best practice is Semantic Versioning (SemVer):
Format: MAJOR.MINOR.PATCH

MAJOR: Breaking changes
MINOR: New features, backward compatible
PATCH: Bug fixes, backward compatible
"""
try:
    from importlib.metadata import version
    __version__ = version("config-saver")
except ImportError:
    __version__ = "unknown"
try:
    from importlib.metadata import version
    __version__ = version("config-saver")
except ImportError:
    __version__ = "unknown"