#!/usr/bin/env python3
"""Filesystem orchestration for config-saver backups.

This module is deliberately free of terminal rendering: every operation returns
a structured result and the CLI decides what to print and which exit code to
use. That is also what makes parallel batch compression possible without
interleaved worker output.
"""

from __future__ import annotations

import glob
import os
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field

from config_saver.lib.errors import RootRequiredError
from config_saver.lib.parser.parser import Parser
from config_saver.lib.tar_compressor.tar_compressor import (
    ARCHIVE_MODE,
    CompressResult,
    TarCompressor,
)

# Backups contain secrets; their directories are private by construction.
PRIVATE_DIR_MODE = 0o700

# Config files accepted in directory (batch) mode. YAML parsing also accepts JSON.
CONFIG_GLOBS = ("*.yaml", "*.yml", "*.json")


@dataclass
class ConfigOutcome:
    """What happened to a single configuration during a batch run."""

    config_path: str
    archive_path: str | None = None
    result: CompressResult | None = None
    skipped_root_only: bool = False
    error: str | None = None

    @property
    def name(self) -> str:
        return os.path.splitext(os.path.basename(self.config_path))[0]


@dataclass
class BatchResult:
    """Aggregated outcome of compressing a directory of configurations."""

    outcomes: list[ConfigOutcome] = field(default_factory=list)

    @property
    def created(self) -> list[str]:
        return [o.archive_path for o in self.outcomes if o.archive_path]

    @property
    def skipped_root_only(self) -> list[str]:
        return [o.config_path for o in self.outcomes if o.skipped_root_only]

    @property
    def failures(self) -> list[ConfigOutcome]:
        return [o for o in self.outcomes if o.error]

    @property
    def missing_inputs(self) -> list[str]:
        missing: list[str] = []
        for outcome in self.outcomes:
            if outcome.result:
                missing.extend(outcome.result.missing_inputs)
        return missing


def _ensure_private_dir(path: str) -> None:
    """Create a directory (and any missing parent) with 0700 permissions.

    os.makedirs() only applies its mode to the leaf, which would leave the
    intermediate <saves>/configs/<name> directories world-readable.
    """
    path = os.path.abspath(path)
    missing: list[str] = []
    current = path
    while not os.path.isdir(current):
        missing.append(current)
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent

    for directory in reversed(missing):
        try:
            os.mkdir(directory, PRIVATE_DIR_MODE)
        except FileExistsError:
            pass
        _chmod_private(directory)

    if not missing:
        # Tighten a directory created by an older version with the plain umask.
        _chmod_private(path)


def _chmod_private(path: str) -> None:
    try:
        # The insecure-file-permissions rule cannot resolve the constant and suggests
        # 0o644 as a "good default"; PRIVATE_DIR_MODE is 0o700, which is the point (#15).
        # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
        os.chmod(path, PRIVATE_DIR_MODE)
    except (PermissionError, FileNotFoundError):
        # Directory owned by someone else: leave its permissions alone.
        pass


def _compress_job(job: tuple[str, str, str, str | None, bool]) -> ConfigOutcome:
    """Compress one configuration. Top-level so ProcessPoolExecutor can pickle it."""
    config_path, dest_dir, archive_name, description, show_progress = job
    outcome = ConfigOutcome(config_path=config_path)
    try:
        manager = BackupManager()
        outcome.archive_path, outcome.result = manager.compress_config_to_directory(
            config_path,
            dest_dir,
            archive_name,
            description=description,
            show_progress=show_progress,
        )
    except RootRequiredError:
        outcome.skipped_root_only = True
    except Exception as exc:  # reported per config; the batch keeps going
        outcome.error = f"{type(exc).__name__}: {exc}"
    return outcome


