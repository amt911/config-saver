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
from datetime import datetime

from config_saver.lib.archive_names import LEGACY_NAME
from config_saver.lib.errors import RootRequiredError, StoreError
from config_saver.lib.models.encryption_model import EncryptionModel
from config_saver.lib.models.model import Model
from config_saver.lib.models.retention_model import RetentionModel
from config_saver.lib.parser.parser import Parser
from config_saver.lib.restic_store import ResticStore, Snapshot
from config_saver.lib.tar_compressor.tar_compressor import (
    ARCHIVE_MODE,
    CompressResult,
    TarCompressor,
)
from config_saver.lib.tar_prune import ARCHIVE_GLOBS, TIMESTAMP_FORMAT, has_archives, prune_config_dir

# Backups contain secrets; their directories are private by construction.
PRIVATE_DIR_MODE = 0o700

# LEGACY_NAME (see archive_names) is reserved for migrated pre-store archives: never pruned automatically.

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
    # Set instead of archive_path when the backup went into the restic store.
    snapshot: Snapshot | None = None
    # What pruning removed for this config afterwards (paths in tar mode, snapshot ids in restic mode).
    pruned: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return os.path.splitext(os.path.basename(self.config_path))[0]


@dataclass
class PruneReport:
    """What automatic pruning did after a batch: removals per config, and errors."""

    removed: dict[str, list[str]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    deferred: list[str] = field(default_factory=list)  # restic mode: archives still await migration


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


def _own_stamp(outcome: ConfigOutcome) -> list[datetime]:
    """The timestamp of the directory this run just wrote, parsed from its name."""
    if not outcome.archive_path:
        return []
    name = os.path.basename(os.path.dirname(outcome.archive_path))
    try:
        return [datetime.strptime(name, TIMESTAMP_FORMAT)]
    except ValueError:
        return []


def _describe(exc: Exception) -> str:
    """A store error already reads as a sentence; anything else needs its type."""
    return str(exc) if isinstance(exc, StoreError) else f"{type(exc).__name__}: {exc}"


@dataclass(frozen=True)
class _CompressJob:
    """One configuration to compress. Plain fields only: it is pickled to pool workers."""

    config_path: str
    dest_dir: str
    archive_name: str
    description: str | None
    show_progress: bool
    encryption: EncryptionModel | None
    extra_directories: tuple[str, ...]
    store: ResticStore | None = None
    when: datetime | None = None


def _compress_job(job: _CompressJob) -> ConfigOutcome:
    """Compress one configuration. Top-level so ProcessPoolExecutor can pickle it."""
    outcome = ConfigOutcome(config_path=job.config_path)
    try:
        manager = BackupManager()
        if job.store is not None:
            assert job.when is not None
            outcome.snapshot, outcome.result = manager.compress_config_to_store(
                job.config_path,
                job.store,
                job.when,
                description=job.description,
                show_progress=job.show_progress,
                extra_directories=job.extra_directories,
            )
        else:
            outcome.archive_path, outcome.result = manager.compress_config_to_directory(
                job.config_path,
                job.dest_dir,
                job.archive_name,
                description=job.description,
                show_progress=job.show_progress,
                encryption=job.encryption,
                extra_directories=job.extra_directories,
            )
    except RootRequiredError:
        outcome.skipped_root_only = True
    except Exception as exc:  # reported per config; the batch keeps going
        outcome.error = _describe(exc)
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
        for pattern in ARCHIVE_GLOBS:
            if os.path.isdir(configs_root):
                files.update(glob.glob(os.path.join(configs_root, "**", pattern), recursive=True))
            files.update(glob.glob(os.path.join(self.saves_dir, pattern)))
        return sorted(files)

    def compress_config_file(
        self,
        config_path: str,
        out_path: str,
        show_progress: bool = False,
        encryption: EncryptionModel | None = None,
        extra_directories: tuple[str, ...] = (),
    ) -> CompressResult:
        """Compress a single configuration into the provided output path.

        `encryption` overrides whatever the configuration file declares, which is
        what --encrypt-to does. `extra_directories` are appended to every
        configuration, which is how --include-system-configs works.
        """
        parser, model = self._load_model(config_path, encryption, extra_directories)
        compressor = TarCompressor(model, out_path, show_progress=show_progress)
        result = compressor.compress()
        # A placeholder that matched nothing never becomes a path; report it as missing.
        result.missing_inputs.extend(parser.unresolved_paths)
        return result

    @staticmethod
    def _load_model(
        config_path: str,
        encryption: EncryptionModel | None,
        extra_directories: tuple[str, ...],
    ) -> tuple[Parser, Model]:
        parser = Parser(config_path)
        model = parser.get_model()
        if encryption is not None:
            model = model.model_copy(update={"encrypt": encryption})
        if extra_directories:
            model = model.model_copy(update={"directories": [*model.directories, *extra_directories]})
        return parser, model

    def compress_config_to_store(
        self,
        config_path: str,
        store: ResticStore,
        when: datetime,
        description: str | None = None,
        show_progress: bool = False,
        extra_directories: tuple[str, ...] = (),
    ) -> tuple[Snapshot, CompressResult]:
        """Stream a configuration's tar into the restic store, tagged with its name.

        ``encrypt:`` is ignored here: the store is always encrypted (see
        ``TarCompressor.compress_to_stream``).
        """
        parser, model = self._load_model(config_path, None, extra_directories)
        compressor = TarCompressor(model, "", show_progress=show_progress)
        name = os.path.splitext(os.path.basename(config_path))[0]
        snapshot, result, _added = store.backup(compressor.compress_to_stream, name, when, description)
        result.missing_inputs.extend(parser.unresolved_paths)
        return snapshot, result

    # Backwards-compatible alias (the tool advertises JSON support too).
    compress_yaml_file = compress_config_file

    def compress_config_to_directory(
        self,
        config_path: str,
        dest_dir: str,
        archive_name: str,
        description: str | None = None,
        show_progress: bool = False,
        encryption: EncryptionModel | None = None,
        extra_directories: tuple[str, ...] = (),
    ) -> tuple[str, CompressResult]:
        """Compress a config into dest_dir, optionally writing a description.txt.

        Returns the archive path and the structured compression result. Failures
        to write the description are surfaced, not swallowed.
        """
        _ensure_private_dir(dest_dir)

        out_path = os.path.join(dest_dir, archive_name)
        try:
            result = self.compress_config_file(
                config_path,
                out_path,
                show_progress=show_progress,
                encryption=encryption,
                extra_directories=extra_directories,
            )
        except BaseException:
            # A failed run must not leave an empty timestamp directory that looks like a backup.
            try:
                os.rmdir(dest_dir)
            except OSError:  # not empty (or gone): leave it
                pass
            raise
        # Encryption appends a suffix, so the real path comes from the result.
        out_path = result.output_path

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
        encryption: EncryptionModel | None = None,
        extra_directories: tuple[str, ...] = (),
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
            encryption=encryption,
            extra_directories=extra_directories,
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
        encryption: EncryptionModel | None = None,
        extra_directories: tuple[str, ...] = (),
        store: ResticStore | None = None,
    ) -> BatchResult:
        """Compress each configuration inside input_dir into its own archive.

        Produces <saves_dir>/configs/<cfgname>/<timestamp>/<cfgname>-<timestamp>.tar.gz.
        With jobs > 1 the configurations are compressed in separate processes
        (gzip is CPU-bound and each archive is independent); output order always
        follows the configuration filename order regardless of completion order.
        The worker count is capped at the number of configurations, so a single
        configuration never pays for a pool. See docs/BENCHMARKS.md.
        """
        cfg_files = self.find_config_files(input_dir)
        if not cfg_files:
            raise FileNotFoundError(f"No YAML/JSON configuration files found in {input_dir}.")
        return self.compress_config_files(
            cfg_files,
            timestamp,
            show_progress=show_progress,
            description=description,
            jobs=jobs,
            encryption=encryption,
            extra_directories=extra_directories,
            store=store,
        )

    def compress_config_files(
        self,
        cfg_files: list[str],
        timestamp: str,
        show_progress: bool = False,
        description: str | None = None,
        jobs: int = 1,
        encryption: EncryptionModel | None = None,
        extra_directories: tuple[str, ...] = (),
        store: ResticStore | None = None,
    ) -> BatchResult:
        """Compress an explicit list of configurations, one archive each.

        Directory mode is one caller; another is a command line naming several
        configuration directories (a system one plus your own), which is why the
        list is passed in rather than discovered here.

        With a ``store`` each configuration becomes a snapshot in the restic
        repository (stamped with ``timestamp``, the same clock tar mode uses for
        its directory names) instead of a .tar.gz.
        """
        # More workers than configurations buys nothing, and a process pool for a
        # single configuration is measurably slower than doing it here.
        jobs = max(1, min(jobs, len(cfg_files)))

        self.ensure_saves_dir()

        when = datetime.strptime(timestamp, TIMESTAMP_FORMAT) if store is not None else None
        jobs_spec: list[_CompressJob] = []
        for cfg in cfg_files:
            cfg_basename = os.path.splitext(os.path.basename(cfg))[0]
            ts_dir = os.path.join(self.saves_dir, "configs", cfg_basename, timestamp)
            archive_name = f"{cfg_basename}-{timestamp}.tar.gz"
            jobs_spec.append(
                _CompressJob(
                    cfg,
                    ts_dir,
                    archive_name,
                    description,
                    show_progress and jobs == 1,
                    encryption,
                    extra_directories,
                    store=store,
                    when=when,
                )
            )

        if jobs > 1:
            # The interpreter default start method is deliberate: forcing spawn
            # costs an interpreter startup per worker and measured *slower* than
            # sequential on many small configurations (docs/BENCHMARKS.md). The
            # environment is fixed for the lifetime of a CLI run, so the
            # forkserver's frozen environment (3.14+) never matters in practice —
            # only to tests that change $HOME mid-process, which run with jobs=1.
            with ProcessPoolExecutor(max_workers=jobs) as pool:
                outcomes = list(pool.map(_compress_job, jobs_spec))
        else:
            outcomes = [_compress_job(spec) for spec in jobs_spec]

        return BatchResult(outcomes=outcomes)

    # Backwards-compatible alias.
    compress_directory_of_yamls = compress_directory_of_configs

    def prune_after_batch(self, batch: BatchResult, store: ResticStore | None) -> PruneReport:
        """Apply retention to the configs this batch just produced.

        Only configs whose job succeeded are touched, and a failure here never
        changes the outcome of a backup: it lands in ``PruneReport.errors``.
        Restic mode: ``forget`` per config with its ``retention:`` (or the
        defaults), then one ``prune`` if anything was forgotten. Tar mode: only
        configs that declare ``retention:`` explicitly; without a block nothing
        is deleted, so an upgrade never removes history nobody asked to prune.
        """
        report = PruneReport()
        for outcome in batch.outcomes:
            if outcome.error or outcome.skipped_root_only or outcome.result is None:
                continue
            if outcome.name == LEGACY_NAME:
                continue  # reserved for migrated top-level archives; only a manual prune names it
            if store is not None and has_archives(os.path.join(self.saves_dir, "configs", outcome.name)):
                # Migration is unfinished: pruning now would forget snapshots that
                # `--migrate` would only copy in again.
                report.deferred.append(outcome.name)
                continue
            try:
                retention = Parser(outcome.config_path).get_model().retention
                if store is not None:
                    own = [outcome.snapshot.time] if outcome.snapshot else []
                    forgotten = store.forget(outcome.name, retention or RetentionModel(), False, also_keep=own)
                    removed = [s.short_id for s in forgotten]
                elif retention is not None:
                    cfg_dir = os.path.join(self.saves_dir, "configs", outcome.name)
                    removed = prune_config_dir(cfg_dir, retention, dry_run=False, also_keep=_own_stamp(outcome))
                else:
                    continue
            except Exception as exc:  # one config's failure must not stop the others
                report.errors.append(f"[{outcome.name}] could not prune: {_describe(exc)}")
                continue
            if removed:
                outcome.pruned = removed
                report.removed[outcome.name] = removed
        if store is not None and report.removed:
            try:
                store.prune()
            except Exception as exc:
                report.errors.append(f"could not reclaim space (restic prune): {_describe(exc)}")
        return report
