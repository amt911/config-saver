# Sync over a restic remote — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the git transport of `config-saver --sync` with a restic repository at any
configurable restic URL (Google Drive / Proton Drive through rclone, sftp, s3, local…), with
derived password, fork resolution, pruning, per-config size limit and a hash cache, released as 3.7.0.

**Architecture:** `sync/remote.py` (new) holds `SyncRemote` — the one port that turns
`sync.json` + the age identity into a usable `ResticStore`; `sync/manifest.py` gains pure head
selection with fork resolution and pure "unreferenced snapshot" computation; `engine.py` drops git
and publishes/validates against restic directly; `git_repo.py` is deleted.

**Tech Stack:** Python 3, restic (rclone backend), age identity file, pytest + Hypothesis.

**Spec:** `docs/superpowers/specs/2026-11-23-sync-rclone-transport-design.md` (revises
`docs/superpowers/specs/2026-11-23-multi-machine-sync-design.md`; both bind).

## Global Constraints

- `~/.config/config-saver/sync.json` (0600, parent 0700): `{"repository": str, "options": [str]}`; unknown keys → validation error (exit 3).
- Derived restic password: lowercase hex `sha256(b"config-saver-sync-v1\0" + <the AGE-SECRET-KEY-… line, stripped, as bytes>)`; passed to restic via the environment (`RESTIC_PASSWORD`), never on argv. Identity default `~/.config/age/key.txt`, overridable with `--identity`.
- Fork winner: earliest `(time, id)` among a parent's children; every machine computes the same.
- Publish retry: at most 3 attempts, then `SyncTransportError` (exit 10).
- `sync_max_mb: int = 50` (≥1) in the Model; size error message unchanged except the limit value.
- Weekly prune: `restic prune` at most once every 7 days from a sync run; `--sync-prune` forces it.
- Lock file and state live under `~/.local/state/config-saver/` (XDG); staging under `~/.local/share/config-saver/sync/.staging` (0700).
- Exit codes unchanged: 11 conflict, 2 not set up / tool missing, 10 transport, 3 validation.
- Tests: local-path restic repositories in `tmp_path`, never a network remote; skip when restic/age missing. Heavy runs inside the memory cgroup.
- Version 3.7.0; the never-released 3.6.0 CHANGELOG section becomes 3.7.0.

## Review Focus

1. **Two machines publish on the same head** → exactly one winner, the loser's changes re-published on retry, nothing lost. → Task 3.
2. **Prune never forgets a snapshot a head manifest references** (else a carried file becomes unrestorable). → Task 2 (property) + Task 4.
3. **Hash cache returns a stale fingerprint** when a file changed but kept size+mtime (same-second edit) → must include `mtime_ns` and inode, and a changed `normalize_content`/HOME must invalidate. → Task 5.
4. **`--sync-move` interrupted midway** → `sync.json` still points at the old repo until the copy is verified. → Task 6.
5. **Identity file with comments/CRLF** → the same derived password on every machine. → Task 1.

---

### Task 1: SyncRemote, sync.json and derived password

**Files:** Create `config_saver/lib/sync/remote.py`; Modify `config_saver/lib/restic_store.py`, `config_saver/lib/sync/paths.py`; Test `tests/test_sync_remote.py`, `tests/test_restic_store.py`.

