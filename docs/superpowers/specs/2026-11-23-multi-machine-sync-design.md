# Multi-machine sync of configuration files — design

Date: 2026-11-23. Status: agreed in conversation, pending the author's approval of this document.

## Intent and why

The author works on several machines and wants the **configuration files themselves** (`~/.zshrc`,
VS Code settings, lazygit…, not the config-saver YAML definitions) to follow them: a change made on
one machine shows up on the others on its own, driven by a timer. Some configurations must **never**
leave their machine — KDE/Plasma files (`default-config.yaml`, `klipper.yaml` in the author's
`configs.d`) are machine-specific.

Simultaneous edits must not be silently resolved. On a conflict the run **aborts, the service fails,
and the desktop shows a notification** — generic freedesktop, not KDE-only.

The files include secrets (the author's configs reach `~/.ssh`, rclone credentials), so whatever
leaves the machine is encrypted end to end; the hosting service only ever sees ciphertext. The
architecture of the existing restic store (restic encryption, password wrapped with `age`) is kept.

## Goals

1. Opt-in per configuration: a new `sync: true` field in the config YAML. Configurations without it
   are never synced (default `false`).
2. A dedicated **sync restic repository**, separate from the backup store, whose directory is a
   **git working tree** pushed to a private git remote. That remote is the only shared medium.
3. A three-way comparison per file (base = state at this machine's last successful sync, local =
   disk now, remote = newest synced state) that:
   - pulls remote-only changes onto disk,
   - publishes local-only changes as a new sync snapshot and pushes it,
   - **aborts before writing anything** when any file changed on both sides to different content,
     exiting with a new dedicated exit code.
4. Deletions propagate like changes (a deleted file is "content = absent"); delete-vs-modify is a
   conflict.
5. A systemd **user** timer + service for sync, plus an `OnFailure=` unit that sends a desktop
   notification with `notify-send` (libnotify / `org.freedesktop.Notifications`), so it works on KDE,
   GNOME, XFCE, Cinnamon, sway+mako, etc. The CLI itself stays headless.
6. A way to resolve a conflict by hand and a way to bootstrap a new machine (see "How").

## Non-goals

- Merging file contents line by line. Conflict = abort; the human decides.
- Syncing system files or `only_root_user: true` configurations (reject `sync: true` together with
  `only_root_user: true` at validation, exit 3).
- Syncing the backup store. Backups and sync are independent; the backup store is untouched.
- Per-machine keys / key rotation. The author uses **one age identity copied to every machine**
  (`~/.config/age/key.txt`) and accepts that one lost machine exposes everything. Multi-recipient
  is a possible later extension, not built now.
- Pruning the sync repository. Git keeps all history anyway (see Constraints).
- Real-time sync (inotify). Timer only.

## Anti-goals (would be a failure even if it "works")

- Any run that writes some files and then aborts on a conflict — partial application.
- A conflict resolved silently (last writer wins, `.sync-conflict` copies left behind and the run
  reported as success).
- Plaintext of any synced file, or the restic password, reaching the git remote.
- A synced archive member written outside the path its configuration declares (sync restores are
  extractions of data that came from the network: the existing extraction guard applies in full).
- A KDE configuration (or any without `sync: true`) ever being uploaded.

## Constraints

- Archives hold secrets: sync repo dir, state file and any temp tree are created `0700`/`0600`.
- Every member coming out of the sync repo is untrusted input; restoring goes through the **same**
  extraction guard as `--decompress` (`tar_compressor/tar_decompressor.py`) — no second guard.
- Must run headless under a systemd user timer. `--progress` stays optional.
- Tests never touch the real `$HOME`, real git remotes or GitHub: a bare git repo in `tmp_path`
  plays the remote; two temp `HOME`s play two machines.
- Git history keeps every pack forever. Fine for dotfiles (a few MB/year); a `sync: true`
  configuration whose tree is large must be refused or warned about (threshold left to the builder,
  documented in README).
- Heavy jobs follow AGENTS.md (memory cgroup, timeouts).

## Approaches considered

- **A. Restic store (chosen), transported through a private git repo.** Encrypted by restic with the
  password wrapped by `age`; GitHub sees ciphertext only. Restic repo files are content-addressed and
  never modified after creation (`data/`, `index/`, `snapshots/` are add-only), so two machines that
  each add a snapshot merge in git as a pure union with no git conflicts. The semantic conflict check
  is done by config-saver with hashes, not by git. Cost: we write the three-way logic ourselves —
  pure, property-testable.
- **B. Plain git repo of the files.** Free conflict detection and readable history, but secrets would
  sit in plaintext on the remote, or per-file encryption would throw away git's diff/merge value.
  Rejected.
- **C. External tool (chezmoi / Syncthing).** Syncthing does not abort on conflict (it leaves
  `.sync-conflict` copies and carries on); chezmoi needs a file list maintained apart from the YAMLs.
  Rejected.

The author picked A, explicitly asking for the restic architecture with end-to-end encryption over a
private "sync repo".

## Decided by the author

- Approach A, as above.
- Timer-driven; conflict ⇒ abort + failed service + desktop notification; notification generic
  across desktops if possible (it is: libnotify).
- Selective: per-configuration opt-in, KDE configs stay local.
- One shared age identity on all machines.
- The private git remote may be created by Claude with `gh repo create --private` — **only after the
  author confirms at that moment** (outward-facing action).

## How — proposed (Claude's calls, correctable)

### Layout

- Sync repo working tree: `~/.local/share/config-saver/sync/` (XDG data dir), mode `0700`:
  - `restic/` — the restic repository,
  - `sync-password.age` — restic password encrypted to the shared age recipient,
  - `.gitignore` — `restic/locks/`, `restic/cache` and anything restic writes that is per-run.
- Per-machine state (never committed): `~/.local/state/config-saver/sync-state.json` (`0600`):
  last synced snapshot id per configuration + `{normalized path: sha256 | null}` at that point.
- Reuse `ResticStore` (`config_saver/lib/restic_store.py`) pointed at the sync directory rather than a
  second wrapper; extend it where needed (e.g. a constructor taking explicit repo/password paths).

### Snapshot content and manifest

Each sync snapshot of a configuration carries the files **and** a manifest (path → sha256, deletions
implicit by absence) so the three-way comparison can read remote hashes without restoring the tree.
Paths are stored in the existing normalized (home-relative) form so machines with different user
names / home dirs match. Snapshots are tagged `sync`, the configuration name, and the producing
host name; the "remote" state of a configuration is its newest `sync` snapshot.

### One run (`config-saver --sync`)

1. `git pull` (merge; union of restic files). If git itself reports a conflict (should not happen
   given add-only files), abort with the sync-conflict code.
2. For every `sync: true` configuration: compute local hashes (honouring `exclude:`), read base from
   state, read remote manifest from the newest snapshot.
3. Classify each path: unchanged / take-remote / publish-local / conflict (both changed, L ≠ R).
   L == R with both changed counts as agreement, not conflict.
4. **If any conflict in any configuration: print every conflicting path (per config), write
   nothing, exit with the new code.**
5. Otherwise apply take-remote paths (restore via the extraction guard, deletions via unlink), then
   if any publish-local paths exist, take a new sync snapshot of the configuration's current tree +
   manifest, `git add/commit/push`.
6. Push rejected (another machine pushed first) → pull and restart from 1, bounded retries
   (e.g. 3), then fail with the git/I-O code.
7. Update the state file only after the push succeeded.

### New CLI surface

- `--sync` — one sync run (what the timer calls).
- `--sync-init REMOTE_URL` — create the sync dir, restic repo, `sync-password.age`, first commit,
  set remote, push. On a machine where the remote already exists: clone instead (bootstrap).
- `--sync-resolve {local,remote} [PATH...]` — after a conflict, take one side for the given paths
  (all conflicting paths when none given), then run a normal sync. This is also how a new machine
  bootstraps when its existing files differ from the shared state (no base ⇒ every differing file is
  a conflict, by design).
- `--sync-status` — show per-path classification without changing anything (dry run).
- New exit code (next free, `11`): sync conflict. Git/network failures reuse `10` (I/O) or a further
  new code — builder's call, documented in the README table.

### systemd

- `contrib/systemd/user/config-saver-sync.service` (oneshot, `UMask=0077`, `--sync`) and
  `config-saver-sync.timer` (e.g. `OnBootSec=2min`, `OnUnitActiveSec=15min`; no `Persistent=`, it only affects `OnCalendar=`).
- `config-saver-notify@.service`: `ExecStart=notify-send -u critical -a config-saver "…" "…%i…"`,
  referenced from the sync service via `OnFailure=config-saver-notify@%n.service`.
  The message points at `config-saver --sync-status`.
- `notify-send` is an optional dependency (`libnotify`); AUR package lists it as optdepends.

### Definitions stay per machine (decided by the author)

The YAML definitions are **not** synced. Each machine syncs exactly what its own `sync: true`
configurations declare; configurations are matched across machines **by name**.

- A machine's **scope** for a configuration = the set of normalized paths its own YAML selects
  (after `exclude:`). Remote paths outside the local scope are **ignored** — never written, never
  deleted, never reported as conflicts.
- Absence means deletion **only within the producer's scope**: each snapshot records the scope of the
  machine that produced it, so B (which lists only `.zshrc`) never deletes A's `.zsh_aliases`.
- Publishing from B must **carry forward** the remote manifest entries outside B's scope. B does not
  have those files on disk, so a manifest entry references the snapshot that holds its content, and
  a new snapshot only stores the files that changed. Restoring a path reads it from the referenced
  snapshot. (Exact encoding left to the builder; the requirement is: no machine can drop or
  overwrite paths it does not declare.)
- A remote configuration with no local YAML of that name is skipped (one line in `--sync-status`).
- Known, accepted consequence: a file added to one machine's YAML is not picked up by the others
  until the author adds it to their YAML too. README says so.

### Settled after the builder check (Claude's calls)

- **What is hashed:** the manifest hashes the **normalized** form (the same bytes the compressor
  would store, home path replaced by the placeholder), so machines with different user names agree.
  An entry is `(sha256 of normalized content, mode, symlink target)`; a change of mode or symlink
  target is a change.
- **History is linear:** every sync snapshot carries its parent snapshot id (tag). Pushes serialize
  through git, so the chain stays linear; "remote" = the snapshot of that configuration that no other
  snapshot names as parent, **never** chosen by timestamp (clocks skew). On a rejected push the
  unpublished local commit is discarded (`git reset` to the fetched head + `restic forget` of the
  orphan snapshot) before restarting from step 1. The state file records the snapshot id synced,
  not a time.
- **No remote snapshot yet** for a `sync: true` configuration ⇒ publish everything local.
- **Apply is staged:** take-remote files are extracted to a `0700` temp dir on the same filesystem,
  then renamed into place one by one. Conflicts guarantee zero writes; an I/O error mid-apply is
  best-effort (already-renamed files stay, state not updated, so the next run reclassifies them).
- **Manifest cost:** the remote manifest must be readable without dumping the configuration's tree
  (a 15-min timer cannot afford `restic dump` of a large tar). Store it as its own small stdin
  snapshot next to the data snapshot, or equivalent — builder picks, cost bound is the requirement.
- **Not set up / missing `git`, `restic`, `age`:** `--sync` exits `2` (not found) with the command to
  run (`--sync-init`); install hints like the existing restic notice.

## Left to the builder

- Exact manifest storage (file inside the snapshot vs. snapshot tag/JSON side file), as long as it is
  inside the restic-encrypted data.
- How the bounded push retry and git invocation are wrapped (a thin port over `git` so tests use a
  bare repo in `tmp_path`).
- The large-tree threshold and its message.
- Whether git failures get their own exit code.
- Checking that `DBUS_SESSION_BUS_ADDRESS` reaches `OnFailure=` units of the user manager on
  KDE/GNOME; record in `docs/FINDINGS.md` if it needs `systemctl --user import-environment`.

## Verification expected

- Pure three-way classifier: Hypothesis property tests (symmetry of conflict detection, idempotence
  of a second run, "abort ⇒ no file changed").
- One end-to-end journey: two temp HOMEs + bare remote in `tmp_path`; edit on A → sync A → sync B →
  file on B identical (bytes, mode); concurrent edit on both → second sync exits `11` with no changes
  on disk; `--sync-resolve` then clears it.
- Security regressions: a sync snapshot crafted with a `../` member / absolute path / escaping
  symlink is refused on restore; a configuration without `sync: true` never appears in any snapshot;
  the git remote contains no plaintext of a known secret string.
- systemd unit tests in the style of the existing ones (units parse, `OnFailure=` wired, `UMask`).
