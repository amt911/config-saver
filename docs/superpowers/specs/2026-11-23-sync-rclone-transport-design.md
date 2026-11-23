# Sync over a restic remote (rclone) instead of git — design revision

Date: 2026-11-23. Status: pending the author's approval. **Revises**
`2026-11-23-multi-machine-sync-design.md`: everything there still holds (opt-in `sync: true`,
per-file three-way classification, conflict ⇒ exit 11 with zero writes, per-machine definitions and
scope, single extraction guard, systemd timer + notification) **except the transport and what
follows from it**, which this document replaces.

## Why

The author wants the **agent sessions** synced too (Claude, Codex, opencode), not only settings:
measured on the torre, `~/.claude/projects` is 2.6 GB (one file 627 MB), `opencode.db` 375 MB,
Codex sessions ~100 MB, all growing. A git remote cannot carry that: history can never be pruned,
GitHub degrades past a few GB and rejects pushes over 2 GB. The author also asked to **simplify**:
no extra repositories.

Usage pattern (the author's words): agents run on the torre; when away from home the author wants
to resume on the laptop. Machines are used **one after the other, not concurrently** — so plain
per-file sync is enough; no line-merging of `.jsonl` histories is needed.

## Decisions (author)

- Transport: a **restic repository on Google Drive through restic's rclone backend**
  (`rclone:Google Drive:config-saver-sync`). No git, no GitHub repository for sync.
- Agent sessions are synced by marking the **existing** YAMLs `sync: true` (no parallel `sync-*`
  YAMLs). `zsh.yaml` is not synced (it only holds shell histories, one of them root-owned; the real
  zsh config already lives in its own git repo).
- The git-based 3.6.0 is not released; the feature ships as **3.7.0**.

## Changes to the design (Claude's calls, correctable)

1. **Remote = any restic repository URL, configurable (author asked for Proton, Google Drive, or
   anything generic).** restic is already the provider abstraction (`rclone:<any rclone remote>:path`,
   `sftp:`, `s3:`, `b2:`, `rest:`, local path), so there are **no per-provider classes**: they would be
   one implementation under several names. One small port, `SyncRemote(repository, options)`, builds
   restic's environment/arguments and is the test seam (tests use a local-path repository).
   `~/.config/config-saver/sync.json` = `{"repository": "<url>", "options": ["key=value", …]}` where
   `options` are restic `-o` settings for provider tuning (e.g. `rclone.args` for Proton).
   `--sync-move <new-url>` switches provider: `restic copy` of every sync snapshot into the new
   repository (same derived password), verify the head manifests match, then rewrite `sync.json`.
   Documented examples: Google Drive, Proton Drive, generic.
   Original point: `config-saver --sync-init <restic-repo>` records the URL
   in `~/.config/config-saver/sync.json` (0600). If a restic repository already answers at that URL
   the machine joins it; otherwise it is created. Google Drive via rclone is the documented setup;
   `sftp:`, `s3:`, a local path… work the same because restic does the transport.
2. **No password file to distribute.** The restic password is derived from the shared age identity:
   `sha256("config-saver-sync-v1\0" + <AGE-SECRET-KEY line>)`, hex. Every machine already holds
   that key (the author's model), so joining needs nothing else; the key stays the only secret.
   Consequence, documented: rotating the age key requires `restic key passwd` on the sync repo.
3. **Concurrency without git.** The head of a configuration is still the snapshot no other names as
   parent. After publishing, the machine re-lists the parent's children; if there is more than one
   (two machines published against the same head), the **winner is the earliest (time, id)** —
   every machine computes the same winner. The loser forgets its own snapshot and restarts from the
   top (max 3 attempts, then exit 10). Readers that see a fork pick the winner the same way, so a
   crashed loser's leftover never blocks anyone. The local `flock` stays.
4. **Pruning.** After a successful publish, sync snapshots that are neither a head nor referenced by
   a head manifest are forgotten; `restic prune` runs at most once every 7 days from a sync run
   (timestamp in the state file) and on demand with `--sync-prune`. Space no longer grows forever.
5. **Size limit per configuration.** New optional field `sync_max_mb` (default 50). Session YAMLs
   set it high (e.g. 4096).
6. **Hash cache.** Hashing ~3 GB every 15 minutes is too slow: the state file caches
   `(size, mtime_ns, inode) → fingerprint` per path; only changed files are rehashed. The cache is
   an optimisation only — a mismatch rehashes.
7. **Removed:** `GitRepo`, the git working tree, `git` as a dependency, the commit-metadata rules.
   Added optional dependency: `rclone` (only for rclone remotes).
8. **Timer before leaving home.** Documented: run `config-saver --sync` by hand before leaving if
   the last session ended less than 15 minutes ago; close agents before syncing so their SQLite
   databases (opencode, Codex) are not copied mid-write.

## Unchanged

Classifier, manifest format (snapshot ids, `SELF`), scope rules, staged extraction through
`TarDecompressor`, vet-all-before-write, unreadable-file ⇒ exit 10, conflict ⇒ 11, `--sync-status`,
`--sync-resolve`, systemd units and notification, exit codes.

## Non-goals

- Line-merging `.jsonl` histories (sequential use makes it unnecessary).
- Live-consistent copies of open SQLite databases.
- dasik support for enabling the sync timer (separate task in the dasik repo); until then
  `systemctl --user enable --now config-saver-sync.timer` by hand.

## Configurations to mark `sync: true` (author's machines)

`claude`, `claude-conversations`, `codex`, `codex-conversations`, `opencode`, `opencode-data`,
`lazygit`, `gradle`, `agent-keys` (session ones with `sync_max_mb`). Not: `zsh`, `default-config`
and `klipper` (KDE, per machine), `firefox`, `wallpapers`, `libvirt`, `own-configs`, `vscode`
(1.7 GB of editor state; revisit later). They are edited in `dasik-personal-config`
`common/home/…/configs.d`, so every machine gets them.

## Docs

config-saver README (setup with rclone + Google Drive, first machine, laptop, before leaving home,
conflicts), a step-by-step page in the **dasik wiki**, and a pointer in the `dasik-personal-config`
README.

## Verification

As before, plus: two fake machines sharing one local-path restic repo (no network in tests);
fork-race test (both publish on the same parent → one winner, loser retries, no data lost); prune
never forgets a referenced snapshot (property test); hash-cache invalidation on size/mtime/inode
change; a real `rclone:` smoke against a throwaway remote path on Google Drive run once by hand
with the author present (not in CI).
