# Restic dedup storage + retention prune — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Store each config snapshot in one encrypted, deduplicated restic repository when restic is set up (tar.gz fallback otherwise), migrate existing archives into it, and prune snapshots with a snapper-like retention policy.

**Architecture:** `TarCompressor` gains an "uncompressed tar to a stream" mode; in restic mode that stream is restic's stdin (`restic backup --stdin`), so normalisation/exclude/metadata are reused unchanged. A `ResticStore` wraps every restic call (password via `RESTIC_PASSWORD_COMMAND` = age-decrypt of a password file). A pure `keep_set()` decides retention for both backends.

**Tech Stack:** Python ≥3.10, pydantic, subprocess → `restic` (≥0.16) and `age` binaries, pytest + Hypothesis, mutmut.

**Spec:** `docs/superpowers/specs/2026-10-19-restic-dedup-prune-design.md` (read it; it carries the why and the anti-goals).

## Global Constraints

- No new Python dependencies. `restic`/`age` are external binaries found with `shutil.which`.
- Never put the restic password on argv or in a plaintext file. Never delete an old archive before its snapshot is verified.
- The newest snapshot of a config is never pruned.
- Store configured but unusable ⇒ error (per config, `EXIT_RUNTIME`/`EXIT_ENCRYPTION`), never a silent fallback.
- Automatic prune: restic mode always (defaults 7/4/12/2 when no `retention:`); tar mode only for configs with an explicit `retention:` block. Only in directory/batch `--compress`.
- `--encrypt-to`/`--encrypt-method` with `--compress` in restic mode ⇒ `EXIT_USAGE` (6).
- Legacy top-level archives migrate as `cfg:legacy`; orphaned and legacy configs are never auto-pruned.
- Store files: repo `<saves_dir>/restic/` (0700), password `<saves_dir>/restic-password.age` (0600), settings `<saves_dir>/restic-store.json` (0600, records the identity path used at init).
- Unit tests never need restic installed (mock `subprocess`); one integration test marked `restic` skips when the binary is absent.
- README (user contract) and CHANGELOG updated in the task that changes behaviour. Gates: pytest+cov ≥80%, ruff, ruff format, mypy, mutation gate (add `retention.keep_set*` to its scope).
- Mutation runs only inside the memory cgroup (`docs/TESTING.md` §3).

## Review Focus

1. **Timer fires mid-migration** (both hold the restic lock) — restic's lock error must surface as a per-config error, and re-running `--migrate` must skip what is already in. Test in Task 6.
2. **A config name containing `-` or a timestamp-like suffix** (`own-configs`, `claude-conversations`) — tags and legacy parsing must not split it. Test in Tasks 3 and 6.
3. **Identity file missing/unreadable at timer time** — restic's password command fails; must be a clear "cannot decrypt the store password with <identity>" error, not a restic stack. Test in Task 3.
4. **Two snapshots in the same bucket with equal timestamps / clock skew (future timestamps)** — `keep_set` must still keep the newest and be deterministic. Property test in Task 1.
5. **Export of a config whose YAML no longer exists** — plain `.tar.gz` plus warning, not a crash. Test in Task 5.

---

### Task 1: Retention model and pure keep-set

**Files:**
- Create: `config_saver/lib/models/retention_model.py`, `config_saver/lib/retention.py`
- Modify: `config_saver/lib/models/model.py` (add field), `scripts/mutation-gate.sh` + `docs/TESTING.md` (scope)
- Test: `tests/test_retention.py`

**Interfaces:**
- Produces: `RetentionModel(BaseModel)` — `extra="forbid"`, fields `daily=7, weekly=4, monthly=12, yearly=2`, each `int`, `ge=0`. `Model.retention: RetentionModel | None = None`.
- Produces: `keep_set(timestamps: Iterable[datetime], policy: RetentionModel) -> set[datetime]`.