class BackupManager:
    """Encapsulates filesystem operations for the config-saver CLI.

    Responsibilities:
    - ensure the saves directory exists (with XDG fallback), privately
    - list existing archives (per-config tree plus legacy top-level archives)
    - compress a single configuration into a tar.gz
    - compress every configuration in a directory, optionally in parallel
    """

    def __init__(self, saves_dir: str | None = None):
        self.saves_dir = saves_dir or os.path.expanduser("~/.config/config-saver")

    def ensure_saves_dir(self) -> str:
        """Ensure the base saves dir exists, falling back to the XDG data dir.

        Returns the actual saves_dir that should be used.
        """
        try:
            _ensure_private_dir(self.saves_dir)
            return self.saves_dir
        except PermissionError:
            user_saves = os.path.expanduser("~/.local/share/config-saver/saves")
            _ensure_private_dir(user_saves)
            self.saves_dir = user_saves
            return self.saves_dir

    def list_archives(self) -> list[str]:
        """Return every known archive: the per-config tree *and* legacy top-level ones.

        Both locations are always included; a single per-config archive used to
        make every top-level archive disappear from --list and --export-*.
        """
        configs_root = os.path.join(self.saves_dir, "configs")
        files: set[str] = set()
        if os.path.isdir(configs_root):
            files.update(glob.glob(os.path.join(configs_root, "**", "*.tar.gz"), recursive=True))
        files.update(glob.glob(os.path.join(self.saves_dir, "*.tar.gz")))
        return sorted(files)

    def compress_config_file(self, config_path: str, out_path: str, show_progress: bool = False) -> CompressResult:
        """Compress a single configuration into the provided output path."""
        parser = Parser(config_path)
        model = parser.get_model()
        compressor = TarCompressor(model, out_path, show_progress=show_progress)
        result = compressor.compress()
        # A placeholder that matched nothing never becomes a path; report it as missing.
        result.missing_inputs.extend(parser.unresolved_paths)
        return result

    # Backwards-compatible alias (the tool advertises JSON support too).
    compress_yaml_file = compress_config_file

    def compress_config_to_directory(
        self,
        config_path: str,
        dest_dir: str,
        archive_name: str,
        description: str | None = None,
        show_progress: bool = False,
    ) -> tuple[str, CompressResult]:
        """Compress a config into dest_dir, optionally writing a description.txt.

        Returns the archive path and the structured compression result. Failures
        to write the description are surfaced, not swallowed.
        """
        _ensure_private_dir(dest_dir)

        out_path = os.path.join(dest_dir, archive_name)
        result = self.compress_config_file(config_path, out_path, show_progress=show_progress)

        if description:
            desc_path = os.path.join(dest_dir, "description.txt")
            fd = os.open(desc_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, ARCHIVE_MODE)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(description)

        return out_path, result

    def compress_config_to_timestamp_dir(
        self,
        config_path: str,
        base_cfg_dir: str,
        timestamp: str,
        description: str | None = None,
        show_progress: bool = False,
    ) -> tuple[str, CompressResult]:
        """Compress a configuration into <base_cfg_dir>/<timestamp>/."""
        ts_dir = os.path.join(base_cfg_dir, timestamp)
        _ensure_private_dir(ts_dir)
        cfg_basename = os.path.splitext(os.path.basename(config_path))[0]
        archive_name = f"{cfg_basename}-{timestamp}.tar.gz"
        return self.compress_config_to_directory(
            config_path,
            ts_dir,
            archive_name,
            description=description,
            show_progress=show_progress,
        )

    # Backwards-compatible alias.
    compress_yaml_to_timestamp_dir = compress_config_to_timestamp_dir

    def get_description_for_archive(self, archive_path: str) -> str | None:
        """Return the description stored next to an archive, if any."""
        if not archive_path:
            return None

        archive_dir = os.path.dirname(os.path.abspath(archive_path))
        desc_path = os.path.join(archive_dir, "description.txt")
        if os.path.isfile(desc_path):
            try:
                with open(desc_path, encoding="utf-8") as fh:
                    return fh.read().strip()
            except OSError:
                return None
        return None

    def find_config_files(self, input_dir: str) -> list[str]:
        """Return the top-level configuration files inside input_dir, sorted."""
        cfg_files: list[str] = []
        for pattern in CONFIG_GLOBS:
            cfg_files.extend(glob.glob(os.path.join(input_dir, pattern)))
        return sorted(cfg_files)

    def compress_directory_of_configs(
        self,
        input_dir: str,
        timestamp: str,
        show_progress: bool = False,
        description: str | None = None,
        jobs: int = 1,
    ) -> BatchResult:
        """Compress each configuration inside input_dir into its own archive.

        Produces <saves_dir>/configs/<cfgname>/<timestamp>/<cfgname>-<timestamp>.tar.gz.
        With jobs > 1 the configurations are compressed in separate processes
        (gzip is CPU-bound and each archive is independent); output order always
        follows the configuration filename order regardless of completion order.
        """
        cfg_files = self.find_config_files(input_dir)
        if not cfg_files:
            raise FileNotFoundError(f"No YAML/JSON configuration files found in {input_dir}.")

        self.ensure_saves_dir()

        jobs_spec: list[tuple[str, str, str, str | None, bool]] = []
        for cfg in cfg_files:
            cfg_basename = os.path.splitext(os.path.basename(cfg))[0]
            ts_dir = os.path.join(self.saves_dir, "configs", cfg_basename, timestamp)
            archive_name = f"{cfg_basename}-{timestamp}.tar.gz"
            jobs_spec.append((cfg, ts_dir, archive_name, description, show_progress and jobs == 1))

        if jobs > 1:
            with ProcessPoolExecutor(max_workers=jobs) as pool:
                outcomes = list(pool.map(_compress_job, jobs_spec))
        else:
            outcomes = [_compress_job(spec) for spec in jobs_spec]

        return BatchResult(outcomes=outcomes)

    # Backwards-compatible alias.
    compress_directory_of_yamls = compress_directory_of_configs
