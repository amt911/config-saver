# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [3.3.1] - 2026-08-14

### Added

- `configs/own-configs.yaml`, a shipped example that backs up
  `~/.config/config-saver/configs.d`. Personal configurations live inside `$HOME`, so they travel
  inside an archive — but only if some configuration actually archives that directory. Copying this
  example makes every run produce a self-sufficient archive: restore it on a clean machine and the
  configurations that say what to back up come back too. Nothing is added to archives implicitly.

## [3.3.0] - 2026-08-14

### Changed

- **Configurations are resolved from three layered levels** instead of "the first directory that
  exists wins": `<prefix>/share/config-saver/configs` (examples shipped with the package),
  `/etc/config-saver/configs` (system policy) and `~/.config/config-saver/configs.d` (yours). The
  two lower levels are **merged by configuration name** (extension ignored), with the more specific
  one winning, as systemd layers its drop-ins. Previously `/etc` short-circuited the lookup, which
  made `~/.config/config-saver/configs.d` unreachable by construction.
- **The shipped examples are never used on their own.** With no configuration at either active
  level the run now stops with exit `6` and prints the copy command, instead of backing up whatever
  the examples happen to list. Falling back to them meant a fresh install quietly archiving
  `~/.ssh` and `~/.config/rclone` on a daily timer because a package was installed; a backup nobody
  chose is a surprise with security weight, and one `cp` is not a burden. They remain available
  explicitly: `--input /usr/share/config-saver/configs`.

  *Migration note.* Up to 3.2.0 the AUR package installed those examples into
  `/etc/config-saver/configs`, so machines that relied on them will report "No configurations
  found" after upgrading until a configuration exists at one of the two active levels. Because the
  package never declared a `backup=()` array, pacman would delete an example the administrator had
  **edited in place** without leaving the usual `.pacsave`; the 3.3.0 package therefore ships a
  `pre_upgrade` scriptlet that makes that `.pacsave` copy itself before pacman removes anything.
  Unmodified examples are simply removed, which is the intended outcome.

### Added

- **`--include-system-configs` / `--restore-system-configs`.** `/etc/config-saver/configs` is
  neither archived nor restored by default. That directory belongs to whatever manages the system —
  on a machine installed with `dasik` it is generated from the installer's own JSON — and a restore
  that overwrote it would leave the machine differing from its declared configuration, so every
  subsequent `plan` would show changes: precisely what an idempotent installer must not do. Both
  directions are therefore opt-in and explicit, and skipped members are reported rather than
  dropped silently. Personal configurations need none of this: they live under `$HOME`, so they
  already travel inside any archive that backs up the home directory and come back with it.

- `--input` is repeatable in directory mode, so a private repository of personal configurations can
  be combined with the system directory in one run:
  `config-saver --compress -i /etc/config-saver/configs -i ~/repos/private-configs/config-saver`.
  A configuration name defined in two directories is rejected instead of silently overwritten.

### Fixed

- The systemd timers caught up a missed backup only after `RandomizedDelaySec=10m` and the default
  one-minute accuracy; both are now zero, so a backup the machine slept through runs as soon as the
  timer starts.

## [3.2.0] - 2026-08-11

### Security