Semantics (restic's `--keep-*`, must match so both backends agree): sort descending; for each bucket kind with count N>0, walk newest→oldest, keep a timestamp when its bucket key (`date()`, ISO `(year, week)`, `(year, month)`, `year`) differs from the last kept key of that kind, until N keys are kept. Union all kinds, plus the single newest. Empty input → empty set.

- [ ] **Step 1: Failing tests** — `test_defaults_are_7_4_12_2`, `test_negative_rejected`, `test_unknown_key_rejected`, `test_model_accepts_retention_block`; `test_keep_set_empty`, `test_newest_always_kept_even_all_zero` (policy 0/0/0/0 over 5 stamps → `{newest}`), `test_daily_keeps_last_per_day` (3 stamps on one day + 2 on the previous, daily=2 → the newest of each day), `test_60_daily_runs_default_policy_keeps_expected` (60 consecutive days at 03:00 ending 2026-10-19 → exactly the 7 newest days ∪ last-per-ISO-week for 4 weeks ∪ last-per-month for 12 months ∪ last-per-year for 2 years; assert the set explicitly), Hypothesis `test_keep_set_subset_contains_newest_deterministic` (any list incl. duplicates and future dates: result ⊆ input, newest ∈ result, same result on shuffled input).
- [ ] **Step 2:** `pytest tests/test_retention.py -v` → FAIL (import error).
- [ ] **Step 3:** implement the model, the field and `keep_set`.
- [ ] **Step 4:** tests PASS; add `'config_saver.lib.retention.*'` to `scripts/mutation-gate.sh` and the TESTING.md command; run the gate in the cgroup; kill survivors with tests.
- [ ] **Step 5:** commit `feat(retention): per-config retention policy and keep-set`.

### Task 2: Uncompressed tar to a stream

**Files:**
- Modify: `config_saver/lib/tar_compressor/tar_compressor.py`
- Test: `tests/test_compressor.py`

**Interfaces:**
- Produces: `TarCompressor.compress_to_stream(self, fileobj: IO[bytes]) -> CompressResult` — writes an **uncompressed** tar (`tarfile.open(fileobj=..., mode="w|")`) with the metadata member first, same members/normalisation/exclude/root-owned skipping as `compress()`; never encrypts; `result.output_path` is set by the caller (pass `""`).
- Refactor: the member loop of `compress()` moves to `_write_members(self, tar, items, result, emit) -> None`, used by both. `compress()` behaviour unchanged.

- [ ] **Step 1: Failing tests** — `test_compress_to_stream_matches_gz_members` (same `sample_tree`: member names/sizes/modes of the stream tar == those of the `.tar.gz` from `compress()`), `test_compress_to_stream_is_uncompressed` (first bytes are not `\x1f\x8b`), `test_stream_ignores_encrypt_block` (model with `encrypt:` → no `age` call, mock `crypto.encrypt_file` asserting not called).
- [ ] **Step 2:** FAIL. **Step 3:** implement. **Step 4:** `pytest tests/test_compressor.py tests/test_roundtrip.py -q` PASS.
- [ ] **Step 5:** commit `refactor(compressor): stream an uncompressed tar`.

### Task 3: ResticStore and mode selection

**Files:**
- Create: `config_saver/lib/restic_store.py`
- Modify: `config_saver/lib/errors.py` (add `StoreError(RuntimeError, ConfigSaverError)`)
- Test: `tests/test_restic_store.py` (mocked), `tests/test_restic_integration.py` (marker `restic`, register it in `pyproject.toml`; `pytest.mark.skipif(shutil.which("restic") is None)` and also needs `age`/`age-keygen`)

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) Snapshot: id: str; short_id: str; time: datetime; cfg: str; description: str | None`
  - `class StorageMode(Enum): TAR_NO_RESTIC, TAR_NOT_INITIALISED, RESTIC`
  - `select_mode(saves_dir: str) -> StorageMode` — RESTIC iff repo dir + password file exist; if they exist but `restic` or `age` is not on PATH → raise `StoreError` naming the missing binary. Else TAR_NOT_INITIALISED if `restic` on PATH, else TAR_NO_RESTIC.
  - `NOTICES: dict[StorageMode, str]` — one line each: install restic / run `config-saver --restic-init --encrypt-to <age key>`.
  - `class ResticStore(saves_dir: str, identity: str | None = None)` — identity defaults to the one recorded in `restic-store.json`, else `~/.config/age/key.txt`.
    - `init(recipient: str) -> None` — refuses if repo or password file exists (`StoreError`); password = `secrets.token_urlsafe(32)` piped into `age -r <recipient> -o <pwfile>` (stdin, never argv); `restic init`; writes `restic-store.json` with the identity path. Modes per Global Constraints.
    - `backup(write: Callable[[IO[bytes]], CompressResult], cfg: str, when: datetime, description: str | None) -> tuple[Snapshot, CompressResult]` — runs `restic backup --stdin --stdin-filename <cfg>.tar --tag config-saver --tag cfg:<cfg> [--tag desc:<b64url>] --time "<YYYY-MM-DD HH:MM:SS>" --json` via `Popen`, calls `write(proc.stdin)`, closes stdin, parses the `summary` line for `snapshot_id`. Non-zero exit → `StoreError` with restic's stderr.
    - `snapshots(cfg: str | None = None) -> list[Snapshot]` — `restic snapshots --json --tag config-saver[,cfg:<cfg>]`, sorted by time.
    - `dump(snapshot_id: str, cfg: str, out: IO[bytes]) -> None` — `restic dump <id> /<cfg>.tar` to `out`.
    - `forget(cfg: str, policy: RetentionModel, dry_run: bool) -> list[Snapshot]` — computes the removal set with `retention.keep_set` (single source of truth), then `restic forget <ids…>` (skipped on dry_run). Returns the removed ones.
    - `prune() -> None` — `restic prune`.
    - Every call runs with env `RESTIC_REPOSITORY=<repo>` and `RESTIC_PASSWORD_COMMAND="age -d -i <shlex.quote(identity)> <shlex.quote(pwfile)>"`, env copied from `os.environ` without `RESTIC_PASSWORD*`. A failure whose stderr mentions the password command → `StoreError(f"cannot decrypt the store password with {identity}")`.
  - Description tag: `desc:` + base64url without padding of the UTF-8 text; decoded back in `snapshots()`.

Representation decision is backed by a measurement (2026-10-19, real `claude-conversations`, restic 0.18.1, `--stdin` uncompressed tar): see the result recorded in the spec's "Measurement" section, added in this task's commit.

- [ ] **Step 1: Failing mocked tests** — `test_select_mode_*` (3 modes + store-exists-binary-missing raises), `test_init_refuses_existing`, `test_init_password_never_on_argv` (inspect every recorded argv), `test_backup_argv_and_env` (tags `cfg:own-configs` intact, `--time` format, env has `RESTIC_PASSWORD_COMMAND`, no `RESTIC_PASSWORD`), `test_backup_nonzero_raises_store_error`, `test_identity_failure_message` (stderr contains "password command" → message names the identity path), `test_description_roundtrip_unicode_and_commas`, `test_forget_uses_keep_set_and_never_newest`, `test_forget_dry_run_runs_nothing`.
- [ ] **Step 2:** FAIL. **Step 3:** implement. **Step 4:** PASS.
- [ ] **Step 5: Integration test** `test_real_restic_roundtrip` — tmp saves_dir, `age-keygen` identity, `init`, two `backup`s of the same `sample_tree` via `compress_to_stream`, assert second snapshot's `data_added` < 10% of first (read from `--json` summary; expose it as `Snapshot`-adjacent return or log), `dump` the latest and extract with `TarDecompressor` into tmp → files equal; `forget` with all-zero policy leaves exactly the newest. Run it locally with the scratch restic on PATH; it must pass, and must skip cleanly without it.
- [ ] **Step 6:** commit `feat(store): restic-backed snapshot store`.

### Task 4: Compress through the store + automatic prune

**Files:**
- Modify: `config_saver/lib/backup_manager/backup_manager.py`, `config_saver/lib/cli/cli.py`
- Create: `config_saver/lib/tar_prune.py`
- Test: `tests/test_backup_manager.py`, `tests/test_cli.py`, `tests/test_tar_prune.py`

**Interfaces:**
- Consumes: Tasks 1–3.
- Produces:
  - `tar_prune.prune_config_dir(cfg_dir: str, policy: RetentionModel, dry_run: bool) -> list[str]` — candidates are subdirectories named `YYYYMMDD-HHMMSS`; removes (rmtree) those not in `keep_set`; returns removed paths. Anything else in the dir is untouched.
  - `ConfigOutcome` gains `snapshot: Snapshot | None` and `pruned: list[str]`.
  - `BackupManager.compress_config_files(..., store: ResticStore | None = None)` — when a store is given, each job does `store.backup(compressor.compress_to_stream, cfg, now, description)` instead of writing a `.tar.gz`. The pool path stays (restic serialises on its own lock; if measured contention makes `--jobs` slower, cap jobs to 1 in restic mode and say so in BENCHMARKS.md).
  - `BackupManager.prune_after_batch(batch: BatchResult, store: ResticStore | None) -> PruneReport` — restic: `forget` per produced config with `model.retention or RetentionModel()`, then one `prune()` if anything was forgotten. Tar: only configs whose model has an explicit `retention`. `@dataclass PruneReport: removed: dict[str, list[str]]; errors: list[str]`.
- CLI: `_compress` calls `select_mode`; prints the notice once on stderr for the two TAR modes; rejects `--encrypt-to`/`--encrypt-method` in RESTIC mode with `EXIT_USAGE`; after a directory batch runs `prune_after_batch`; a prune error prints and yields `EXIT_RUNTIME` but the batch report is printed first. Single-file without `--output` in RESTIC mode → store with the config stem; with `--output` → today's `.tar.gz`. No prune for single-file.

- [ ] **Step 1: Failing tests** — `test_batch_uses_store_when_given` (fake store recording calls), `test_tar_mode_without_retention_prunes_nothing` (55 timestamp dirs, no block → none removed), `test_tar_mode_with_retention_prunes` (explicit block → removed set equals complement of `keep_set`), `test_restic_mode_prunes_with_defaults`, `test_prune_error_after_successful_batch_exit_runtime_report_printed`, `test_notice_printed_once_tar_no_restic`, `test_encrypt_to_rejected_in_restic_mode`, `test_single_file_output_still_writes_targz_in_restic_mode`, `test_prune_config_dir_ignores_non_timestamp_entries`.
- [ ] **Step 2:** FAIL. **Step 3:** implement. **Step 4:** `pytest -q` (whole suite once — compress is the hot path) PASS.
- [ ] **Step 5:** README: "Deduplicated storage (restic)" and "Retention" sections, `retention:` in the config reference, notices; CHANGELOG entry. Commit `feat: compress into the restic store and prune after each run`.

### Task 5: Read verbs in restic mode — list, show-configs, export

**Files:**
- Modify: `config_saver/lib/cli/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `ResticStore.snapshots`, `ResticStore.dump`, `crypto.encrypt_file`, `Parser`.
- Behaviour: in RESTIC mode `--list` shows snapshots grouped by `cfg` (date + description) **plus** any tar archives still on disk (labelled as not migrated); `--show-configs` unions both. `--export-config NAME` dumps the newest snapshot to a temp file in the destination dir, gzips it to `<NAME>-<YYYYMMDD-HHMMSS>.tar.gz`, then encrypts with the `encrypt:` block of the config file named `NAME` found via `default_config_dirs()` (if found and it has one), atomically placed; when no config file is found → plain `.tar.gz` and a warning. `--export-all-configs` does that for every `cfg`. Temp plaintext removed on every path. Tar archives not yet migrated keep today's copy behaviour if newer than the newest snapshot.

