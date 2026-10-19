# Deduplicated encrypted storage (restic) and snapper-style pruning — design

Date: 2026-10-19. Status: approved in conversation, pending plan.

## Intent and why

`~/.config/config-saver` on the author's machine is **35 GB** after ~55 daily runs; 27 GB of it is
`claude-conversations` (~500 MB per snapshot), then `wallpapers` 3.5 GB and `firefox` 2.8 GB. Almost
every file is identical between consecutive snapshots (old conversations never change), yet each
snapshot is a fresh `.tar.gz`, usually `age`-encrypted. Gzip plus encryption with a random nonce means
two snapshots of identical inputs share zero bytes, so btrfs copy-on-write / reflink / dedup can never
help in the current format.

Encryption at rest of the stored backups is a hard requirement (the author said so explicitly), so
deduplication must happen **before** encryption. The chosen engine is **restic**: content-defined
chunking, one copy of each chunk, authenticated encryption, and built-in retention (`forget`/`prune`).

Second want: a **snapper-like retention policy** so the number of snapshots stops growing forever.

## Goals

1. When restic is installed and the store is initialised, every `--compress` stores each config as a
   restic snapshot in one shared, encrypted repository; unchanged data is stored once.
2. When restic is **not** installed (or the store is not initialised), behaviour is exactly today's
   (`.tar.gz` [+ age/gpg]) and each compress run prints a one-line notice that deduplicated storage
   can be enabled (install `restic`, then run the init command).
3. Existing installations can **migrate** their historical `.tar.gz[.age|.gpg]` archives into the
   restic store, each keeping its original timestamp and description, so the author's current 35 GB
   collapse into the deduplicated store.
4. Per-config retention (`retention:` block in the config YAML, defaults when absent), applied
   automatically after every `--compress`, plus a manual `--prune` with a dry-run. Works in both
   storage modes.
