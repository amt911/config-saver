# Multi-machine Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `config-saver --sync` keeps the files of every `sync: true` configuration identical across
machines through an encrypted restic repository carried by a private git remote, aborting with exit
`11` (and a desktop notification from systemd) on any conflict.

**Architecture:** A new package `config_saver/lib/sync/` holds pure logic (fingerprints, three-way
classifier, manifest, head selection) and thin IO ports (git, state file) plus one engine that
orchestrates. The restic side reuses `ResticStore` pointed at the sync directory; files enter via
`TarCompressor` (same normalization) and leave via `TarDecompressor` (same extraction guard), both
gaining a path filter. CLI and systemd wire it in.

**Tech Stack:** Python 3, Pydantic, tarfile, restic, age, git (subprocess), pytest + Hypothesis,
systemd user units, `notify-send` (libnotify).

**Spec:** `docs/superpowers/specs/2026-11-23-multi-machine-sync-design.md`

## Global Constraints

- Sync dir `~/.local/share/config-saver/sync/` (`0700`); state `~/.local/state/config-saver/sync-state.json` (`0600`). Both honour `$XDG_DATA_HOME` / `$XDG_STATE_HOME`.
- Inside the sync dir the restic layout is `ResticStore`'s own: `restic/`, `restic-password.age`,
  `restic-store.json` (deviation from the spec's `sync-password.age` name: reuse beats renaming). `restic-store.json`
  is **gitignored** (it holds a local identity path); `.gitignore` also lists `restic/locks/`.
- Exit codes: conflict `11`; sync not set up / `git`, `restic`, `age` missing → `2`; git or network failure → `10`.
- `sync: true` with `only_root_user: true` → validation error (exit `3`).
- Only regular files and symlinks are synced; directories are implied (empty directories are not synced — README says so).
- Remote paths outside the local scope are never written, deleted or reported.
- Conflict ⇒ zero writes to the user's files, zero commits, state untouched.
- Every byte restored goes through `TarDecompressor`'s member validation; no second guard.
- Tests: `tmp_path` only, bare git repo in `tmp_path` as remote, two fake HOMEs as two machines; tests needing `restic`/`age`/`git` skip when the binary is missing, like `tests/test_restic_integration.py`.
- Sync push retry: at most 3 attempts.
- Large tree guard: refuse a `sync: true` configuration whose scope exceeds **50 MiB** of file content (error names the config; README documents it).
- Timer: `OnBootSec=2min`, `OnUnitActiveSec=15min`, `Persistent=true`.

## Review Focus

1. **Two machines with different user names** (`/home/andres` vs `/home/amt`) editing a text file that contains their home path: must sync without a conflict, content denormalized on arrival. → test in Task 7.
2. **A file that becomes a symlink (or vice versa) on one side**: classified as a change, applied without following the old symlink. → test in Task 2 + Task 7.
3. **Remote snapshot whose tar holds a member outside the local scope or with `../`**: never written; `../` raises `UnsafeArchiveError` (exit 5). → test in Task 7.
4. **Machine B's YAML lists fewer files than A's**: B publishing must not delete A's extra files from the shared manifest. → test in Task 2 (classifier) and Task 7 (engine).
5. **Interrupted run after push but before state save**: next run must treat the already-pushed head equal to local and not report a conflict (L == R counts as agreement). → test in Task 7.

---

### Task 1: `sync` field in the model

**Files:**
- Modify: `config_saver/lib/models/model.py`
- Test: `tests/test_parser.py`

**Interfaces:**
- Produces: `Model.sync: bool = False`.

- [ ] **Step 1: Failing tests** — `test_sync_defaults_false` (`make_model([...]).sync is False`); `test_sync_true_accepted`; `test_sync_with_only_root_user_rejected` (`pytest.raises(ValidationError)` for `sync=True, only_root_user=True`).
- [ ] **Step 2:** `pytest tests/test_parser.py -k sync` → FAIL (extra field forbidden).
- [ ] **Step 3:** Add the field (description: "Share this configuration's files with other machines through `--sync`") and a `model_validator(mode="after")` raising `ValueError("sync cannot be combined with only_root_user")`.
- [ ] **Step 4:** Same command → PASS; also `pytest tests/test_shipped_examples.py`.
- [ ] **Step 5:** Commit `feat(models): sync opt-in field`.

### Task 2: Pure three-way classifier

**Files:**
- Create: `config_saver/lib/sync/__init__.py`, `config_saver/lib/sync/classify.py`
- Test: `tests/test_sync_classify.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class Fingerprint: sha256: str; mode: int; link: str | None` (`link` = symlink target, `sha256` of the target text for symlinks).
  - `Side = dict[str, Fingerprint]` (key: normalized arcname, e.g. `home/user/.zshrc`).
  - `@dataclass class Plan: take_remote: list[str]; delete_local: list[str]; publish: list[str]; conflicts: list[str]` (all sorted).
  - `classify(base: Side, local: Side, remote: Side, local_scope: set[str], remote_scope: set[str]) -> Plan`.

Rules, per path `p` in `local_scope`, with `B/L/R` = entry or absent; a remote absence counts as a deletion only when `p in remote_scope`, otherwise `R := B`:
`L==R` → nothing; `L==B, R!=B` → take_remote (or delete_local if R absent); `R==B, L!=B` → publish; otherwise conflict. Paths not in `local_scope` never appear in the plan.

- [ ] **Step 1: Failing tests** — one example test per row of the rule table; `test_out_of_scope_remote_path_ignored`; `test_remote_absence_outside_remote_scope_is_not_deletion` (Review Focus 4); `test_file_to_symlink_is_change` (Review Focus 2); Hypothesis properties: `classify(b,l,r) conflicts == classify(b,r,l) conflicts` (symmetry); `classify(b, x, x)` has no conflicts and no publish/take; when `l == b` there are no conflicts and no publish.
- [ ] **Step 2:** `pytest tests/test_sync_classify.py` → FAIL (import error).
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** → PASS.
- [ ] **Step 5:** Commit `feat(sync): three-way classifier`.

### Task 3: Manifest and head selection

**Files:**
- Create: `config_saver/lib/sync/manifest.py`
- Test: `tests/test_sync_manifest.py`

**Interfaces:**
- Consumes: `Fingerprint` (Task 2).
- Produces:
  - `MANIFEST_MEMBER = ".config-saver-sync-manifest.json"`, `SELF = "@"`.
  - `@dataclass class Manifest: cfg: str; scope: list[str]; entries: dict[str, tuple[Fingerprint, str]]` — the `str` is the snapshot id holding the content, `SELF` for the snapshot that carries this manifest.
  - `Manifest.to_json() -> bytes`; `Manifest.from_json(data: bytes) -> Manifest` raising `ArchiveError` on any shape error (untrusted input: types checked, every path rejected if absolute or containing `..`, `sha256` must be 64 hex chars, snapshot id hex or `SELF`).
  - `Manifest.resolve(own_id: str) -> Manifest` replacing `SELF` with `own_id`.
  - `Manifest.side() -> Side`.
  - `find_head(snaps: list[tuple[str, str | None]]) -> str | None` — input `(snapshot_id, parent_id)`; returns the only id no other names as parent; `None` when empty; raises `StoreError("sync history of <cfg> forked")` for two heads.

- [ ] **Step 1: Failing tests** — round trip; each rejection (`../x`, `/etc/x`, bad hash, non-dict, unknown key); `find_head` linear chain, empty, fork.
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5:** Commit `feat(sync): manifest format and head selection`.

### Task 4: Compressor/decompressor path filters and local fingerprints

**Files:**
- Modify: `config_saver/lib/tar_compressor/tar_compressor.py`, `config_saver/lib/tar_compressor/tar_decompressor.py`
- Create: `config_saver/lib/sync/scan.py`
- Test: `tests/test_sync_scan.py`, extend `tests/test_extraction_security.py`

**Interfaces:**
- Produces:
  - `TarCompressor.compress_to_stream(fileobj, only: Collection[str] | None = None, extra_members: Mapping[str, bytes] | None = None) -> CompressResult` — `only` filters by arcname (directories that are ancestors of kept files are still written); `extra_members` are written as plain `0600` files (used for the manifest).
  - `TarCompressor.iter_files() -> Iterator[tuple[str, str]]` — `(path, arcname)` of regular files and symlinks, excluding root-owned skips, same order as `_collect`.
  - `TarCompressor.content_stream(path) -> IO[bytes]` — the bytes that would be archived (normalized when `normalize_content`, else raw); caller closes.
  - `TarDecompressor(..., members: Collection[str] | None = None)` — when set, every other member is skipped **after** validation (validation still runs for all members, so a malicious member still raises).
  - `scan(model: Model) -> tuple[set[str], Side, int]` in `sync/scan.py` — `(scope, fingerprints, total_bytes)`; raises `ConfigSaverError` subclass `SyncTooLargeError` (in `errors.py`, subclass of `ValueError`+`ConfigSaverError`, mapped to exit 3) when `total_bytes > 50 MiB`.

- [ ] **Step 1: Failing tests** — `scan` on `sample_tree` with `normalize_content=True` gives the same sha for a file containing `$HOME` under two different fake HOMEs (Review Focus 1); symlink fingerprint uses target; `compress_to_stream(only={...})` produces exactly those members plus ancestors and `extra_members`; `TarDecompressor(members=...)` restores only the listed member and still raises `UnsafeArchiveError` for a `../` member it would have skipped.
- [ ] **Step 2–4:** FAIL → implement → PASS; run `pytest tests/test_compressor.py tests/test_roundtrip.py tests/test_extraction_security.py` too (no regression).
- [ ] **Step 5:** Commit `feat(sync): path filters and local fingerprints`.

### Task 5: ResticStore tags for sync snapshots

**Files:**
- Modify: `config_saver/lib/restic_store.py`
- Test: `tests/test_restic_store.py` (unit), `tests/test_restic_integration.py` (skips without restic)

**Interfaces:**
- Produces:
  - `ResticStore.backup(..., extra_tags: Sequence[str] = ())` — each tag validated by `_check_cfg` rules (no comma).
  - `Snapshot.tags: tuple[str, ...] = ()` filled by `snapshots()`.
  - `ResticStore.snapshots(cfg=None, tag: str | None = None)` — extra tag filter ANDed.
  - Constants `SYNC_TAG = "sync"`, `PARENT_TAG_PREFIX = "parent:"`, `HOST_TAG_PREFIX = "host:"`.

- [ ] **Step 1: Failing tests** — argv contains the extra tags; comma in an extra tag raises `StoreError`; integration: backup with `extra_tags=[SYNC_TAG, "parent:none"]` then `snapshots(tag=SYNC_TAG)` returns it with tags.
- [ ] **Step 2–4:** FAIL → implement → PASS; `pytest tests/test_restic_store.py tests/test_restic_integration.py`.
- [ ] **Step 5:** Commit `feat(store): extra tags for sync snapshots`.

### Task 6: Git port and state file

**Files:**
- Create: `config_saver/lib/sync/git_repo.py`, `config_saver/lib/sync/state.py`, `config_saver/lib/sync/paths.py`
- Modify: `config_saver/lib/errors.py`
- Test: `tests/test_sync_git.py`, `tests/test_sync_state.py`

**Interfaces:**
- Produces:
  - `errors.SyncTransportError(OSError, ConfigSaverError)` (→ exit 10), `errors.SyncNotSetUpError(FileNotFoundError, ConfigSaverError)` (→ exit 2), `errors.SyncConflictError(ConfigSaverError)` with `conflicts: dict[str, list[str]]` (→ exit 11).
  - `sync/paths.py`: `sync_dir() -> str`, `state_path() -> str` (XDG-aware).
  - `class GitRepo(path: str)`: `clone(url) -> None`, `init(url) -> None` (init, `.gitignore`, set `origin`, branch `main`), `pull() -> None` (`git pull --no-rebase --no-edit`; a merge conflict → `git merge --abort` then `SyncTransportError`), `commit_all(message: str) -> bool` (False if nothing to commit), `push() -> bool` (False on non-fast-forward, `SyncTransportError` on other failures), `reset_to_upstream() -> None` (`git reset --hard @{u}` + `git clean -fd restic/`). Missing `git` → `SyncNotSetUpError`. Git always run with `-C path`, `GIT_TERMINAL_PROMPT=0`.
  - `sync/state.py`: `@dataclass class SyncState: heads: dict[str, str]; base: dict[str, Side]`; `load(path) -> SyncState` (missing → empty); `save(path, state)` atomic (`tmp` + `os.replace`), mode `0600`, parent `0700`.

- [ ] **Step 1: Failing tests** — with a bare repo in `tmp_path`: two clones, both commit different new files, second `push()` returns False, `pull()` merges cleanly, `push()` True; a real conflicting edit makes `pull()` raise `SyncTransportError` and leaves no merge in progress; `commit_all` with no changes is False; state round trip, mode `0600`, missing file → empty.
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5:** Commit `feat(sync): git port and state file`.

### Task 7: Sync engine

**Files:**
- Create: `config_saver/lib/sync/engine.py`
- Test: `tests/test_sync_engine.py` (needs restic, age, git; skip otherwise)

**Interfaces:**
- Consumes: Tasks 2–6.
- Produces:
  - `@dataclass class SyncReport: applied: dict[str, list[str]]; deleted: dict[str, list[str]]; published: dict[str, list[str]]; skipped_remote: list[str]; conflicts: dict[str, list[str]]`.
  - `class SyncEngine(sync_dir: str, state_file: str, identity: str | None, host: str)`:
    - `init(remote_url: str, recipient: str | None) -> None` — remote has commits ⇒ clone; else `ResticStore.init(recipient)` + `GitRepo.init` + commit + push. `recipient` required only in the second case (else `SyncNotSetUpError` message says to pass `--encrypt-to`).
    - `status(configs: dict[str, Model]) -> SyncReport` — pull, classify, no writes.
    - `run(configs: dict[str, Model]) -> SyncReport` — algorithm below; raises `SyncConflictError` (after printing nothing; CLI prints).
    - `resolve(configs, side: Literal["local", "remote"], paths: list[str] | None) -> SyncReport` — rewrites `base` for the chosen conflicting paths so the next classification favours `side` (local ⇒ base := remote entry; remote ⇒ base := local entry), then `run`.

`run` algorithm (decisions the implementer cannot derive):
1. `pull()`. For each `sync: true` config: `scan`; head = `find_head` over `snapshots(cfg, tag=SYNC_TAG)` parent tags; remote manifest = dump head, read `MANIFEST_MEMBER`, `resolve(head)` (none ⇒ empty side, empty scope).
2. `classify(state.base.get(cfg, {}), local, remote, local_scope, set(remote.scope))`. Collect conflicts across **all** configs; any ⇒ raise before step 3.
3. Apply: per config, group `take_remote` by holding snapshot id; `dump` each to a private temp file; `TarDecompressor(tmp, output_dir=<0700 staging dir inside the sync dir's parent filesystem: sync_dir/.staging>, members=group)`; then for each path `os.replace(staging/arcname, denormalize(arcname))` (create parents `0700` if missing); `delete_local` → `os.unlink`.
4. If any config has `publish`: new manifest = remote entries outside local scope (carried, with their snapshot ids) + local entries in scope (`SELF` for published paths, remote id for unchanged ones); scope = local scope ∪ remote scope; `backup` with `only=publish`, `extra_members={MANIFEST_MEMBER: manifest}`, tags `[SYNC_TAG, parent:<head or none>, host:<host>]`. `commit_all`, `push()`; False ⇒ `restic forget` the new snapshots, `reset_to_upstream()`, restart from 1 (max 3, then `SyncTransportError("another machine kept pushing")`).
5. After push succeeded (or nothing to publish): `state.base[cfg] = merged side`, `state.heads[cfg] = new head`; `save`.

- [ ] **Step 1: Failing tests** (two fake machines = two `SyncEngine`s with own HOME/state/sync dir, one bare remote):
  - `test_edit_on_a_arrives_on_b` (bytes and mode equal).
  - `test_conflict_aborts_without_writes` — edit both; `run` on second raises `SyncConflictError` listing the path; B's file bytes, state file and git HEAD unchanged.
  - `test_resolve_local_then_remote_side_gets_it`.
  - `test_delete_propagates`; `test_delete_vs_modify_conflicts`.
  - `test_narrower_yaml_does_not_delete_wider_files` (Review Focus 4).
  - `test_different_usernames_no_conflict` (Review Focus 1; second HOME `/…/amt`).
  - `test_malicious_member_refused` — craft a sync snapshot whose tar has `home/user/../../evil`; `run` raises `UnsafeArchiveError`, nothing outside HOME written (Review Focus 3).
  - `test_out_of_scope_member_not_written`.
  - `test_rerun_after_push_before_state_save` (Review Focus 5).
  - `test_concurrent_push_retries` — A publishes between B's pull and push (inject via a `GitRepo` subclass hook); B ends up with both changes, one linear chain.
  - `test_non_sync_config_never_uploaded` — a config without `sync: true` passed in; grep the whole remote bare repo and the restic dump for its marker string → absent.
  - `test_remote_has_no_plaintext` — a secret marker string in a synced file never appears in any file of the bare remote.
- [ ] **Step 2:** `pytest tests/test_sync_engine.py` → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** → PASS (inside the memory cgroup, `timeout --kill-after=60s 900`).
- [ ] **Step 5:** Commit `feat(sync): engine`.

### Task 8: CLI

**Files:**
- Modify: `config_saver/lib/cli/cli.py`, `README.md` (usage, exit-code table with `11`, new "Sync between machines" section: setup on first machine, bootstrap on the next, resolving a conflict, limits: per-machine YAMLs, no empty dirs, 50 MiB, git history only grows)
- Test: `tests/test_cli.py` (exit codes, subprocess), `tests/test_sync_e2e.py`

**Interfaces:**
- Consumes: `SyncEngine` (Task 7), `_collect_configs` existing loader.
- Produces flags: `--sync`, `--sync-init URL` (with optional existing `--encrypt-to`), `--sync-status`, `--sync-resolve {local,remote}` with positional-free repeatable `--path P`. `EXIT_SYNC_CONFLICT = 11`. Mutually exclusive with the other actions (exit 6 when combined). Conflict output: one line per `cfg: path`, then `Resolve with: config-saver --sync-resolve local|remote [--path P]`.
- Mapping in `run()`: `SyncConflictError` → 11 (before generic branches), `SyncTooLargeError` → 3; the others fall into existing `FileNotFoundError`/`OSError` branches (2/10) — order the `except` clauses so they do.

- [ ] **Step 1: Failing tests** — unit: each flag parses, combinations rejected (6), `--sync` without setup → 2. E2E (subprocess, two temp HOMEs, bare remote; comment says why it is E2E: real exit code + real files): `--sync-init` on A, `--sync-init` on B (clone), edit on A, `--sync` A then B → file identical; edit both → B exits **11**, stdout names the file; `--sync-resolve remote` on B → 0 and file equals A's.
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5:** Commit `feat(cli): --sync, --sync-init, --sync-status, --sync-resolve`.

### Task 9: systemd units and notification

**Files:**
- Create: `contrib/systemd/user/config-saver-sync.service`, `contrib/systemd/user/config-saver-sync.timer`, `contrib/systemd/user/config-saver-notify@.service`
- Modify: `pyproject.toml`/packaging data list if units are enumerated there; `README.md` systemd section; `docs/FINDINGS.md` (DBUS finding, see step 3)
- Test: `tests/test_systemd_units.py`, `tests/test_packaging.py`

**Interfaces:**
- `config-saver-sync.service`: `Type=oneshot`, `UMask=0077`, `WorkingDirectory=%h`, `ExecStart=/usr/bin/env python3 -m config_saver --sync`, `OnFailure=config-saver-notify@%n.service`, no `[Install]` (timer-driven).
- `config-saver-sync.timer`: values from Global Constraints, `Unit=config-saver-sync.service`, `WantedBy=timers.target`.
- `config-saver-notify@.service`: `Type=oneshot`, `ExecStart=-/usr/bin/notify-send -u critical -a config-saver "config-saver: sync failed" "%i failed. Run: config-saver --sync-status"` (leading `-`: a missing notifier must not cascade).

- [ ] **Step 1: Failing tests** — units parse (existing parametrized test picks them up); `OnFailure` value; timer values; notify unit uses `%i`; packaging test lists the new units.
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 3b: Real-environment check** (committed script `scripts/check-sync-notify.sh`, per real-environment-verification rules): installs the units into a temp `systemd --user` drop-in dir, starts a failing copy of the service (`ExecStart=/bin/false`) and asserts the notify unit ran (`systemctl --user show -p ActiveEnterTimestamp`). Run it once seen failing (point `OnFailure` at a wrong name), then passing. If the notification needs `systemctl --user import-environment DBUS_SESSION_BUS_ADDRESS`, record it in `docs/FINDINGS.md` and README.
- [ ] **Step 5:** Commit `feat(systemd): sync timer and failure notification`.

### Task 10: Finish

- [ ] Full suite once, in the cgroup: `systemd-run --user --scope --quiet -p MemoryHigh=5G -p MemoryMax=6G -p MemorySwapMax=0 -- timeout --kill-after=60s 1800 pytest --cov=config_saver` → green, coverage ≥ 80%.
- [ ] `ruff check . && ruff format --check . && mypy config_saver`.
- [ ] Mutation gate on the new pure modules: add `config_saver/lib/sync/classify.py` and `manifest.py` to `SCOPE` in `scripts/mutation-gate.sh`; run it in the cgroup; threshold unchanged (93).
- [ ] Wheel smoke: build, install in a scratch venv, `config-saver --sync` without setup → exit 2 with the init hint.
- [ ] Update `AGENTS.md` layout (`config_saver/lib/sync/`) and the "What to test per module" table.
- [ ] Note for `config-saver-aur`: add `optdepends=('libnotify: desktop notification when sync fails' 'git: --sync')` (separate repo, not done here).
- [ ] Creating the private GitHub remote (`gh repo create --private`) is NOT part of the code: ask the author at that moment.
- [ ] Commit, then PR per `docs/agents/pr-verification.md` (push only when the author allows).