- [ ] **Step 1: Failing tests** — `test_list_restic_mode_groups_by_cfg_and_shows_unmigrated`, `test_export_config_restic_gz_then_age` (fake store dump returns a real tar bytes; result decompresses with `TarDecompressor` + identity), `test_export_config_missing_yaml_plain_and_warns`, `test_export_no_plaintext_left_on_encrypt_failure`.
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5:** README export/list notes; commit `feat(cli): list and export from the restic store`.

### Task 6: `--restic-init`, `--migrate`, `--prune`

**Files:**
- Create: `config_saver/lib/migrate.py`
- Modify: `config_saver/lib/cli/cli.py`
- Test: `tests/test_migrate.py`, `tests/test_cli.py`

**Interfaces:**
- Produces: `migrate_archives(manager: BackupManager, store: ResticStore, identity: str | None, delete: bool) -> MigrationReport` with `@dataclass MigrationReport: migrated: list[str]; skipped: list[str]; failed: list[tuple[str, str]]; deleted: list[str]; bytes_before: int`.
  - Order: oldest first. cfg/timestamp from `FILENAME_PATTERN` (move it to a shared module, e.g. `config_saver/lib/archive_names.py`, used by cli and migrate); legacy `config-saver-<ts>…` at the top level → `cfg="legacy"`; unparsable → mtime.
  - Skip when a snapshot with that `cfg` and identical `time` exists (idempotent).
  - Stream: decrypt (`age -d -i` / `gpg --decrypt` to stdout) → `gzip.open` → `store.backup(write=copy into stdin)`; the write callback returns a `CompressResult` with `added` = member count read. No plaintext temp files.
  - Delete only with `delete=True`, per archive, after `store.dump` of the new snapshot hashes (sha256) equal to the hash of the decompressed source stream computed during backup; then remove the archive and its `description.txt` and the empty timestamp dir. After the loop, one `restic check`; its failure is reported (deletes already done were individually verified).