5. Everything the user sees from outside stays the same format: `--export-config`,
   `--export-all-configs` and `--decompress` keep producing / consuming `.tar.gz` (encrypted with the
   config's `encrypt:` block when it has one).
6. After it works end to end, `dasik-personal-config` is updated to use it (see the last section).

## Non-goals

- Remote restic backends (s3, sftp, rest-server). The repo lives locally; syncing it elsewhere is the
  user's business (a restic repo is plain files and syncs fine).
- Browsing snapshots per file (`restic mount`, `restic ls`). Not required.
- Replacing `age`/`gpg` for exports.
- Deduplicating the legacy tar store (fallback mode stays exactly as wasteful as today — it is the
  fallback).

## Anti-goals (would be a failure even if it "works")

- **Losing a backup during migration**: an old archive deleted before restic has verifiably stored it.
- **Storing the restic password in the clear on disk.**
- **Silent downgrade**: restic configured but failing, and config-saver quietly writing nothing or
  quietly falling back without saying so. A restic failure for a config is a reported per-config error
  (same `ConfigOutcome.error` path as today), not a fallback.
- **Pruning the newest snapshot of a config**, under any policy or bug.
- Breaking existing users who never install restic.

## Decisions taken in conversation

- **Engine: restic**, an optional external dependency (Arch: `extra/restic`, 0.19.x at time of
  writing). Not a Python dependency — called via `subprocess`, like `age`/`gpg` already are.
- **Missing restic ⇒ old behaviour + notice** that it can be enabled.
- **Existing archives must be migratable** (the author's own case).
- **Password: random, stored age-encrypted**, decrypted at run time with the user's age identity
  (default `~/.config/age/key.txt`, overridable with the existing `--identity`). The only secret to
  keep off-machine stays the age private key. (Builder's call recorded by the author: "todo bien".)
- **Retention defaults: `daily: 7, weekly: 4, monthly: 12, yearly: 2`**, per-config override via a
  `retention:` block, applied automatically after each `--compress`, manual `--prune` with
  `--dry-run`. The newest snapshot is always kept. Same policy in tar mode (deleting surplus
  `<timestamp>/` directories).

## Design

### Representation: an uncompressed tar stream into `restic backup --stdin`

Each config snapshot is stored as **the same tar the tool already builds, but uncompressed**, piped to
`restic backup --stdin --stdin-filename <cfg>.tar --tag config-saver --tag cfg:<cfg> --time <ts>`.

Why (author's call delegated to the designer, recorded here):

- `TarCompressor` already owns everything that defines a backup's content — path/content
  normalisation, `exclude`, missing-input reporting, the `.config-saver-metadata.json` member,
  `only_root_user`. Feeding restic the tar reuses all of it with no second implementation.
- An uncompressed tar is chunk-friendly (512-byte aligned, file contents verbatim), so restic's
  content-defined chunking deduplicates unchanged files across snapshots even when offsets shift.
- Restore/export become `restic dump <snapshot> <cfg>.tar` → the existing decompressor / gzip+encrypt
  path, so the external formats do not change.

`TarCompressor` therefore needs an output mode "uncompressed tar to a writable stream" alongside the
current "gzip to an atomic file". The `restic` process's stdin is that stream. `CompressResult` stays
the per-config result; its `output_path` for restic mode is a stable identifier of the snapshot
(e.g. `restic:<short-id>`), and the snapshot id comes from `restic backup --json` output.

The builder must measure dedup on a real tree (e.g. two consecutive `claude-conversations` tars) before
committing to this; if the measured second-snapshot growth is not a small fraction of the first, stop
and report — the fallback idea is to back up a staging tree instead, which is a design change to bring
back to the author.

### Store layout

- Repository: `<saves_dir>/restic/` (`saves_dir` as resolved today, normally
  `~/.config/config-saver`), mode `0700`. One repo for all configs, so data shared between configs
  (`claude` and `claude-conversations`) is also stored once.
- Password: `<saves_dir>/restic-password.age` (`0600`), the random password encrypted to the age
  recipient given at init. Passed to restic via `RESTIC_PASSWORD_COMMAND` or a pipe — never on argv,
  never written decrypted to disk.
- Description (`--description`) goes into the snapshot as a tag-safe value or, better, restic's
  snapshot description if 0.19 supports one; otherwise a `desc:` tag with an encoding the builder
  picks. `--list` must keep showing it.
- Per-config identity in restic: tag `cfg:<cfgname>` (cfgname = config file stem, as today).

### Enabling: init command

`config-saver --restic-init --encrypt-to <age-recipient> [--identity <path>]`

- Fails clearly if `restic` or `age` is missing.
- Generates a random password (`secrets`), writes it age-encrypted, runs `restic init`.
- Refuses to overwrite an existing repo or password file.
- Tells the user the age private key is now the only thing needed to read the store, and that it must
  be kept off-machine.

Mode selection per run: restic mode iff `restic` is on `PATH` **and** the repo + password file exist.
Restic installed but store not initialised ⇒ tar mode + notice pointing at `--restic-init`. Restic
not installed ⇒ tar mode + notice pointing at installing restic. One notice per run, not per config,
on stderr, and suppressible is not required.

The config's own `encrypt:` block keeps meaning "encrypt exports/tar-mode archives of this config";
in restic mode the store is always encrypted regardless.

### Migration

`config-saver --migrate [--identity <path>] [--delete-migrated]`

- Requires restic mode to be enabled.
- For every archive found by `BackupManager.list_archives()` (per-config tree and legacy top level),
  oldest first: decrypt if `.age`/`.gpg` (needs the identity; gpg uses the agent as today), gunzip,
  pipe the tar into `restic backup --stdin` with `--time` = the archive's timestamp (from its
  `<timestamp>` directory name, else file mtime) and its `description.txt` if any.
- Idempotent: an archive already migrated (snapshot with matching `cfg:` tag and time exists) is
  skipped, so an interrupted migration can be re-run.
- Without `--delete-migrated` nothing is deleted. With it, an old archive is deleted only after its
  snapshot is confirmed present (and `restic check` passes once at the end — builder decides whether
  per-archive verification is `restic check --read-data-subset` or comparing `restic dump` size/hash
  against the source tar; the requirement is: verified before delete).
- Prints a summary: archives migrated / skipped / failed, bytes before, repo size after.

### Retention / prune

Model: optional `retention:` block on `Model`, `extra="forbid"`, all fields non-negative ints:
`daily`, `weekly`, `monthly`, `yearly` (defaults 7/4/12/2). `0` disables that bucket. Same semantics
as restic's `--keep-*` (the newest snapshot of each day/week/month/year), so both modes agree.

- **Restic mode**: per config `restic forget --tag cfg:<name> --keep-last 1 --keep-daily … …`, then
  one `restic prune` per run (prune is repo-wide and the expensive part).
- **Tar mode**: compute the keep set from the `<timestamp>/` directory names with the same bucket
  rules (pure function, unit- and property-tested), always include the newest, delete the rest.
  Legacy top-level archives (not in a `<timestamp>/` dir) are never pruned.
- **When automatic prune is active (author's decision after the builder check):** in restic mode,
  always — the defaults apply to configs without a `retention:` block. In tar mode, **only for configs
  whose YAML has an explicit `retention:` block**; without one, nothing is pruned. This keeps the
  first timer run after an upgrade from deleting un-migrated history, and changes nothing for users
  who never opt in.
- Automatic after every `--compress` (directory/batch mode — the timer path), for the configs that run
  produced. Single-file `--compress --output` is not pruned.
- Manual: `config-saver --prune [--input …] [--dry-run]` — prints what would be/was removed per config.
- A prune failure is reported (non-zero exit consistent with the existing exit-code table) but never
  undoes or hides a successful compress.
- The keep-set logic is pure decision logic: it belongs in the mutation-tested tier.

### Existing verbs in restic mode

- `--list`: snapshots per config (date + description), plus any not-yet-migrated tar archives.
- `--export-config NAME` / `--export-all-configs`: `restic dump` the newest snapshot → gzip → encrypt
  per the config's `encrypt:` (if the config file is still found; otherwise plain `.tar.gz` with a
  warning) → the same output locations as today.
- `--decompress`: unchanged for a `.tar.gz` input. Restoring directly from the store is via
  `--export-config` then `--decompress`; a direct path is optional, builder's call.

### systemd timer

The units already run as the user, so `~/.config/age/key.txt` is readable; nothing changes. A
root-only config (`only_root_user`) under root gets root's own `saves_dir` and therefore its own
store, which needs its own `--restic-init`; document it, don't automate it.

## Constraints

- Python ≥ the project's current floor; no new Python dependencies. `restic` and `age` are external
  binaries, located like `crypto.require_binary` does today.
- Existing tests keep passing; CLI flags and exit codes are a public contract (README is the contract —
  update it in the same change, plus CHANGELOG).
- Gates: pytest + coverage ≥ 80%, ruff, mypy, mutation tier for the pure retention logic.
- Tests must not need restic installed — mock the subprocess boundary — except one integration test
  marked/skipped when `restic` is absent, which runs a real init → backup ×2 → forget → dump round
  trip in `tmp_path` and asserts the second backup added little data.

## Designer's calls after the builder check (author may override)

- **Store configured but unusable** (repo + password file exist, but `restic`/`age`/identity missing or
  failing) ⇒ **error**, per-config, non-zero exit — never a fallback. Mode selection keys on "the
  store exists"; the "install restic" notice only appears when no store exists.
- **Legacy top-level archives** (`config-saver-<ts>.tar.gz`, no config name) migrate with tag
  `cfg:legacy` and are **never pruned automatically** (only by an explicit `--prune --input`-less
  manual run that names `legacy`, builder's syntax).
- **Single-file `--compress`**: with `--output` it still writes the requested `.tar.gz` (the user
  named a file); without `--output`, in restic mode it goes into the store tagged with the config
  stem. Neither triggers automatic prune.
- **`--encrypt-to`/`--encrypt-method` on `--compress` in restic mode** ⇒ rejected with the usage
  exit code (6) and a message saying the store is always encrypted and recipients apply to exports.
  Accepted consequence, documented: a config whose `encrypt:` recipient is *not* the local key becomes
  readable with the local key once in the store (the author uses one key everywhere).
- **Orphaned configs** (snapshots whose config file is gone) are never pruned automatically; a manual
  `--prune` applies the defaults to them and lists them.

## Left to the builder

- Module layout (e.g. a `storage/` package with a tar backend and a restic backend behind one small
  interface — a second implementation exists, so the abstraction is justified).
- How the description is stored in restic, the exact snapshot-id format in `CompressResult`, and the
  migration verification method (within the "verified before delete" requirement).
- Wording of notices.

## After it works: dasik-personal-config

In `/home/andres/repos/dasik-personal-config`:

- Add `restic` to `common/packages.json` (next to `config-saver`).
- Add `retention:` blocks to the YAMLs under `common/home/andres/.config/config-saver/configs.d/`
  (and `torre-amd/…/kvm-switch.yaml`) only where a config should differ from the defaults; the
  defaults otherwise apply. Proposed: `claude-conversations` and `wallpapers` (the big ones) keep the
  defaults; no overrides unless the builder finds a reason.
- Document the one-time manual steps (`--restic-init`, `--migrate --delete-migrated`) in that repo's
  README; they are not automated by dasik.
- Release chain: new config-saver version → `config-saver-aur` PKGBUILD (add `restic` to
  `optdepends`) → dasik-personal-config. Follows the project's existing release procedure.