- **Extraction is containment-checked** (#14). Archive members that use `..`, absolute names, links
  pointing outside the extraction root, or device/fifo entries are refused before anything is
  written, in both `--output` and restore-in-place modes. `setuid`/`setgid` bits are never restored
  and files are opened with `O_NOFOLLOW`.
- **Backups are private by construction** (#15). Archives and `description.txt` are created with
  mode `0600`, every directory config-saver creates with `0700`, independently of the umask. The
  systemd units set `UMask=0077`.
- The shipped `default-config.yaml` no longer backs up the SSH **private** key, and documents that
  `.tar.gz` is compression, not encryption.

### Added

- **Optional archive encryption** with `age` or `gpg`, per configuration (`encrypt:` block) or from
  the command line (`--encrypt-to`, `--encrypt-method`), restored with `--decompress --identity`.
  The plaintext archive never survives the run, encrypted archives keep mode `0600`, and failures
  exit with the new code `9`. This is what makes a backup of `~/.ssh` or `~/.config/rclone` safe to
  copy anywhere.

- `tests/` — pytest suite covering round-trip integrity, one regression per extraction-attack
  vector, parser/models, path expander, backup manager, CLI exit codes, systemd units and packaging
  metadata (#20). CI gate: 90% coverage.
- `--jobs N` / `--jobs auto`: compress independent configurations in parallel processes, with output
  ordered by config filename regardless of completion order (#13). **Parallel is the default**
  (`auto`), capped at the number of configurations; measured at 2–3.3× in `docs/BENCHMARKS.md`.
- `--strict`: exit with code `8` when a configured path was missing from the backup.
- Documented, stable exit codes (`0/2/3/4/5/6/7/8/10`) in the README.
- Archive metadata member `.config-saver-metadata.json` recording whether `normalize_content` was
  applied, so restores no longer rewrite files that merely contain the placeholder string.
- Empty directories are archived and restored.
- `LICENSE` (MIT), `CHANGELOG.md`, `docs/FINDINGS.md`, `docs/RELEASING.md`.
- Property-based tests (Hypothesis) for the round-trip, path-normalization and expander
  invariants, plus a mutation-testing setup (mutmut) scoped to the pure logic and documented in
  `docs/TESTING.md`.
- Dev tooling: `ruff` (lint + format), `pre-commit` hooks (pre-commit + pre-push), CI matrix on
  Python 3.10–3.13, wheel/sdist install smoke tests, `systemd-analyze verify`, and a tag-vs-version
  consistency check (#21).

### Fixed

- Latin-1 files were normalized on compression but never denormalized on restore: the
  decompressor's text detection rejected everything that was not UTF-8, so the placeholder was
  left in the restored file. Found by a test written against the compressor/decompressor asymmetry.
- `--list` showed file mtimes instead of backup timestamps: `_parse_ts()` parsed the config name
  (`group(1)`) instead of the timestamp (`group(2)`) (#16).
- Home-prefix detection used `startswith()`, so `/home/andres2/x` was normalized as if it lived in
  `/home/andres`; the `archived_path[10:]` magic slice is gone too (#17).
- The user systemd unit was a copy of the templated system unit (`User=` in a user unit, unexpanded
  `%i`, `multi-user.target`), and both timers used `OnActiveSec=3h` while claiming to run daily.
  They now use `OnCalendar=*-*-* 03:00:00` with `Persistent=true` (#18).
- Packaging: version drift `3.0.3` vs tag `3.1.1`, missing `LICENSE`, `requires-python` `>=3.7`
  (the code needs 3.10), unpinned pydantic, `setuptools_scm` fighting the static version, empty
  `MANIFEST.in`, example configs missing from the wheel, `.venv` not gitignored (#19).
- `only_root_user` skipping keyed off the *text* of the exception message; there is now a typed
  `RootRequiredError` (#22).
- Missing configured paths were silently dropped from backups; they are now collected, reported and
  can fail the run with `--strict` (#13).
- Archives are written to a temporary file and `os.replace()`d into place, so an interrupted run
  cannot leave a truncated file that looks like a valid backup (#13).
- Decompression failures no longer print and return success: they raise typed errors mapped to exit
  codes (#13).
- Directory (batch) mode now also picks up `*.json` configurations, as the README advertises (#13).
- `list_archives()` combined per-config and legacy top-level archives; a single per-config archive
  used to hide every top-level one from `--list`, `--show-configs` and the exports (#13).
- `${BEGINS_WITH=…}` / `${ENDS_WITH=…}` resolution is deterministic (sorted), reports ambiguity, and
  a placeholder that matches nothing is reported as a missing input instead of vanishing (#13).
- Overlapping/duplicated configured paths are deduplicated instead of being archived twice (#13).
- `--output` combined with `--description` is now rejected instead of silently ignored (#13).

### Changed

- Library modules return structured results (`CompressResult`, `BatchResult`, `DecompressResult`)
  and no longer print; all rendering lives in the CLI layer (#23).
- Config models use `extra="forbid"`: a typoed key fails instead of being ignored (#13).
- `config_saver.lib.backup_mapager` was renamed to `config_saver.lib.backup_manager` (#23).
- The compression loop is written once (the progress/headless duplication is gone), the file list is
  streamed when no progress bar needs a total, and `BackupTable` scans the archive tree once instead
  of once per configuration (#23).
- The coverage gate is 90% and `pip-audit` is a blocking CI check rather than advisory.
- Content normalization is a streaming, byte-level pass: peak memory for a 64 MB file drops from
  ~400 MB to ~9 MB and the pass is 40% faster, at the cost of ~4 µs per small file. Files with
  nothing to replace are no longer copied at all.
- `pip install`ed environments fall back to the example configs shipped under
  `<prefix>/share/config-saver/configs` and print an actionable error when no config directory
  exists at all (#19).