**Interfaces — Produces:**
- `ResticStore.for_repository(repository: str, password: str, options: Sequence[str] = ()) -> ResticStore` (classmethod): same verbs as today; `_env` sets `RESTIC_REPOSITORY`/`RESTIC_PASSWORD` instead of the password command; every restic invocation gets `-o <opt>` per option. Existing constructor unchanged.
- `ResticStore.exists() -> bool` (`restic cat config` succeeds), `ResticStore.init_plain() -> None` (`restic init` with the given password, no age file), `ResticStore.copy_from(src: ResticStore, ids: list[str]) -> None` (`restic copy --from-repo … --from-password-command`/env), `ResticStore.prune() -> None` (exists).
- `sync/paths.py`: `remote_config_path() -> str`, `lock_path() -> str`.
- `sync/remote.py`: `derive_password(identity_path: str) -> str`; `@dataclass(frozen=True) class SyncRemote: repository: str; options: tuple[str, ...]`; `load_remote(path) -> SyncRemote` (missing → `SyncNotSetUpError` with the `--sync-init <restic-repo>` hint; bad shape → `SyncStateError`); `save_remote(path, remote)` atomic 0600; `SyncRemote.store(identity_path) -> ResticStore`; `SyncRemote.needs_rclone -> bool` (`repository.startswith("rclone:")`).

- [ ] Step 1: failing tests — `derive_password` equal for an identity with comments+LF vs CRLF vs extra blank lines, different for another key, raises `SyncNotSetUpError` when the file has no `AGE-SECRET-KEY-` line; `load_remote`/`save_remote` round trip + 0600 + unknown key rejected; `for_repository` env contains `RESTIC_PASSWORD` and no password on argv, options become `-o` pairs (argv test like the existing ones); integration (restic present): `init_plain` on a tmp path, `exists()` True, second store with a wrong password raises `StoreError`.
- [ ] Step 2: run → FAIL. Step 3: implement. Step 4: `pytest tests/test_sync_remote.py tests/test_restic_store.py tests/test_restic_integration.py` → PASS.
- [ ] Step 5: commit `feat(sync): configurable restic remote and derived password`.

### Task 2: Pure head selection with forks, and unreferenced snapshots

**Files:** Modify `config_saver/lib/sync/manifest.py`; Test `tests/test_sync_manifest.py`.

**Interfaces — Produces:**
- `@dataclass(frozen=True) class SnapRef: id: str; parent: str | None; time: datetime`.
- `select_head(snaps: list[SnapRef]) -> str | None` — walk from roots; at each node with several children keep the earliest `(time, id)`; return the end of the winning chain. Replaces `find_head` (delete it and its fork error; update callers in Task 3).
- `losers(snaps: list[SnapRef]) -> set[str]` — ids not on the winning chain.
- `unreferenced(snaps: list[SnapRef], heads: Mapping[str, Manifest]) -> set[str]` — sync snapshot ids that are not a head and not referenced by any entry of any head manifest (`resolve`d).

- [ ] Step 1: failing tests — linear chain; fork → earliest wins regardless of list order; tie on time → smaller id; a loser's descendants are losers; empty → None; Hypothesis: `select_head` invariant under permutation of input; `unreferenced` ∩ (heads ∪ all referenced ids) = ∅ (Review Focus 2).
- [ ] Steps 2–4: FAIL → implement → `pytest tests/test_sync_manifest.py` PASS.
- [ ] Step 5: commit `feat(sync): fork-tolerant head selection and unreferenced snapshots`.

### Task 3: Engine without git

**Files:** Modify `config_saver/lib/sync/engine.py`, `config_saver/lib/tar_decompressor` only if needed; Delete `config_saver/lib/sync/git_repo.py`, `tests/test_sync_git.py`; Test `tests/test_sync_engine.py` (rewrite fixtures: two fake machines sharing one local-path restic repo).

**Interfaces:**
- Consumes: Task 1 (`SyncRemote`, `ResticStore.for_repository`, `exists`, `init_plain`), Task 2 (`SnapRef`, `select_head`, `losers`).
- Produces: `SyncEngine(remote: SyncRemote, identity: str | None, state_file: str, staging_root: str, lock_file: str, host: str, store: ResticStore | None = None)`; `.init() -> Literal["created", "joined"]`; `.status/.run/.resolve` unchanged signatures.