- CLI flags (mutually exclusive with the existing action group): `--restic-init` (requires `--encrypt-to`, uses `--identity`), `--migrate [--delete-migrated]`, `--prune [--dry-run] [--input …]`. `--prune` covers every config with snapshots/dirs, defaults for orphaned (restic) and only-explicit (tar), `legacy` is skipped unless `--include-legacy` is given. Prints per-config removals and a total.
- `--migrate` and `--prune` in a TAR_* mode: `--prune` works (tar rules); `--migrate` → `EXIT_USAGE` with the init notice.

- [ ] **Step 1: Failing tests** — `test_migrate_oldest_first_preserves_time_and_description`, `test_migrate_is_idempotent_on_rerun`, `test_migrate_legacy_top_level_tagged_legacy`, `test_migrate_hyphenated_cfg_name` (`claude-conversations-20261019-120456.tar.gz.age` → cfg `claude-conversations`), `test_migrate_delete_only_after_hash_match` (fake dump returns different bytes → not deleted, listed in failed), `test_migrate_restic_lock_error_is_failure_not_crash`, `test_cli_restic_init_requires_encrypt_to`, `test_cli_prune_dry_run_deletes_nothing`, `test_cli_prune_skips_legacy_by_default`.
- [ ] **Step 2–4:** FAIL → implement → PASS. Extend `test_real_restic_roundtrip` (or a sibling) with a real `.tar.gz.age` migrated and deleted.
- [ ] **Step 5:** README "Migrating existing backups" (exact commands, warning that the age key is the only secret); CHANGELOG; commit `feat: restic init, migration and manual prune`.