Algorithm changes (decisions):
1. `_analyse` no longer pulls/resets; it lists `snapshots(tag=SYNC_TAG)` once, builds `SnapRef`s per cfg, head = `select_head`.
2. Publish: back up as today with `parent:<head|none>`; then re-list the cfg's sync snapshots; if `select_head` ≠ the new id (lost a race) → `forget_ids` of this attempt's snapshots and restart the whole run (max 3, then `SyncTransportError("another machine kept publishing")`).
3. `init()`: `exists()` → "joined" (nothing written remotely); else `init_plain()` → "created".
4. Lock: flock on `lock_file`; staging under `staging_root/.staging`.
5. Remove every git reference; `SyncTransportError` now also wraps restic transport failures (`StoreError` from a network/backend failure stays `StoreError` → exit 5 is wrong for transport: map `StoreError` raised inside sync verbs to `SyncTransportError` with the restic message).

- [ ] Step 1: rewrite/extend failing tests — all existing engine behaviours (edit arrives, conflict aborts with zero writes, resolve both sides, deletes, narrower YAML, usernames, malicious member, symlink swaps, lock, hostile later group, missing member, rerun after publish before state save) on the shared local repo; new: `test_fork_race_one_winner_loser_retries` (inject a competing publish between backup and re-list via a store subclass hook; both machines' changes present after B's retry), `test_join_existing_repo`, `test_create_new_repo`, `test_restic_failure_is_transport_error`.
- [ ] Steps 2–4: FAIL → implement → `pytest tests/test_sync_engine.py tests/test_extraction_security.py` PASS (cgroup, timeout 900).
- [ ] Step 5: commit `feat(sync): restic remote transport, drop git`.

### Task 4: Pruning

**Files:** Modify `engine.py`, `sync/state.py`; Test `tests/test_sync_engine.py`, `tests/test_sync_state.py`.

**Interfaces — Produces:** `SyncState.last_prune: float | None` (epoch seconds; JSON key `last_prune`, absent in old files → None); `SyncEngine.prune(configs) -> list[str]` (forgotten ids; always runs `restic prune`).
Decisions: after a successful publish, `forget_ids(unreferenced(...) ∪ losers(...))` over **all** sync cfgs' heads read this run; then `restic prune` if `last_prune` is None or older than 7 days, update `last_prune`.

- [ ] Step 1: failing tests — after A publishes twice and B once, unreferenced snapshots are gone and every head-manifest-referenced id still exists; B can still restore a carried file afterwards; prune skipped when `last_prune` is recent (clock injected via a parameter, no sleeps).
- [ ] Steps 2–5: FAIL → implement → PASS → commit `feat(sync): forget unreferenced snapshots and weekly prune`.

### Task 5: Per-config size limit and hash cache

**Files:** Modify `config_saver/lib/models/model.py`, `config_saver/lib/sync/scan.py`, `config_saver/lib/sync/state.py`, `engine.py`; Test `tests/test_parser.py`, `tests/test_sync_scan.py`, `tests/test_sync_state.py`.

**Interfaces — Produces:** `Model.sync_max_mb: int = Field(50, ge=1)`; `scan(model, cache: MutableMapping[str, tuple[int, int, int, bool, Fingerprint]] | None = None)` where the key is the local path and the value `(size, mtime_ns, inode, normalize_content, fingerprint)`; a hit requires all four to match; `SyncState.hash_cache` persisted (JSON key `hash_cache`), entries for paths not seen in a scan are dropped.

- [ ] Step 1: failing tests — limit taken from `sync_max_mb`; cache hit skips hashing (spy on the hashing function), and each of size/mtime_ns/inode/normalize_content change forces a rehash (Review Focus 3); a file replaced by `os.replace` with same size and forced same mtime_ns still rehashes (inode).
- [ ] Steps 2–5: FAIL → implement → PASS → commit `feat(sync): sync_max_mb and hash cache`.

### Task 6: CLI — init, move, prune

**Files:** Modify `config_saver/lib/cli/cli.py`, `config_saver/lib/cli/sync_commands.py`, `engine.py` (move); Test `tests/test_cli.py`, `tests/test_sync_engine.py`, `tests/test_sync_e2e.py` (local-path repo).

**Interfaces:**
- `--sync-init URL [--sync-option K=V ...]` → writes `sync.json`, runs `engine.init()`, prints `created`/`joined`. `--encrypt-to` no longer used by sync.
- `--sync-move URL [--sync-option K=V ...]` → `SyncEngine.move(new: SyncRemote) -> int` (snapshots copied): target init if missing, `copy_from` every sync snapshot, verify per-cfg head manifests equal on both sides, **only then** `save_remote`. Failure leaves `sync.json` untouched (Review Focus 4).
- `--sync-prune` → `engine.prune`.
- Not-set-up checks: `restic`, `age` always; `rclone` only when `remote.needs_rclone`; missing `sync.json` → exit 2 hint `config-saver --sync-init <restic repository, e.g. rclone:Google Drive:config-saver-sync>`.

- [ ] Step 1: failing tests — flags parse/combos exit 6; move success switches config and B keeps syncing against the new repo; move with an injected copy failure leaves `sync.json` unchanged; e2e: init (created) on A, init (joined) on B, edit → sync A → sync B, conflict → 11, resolve → 0, prune → 0.
- [ ] Steps 2–5: FAIL → implement → PASS → commit `feat(cli): restic remote init, --sync-move, --sync-prune`.

### Task 7: Docs, packaging metadata, version

**Files:** `README.md`, `CHANGELOG.md` (3.6.0 section → 3.7.0, rewrite for the restic remote, add `sync_max_mb`, `--sync-move`, `--sync-prune`), `pyproject.toml` (3.7.0), `docs/FINDINGS.md` (main is PR-only: releases go through a PR; long pre-push hooks outlive an idle SSH connection — `ServerAliveInterval`), `AGENTS.md` (Layout: no git_repo; sync row), systemd unit text if it mentions git.

README sync section must cover: what syncs and what doesn't; setup on the first machine with **three examples** (`rclone:Google Drive:config-saver-sync`, `rclone:Proton:config-saver-sync` with an `rclone.args` tuning example, generic `sftp:`/local path); next machines (`--sync-init` same URL → joined; `--sync-status`/`--sync-resolve` before enabling the timer); `sync_max_mb` for sessions; before leaving home (`config-saver --sync`, close agents for SQLite); switching provider (`--sync-move`); key rotation (`restic key passwd`); limits.

- [ ] Steps: write docs; `pytest tests/test_packaging.py tests/test_shipped_examples.py tests/test_systemd_units.py`; commit `docs: sync over a restic remote; release 3.7.0`.

### Task 8: Finish

- [ ] Full suite once (cgroup, `timeout --kill-after=60s 1800`), ruff/format/mypy, coverage ≥ 80%.
- [ ] Mutation gate (cgroup, timeout 3600), threshold 93 untouched; `manifest.py` new pure functions included by the existing scope.
- [ ] Wheel smoke in a scratch venv: `--sync` with no `sync.json` → exit 2 with the init hint.
- [ ] Real-remote smoke **only with the author's go-ahead at that moment** (writes to their Google Drive): `--sync-init "rclone:Google Drive:config-saver-sync-smoke"` from a temp HOME with a throwaway config, sync, then delete that remote path.
- [ ] Push the branch and open the PR (main is PR-only); tag/release/AUR/arch-packages after the author merges.
- [ ] Other repos, after the PR (each on its own branch + PR): `dasik-personal-config` — `sync: true` (+ `sync_max_mb` for session YAMLs) on claude, claude-conversations, codex, codex-conversations, opencode, opencode-data, lazygit, gradle, agent-keys in `common/home/andres/.config/config-saver/configs.d/`, README pointer; dasik wiki page "Sync configs and agent sessions between machines"; `config-saver-aur` 3.7.0 (`rclone` replaces `git` in optdepends, sync units).