### Task 7: End-to-end on a copy of the real store, then full gates

**Files:** none new (fixes go to the owning task's module with a test).

- [ ] **Step 1:** with the scratch restic on PATH, run against a **copy**: `HOME=<scratch>/home`, copy `~/.config/config-saver/configs.d` and 3 configs' archive history (`zsh`, `firefox`, `claude-conversations`) into it, plus the age key. Run: `--compress` (expect TAR_NOT_INITIALISED notice, no prune) → `--restic-init --encrypt-to <pub>` → `--migrate` → `--list` → `--compress` (expect snapshot + automatic prune) → `--prune --dry-run` → `--export-config claude-conversations` → `--decompress --input <export> --output <tmp> --identity <key>` and diff against the source tree → `--migrate --delete-migrated`. Record sizes before/after (`du -sh`) in the PR.
- [ ] **Step 2:** remove restic from PATH, run `--compress` → expect `StoreError` exit (store exists, binary missing).
- [ ] **Step 3:** full gates once: `pytest --cov=config_saver`, `ruff check . && ruff format --check .`, `mypy config_saver`, `scripts/mutation-gate.sh` in the cgroup. Fix reds with `pytest --lf`.
- [ ] **Step 4:** add a `docs/FINDINGS.md` entry for anything non-obvious met (restic's password-command error text, lock behaviour). Version bump + CHANGELOG per `docs/RELEASING.md`. Commit.

### Task 8: Ship chain (after the user merges config-saver)

- [ ] **config-saver-aur**: bump `pkgver`, add `optdepends=('restic: deduplicated encrypted storage and retention')`, refresh checksums/.SRCINFO, PR.
- [ ] **dasik-personal-config**: add `"restic"` to `common/packages.json` next to `"config-saver"`; no `retention:` overrides (defaults apply in restic mode); README section with the one-time steps (`config-saver --restic-init --encrypt-to age1xa2jq3d…`, `--migrate`, then `--migrate --delete-migrated` after checking `--list`). `dasik plan` against each machine JSON must show only the `restic` package added. PR.
- [ ] Never merge; each PR gets the build-and-smoke verdict comment per the repos' rules.
