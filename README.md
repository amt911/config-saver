
# Config Saver

Python CLI tool for compressing and decompressing directories or files by using configuration files (YAML/JSON), with optional progress bar.

## Main Features

- Validate YAML and JSON files using Pydantic models (unknown keys are rejected, not ignored).
- Compress files and directories into `.tar.gz` archives, written atomically and with mode `0600`.
- Decompress `.tar.gz` archives, preserving the original structure, with containment-checked extraction.
- Optional progress bar for compression/decompression (`--progress`/`-P`).
- Parallel compression of independent configurations (`--jobs`/`-j`).
- Missing inputs are reported, never silently skipped (`--strict` turns them into a non-zero exit).
- Three layered configuration levels ([examples, system policy, yours](#where-configurations-live)),
  merged with the most specific one winning.
- Several configuration directories in one run, so your own configs can live in a
  [private repository](#personal-configurations-from-a-private-repository) instead of `/etc`.
- Optional archive [encryption](#encryption-optional) with `age` or `gpg`.
- Scheduled daily backups that [catch up immediately](#scheduling) after downtime.
- Stable exit codes for scripting (see [Exit codes](#exit-codes)).

> **⚠ A plain `.tar.gz` is compressed, not encrypted.** An archive produced from a config that lists
> `~/.ssh`, `~/.config/rclone` or similar contains those secrets in the clear. config-saver keeps
> its directories `0700` and its archives `0600`, but that protection ends the moment the file is
> copied to cloud storage, a shared drive or another machine.
> Use [encryption](#encryption-optional) for those configs.

## Installation

### Production dependencies

Install the package and its main dependencies:

```sh
pip install .
```

### Development dependencies

Install the package along with development tools (tests, type checking, linters, hooks):

```sh
pip install -e '.[dev]'
pre-commit install --hook-type pre-commit --hook-type pre-push
```

This installs `pytest`, `pytest-cov`, `ruff`, `mypy`, type stubs and `pre-commit`.

Day-to-day commands:

```sh
pytest                      # the behavioural suite
pytest --cov=config_saver   # with coverage (CI gate: 80%)
ruff check . && ruff format --check .
mypy config_saver
```

The hooks run `ruff`, `ruff format` and `mypy` before each commit, and the full test suite before
each push, so activate this environment in the shell you commit and push from.

The suite includes property-based tests (Hypothesis) for the round-trip and path-normalization
invariants. Mutation testing (mutmut) is available for the pure logic and is run by hand, not in CI
— see [docs/TESTING.md](docs/TESTING.md).


### As an Arch Linux package
You can install `config-saver` from the AUR using an AUR helper like `yay`:

```sh
yay -S config-saver
```

This will also install the templated systemd unit and timer files to run periodic backups. If you want to enable them for a specific user, do the following:

```bash
  systemctl enable --now config-saver@user.timer
```

## Usage
### Export all configurations

Exports the latest version of every saved configuration. By default, files are copied to the user's home directory. If you specify `--output`, all files are exported to the given directory.

```sh
config-saver --export-all-configs
config-saver --export-all-configs --output /path/to/destination
```

### Export a specific configuration
Exports the latest archive for a specific configuration name. By default, the file is copied to your home directory. If you specify `--output`, it will be exported to the given path.

```sh
config-saver --export-config myconfig
config-saver --export-config myconfig --output /path/to/destination/myconfig-latest.tar.gz
```

### Compression

- Compress all system configs (default behaviour). This will read YAMLs from `/etc/config-saver/configs` and create per-config archives under `~/.config/config-saver`:

```sh
config-saver --compress
# With progress bar
config-saver --progress --compress
```

Compress **one** configuration to a custom location. `--output` names a single archive, so it
requires a single config file as `--input` (in directory mode config-saver creates one archive per
configuration and `--output` is rejected):

```sh
config-saver --compress --input /etc/config-saver/configs/zsh.yaml --output archive.tar.gz
# With progress bar
config-saver --progress --compress --input /etc/config-saver/configs/zsh.yaml --output archive.tar.gz
```

Directory mode compresses the configurations **in parallel by default** (`--jobs auto`: one worker
per CPU, capped at the number of configurations, since gzip is CPU-bound and each archive is
independent). Measured speedups are 2–3.3× — see [docs/BENCHMARKS.md](docs/BENCHMARKS.md):

```sh
config-saver --compress            # parallel by default
config-saver --compress --jobs 4   # fixed worker count
config-saver --compress --jobs 1   # force sequential
```

`--progress` on its own falls back to sequential, because a per-file progress bar and several
workers writing at once are unreadable together; pass `--jobs N` explicitly to keep both.

Fail the run when a configured path was missing:

```sh
config-saver --compress --strict
```

Compress with a short description. This creates a per-config timestamp directory and a `description.txt` next to the archive:

```sh
config-saver --compress -m "Daily backup before upgrade"
```

Compress a single config file to a specific output path (no description):

```sh
config-saver --compress -i /etc/config-saver/configs/default-config.yaml -o ~/backups/default-config-20251018.tar.gz
```

### Decompression

Decompress a tar.gz archive. Without `--output` the archive is restored to its original absolute
locations (home-relative members land in the *current* user's home); with `--output` everything is
extracted below that directory:

```sh
config-saver --decompress --input archive.tar.gz
config-saver --decompress --input archive.tar.gz --output /tmp/restored
# With progress bar
config-saver --progress --decompress --input archive.tar.gz
```

Extraction refuses any member that would land outside the destination: `..` traversal, absolute
member names, links pointing out of the root, and device/fifo members are rejected with exit code
`5` before anything is written. `setuid`/`setgid` bits are never restored.

### Listing

List saved archives (shows date + description preview):

```sh
config-saver --list
```

#### Examples (Compression)

Compress all system configs (default behaviour). This will read YAMLs from `/etc/config-saver/configs` and create per-config archives under `~/.config/config-saver`:

```sh
config-saver --compress
```


## Main CLI Options

`-h`/`--help`: Show this help message and exit
`--compress`/`-c`: Compress files/directories from YAML config
`--decompress`/`-d`: Decompress a tar file
`--list`/`-l`: List saved config-saver tar.gz files
`--export-config`/`-e NAME`: Export the latest config archive by name
`--export-all-configs`: Export the latest archive for every saved configuration
`--show-configs`: Show available configuration names
`--input`/`-i PATH`: Input YAML/JSON config **or config directory** (for compress) or tar file (for decompress). **Repeatable** when every value is a directory, to combine the system configs with your own. Defaults to `/etc/config-saver/configs`, falling back to the examples shipped with a pip install (`<prefix>/share/config-saver/configs`)
`--output`/`-o OUTPUT`: Output tar file (for compress), extraction directory (for decompress), or destination directory (for export-all-configs)
- `--progress`/`-P`: Show progress bar during compression/decompression
- `--jobs`/`-j N`: Worker count for directory mode. Default `auto` (one per CPU, capped at the number of configurations); `1` forces sequential
- `--strict`: Exit with code 8 when a configured path was missing from the backup
- `--include-system-configs`: Also archive `/etc/config-saver/configs` (off by default)
- `--restore-system-configs`: Restore members landing in `/etc/config-saver/configs` (off by default)
- `--encrypt-to RECIPIENT`: Encrypt the archive for this age public key or gpg key id (repeatable)
- `--encrypt-method {age,gpg}`: Backend for `--encrypt-to` (default `age`)
- `--identity FILE`: Key file used to decrypt an encrypted archive (required for `age`)
- `--version`/`-v`: Show program version and exit

- `--description`/`-m DESCRIPTION`: Optional short description to save alongside a created archive. When provided, the CLI will create a per-config timestamp directory and store both the `.tar.gz` and a `description.txt` file inside:

```text
~/.config/config-saver/configs/<cfgname>/<timestamp>/
  <cfgname>-<timestamp>.tar.gz
  description.txt  # contains the provided description (UTF-8)
```

If no `--description` is given, archives are stored in the original (backwards-compatible) locations.
`--description` and `--output` cannot be combined: the description mode decides the archive location
itself.

## Exit codes

| Code | Meaning |
|-----:|---------|
| `0` | Success |
| `2` | Configuration file, config directory or archive not found |
| `3` | Configuration failed validation (bad shape, unknown key) |
| `4` | Permission denied, including `only_root_user: true` run as non-root |
| `5` | Archive error: corrupt archive, or a member refused as unsafe |
| `6` | Invalid option combination (e.g. `--output` in directory mode, bad `--jobs`) |
| `7` | `--export-config NAME` matched no saved configuration |
| `8` | `--strict` and at least one configured path was missing |
| `9` | Encryption or decryption failed (backend missing, wrong key, bad recipient) |
| `10` | I/O error |

argparse itself exits with `2` for usage errors such as a missing action flag.

## Round-trip semantics

`compress` → `decompress` reproduces:

- file contents, byte for byte (binary, UTF-8 and Latin-1 text alike);
- the directory structure, **including empty directories**;
- symlinks, as symlinks;
- permission bits, minus `setuid`/`setgid`, which are deliberately dropped on restore.

It does **not** preserve ownership, mtimes, ACLs or extended attributes.

Each archive carries a `.config-saver-metadata.json` member recording whether `normalize_content`
was enabled, so a file that legitimately contains the literal `<<<HOME_PLACEHOLDER>>>` string is no
longer rewritten on extraction. Archives created before this metadata existed are still treated as
normalized.

## Storage layout and permissions

- Directories created by config-saver: `0700`.
- Archives and `description.txt`: `0600`, set at creation instead of relying on the umask.
- Archives are written to a temporary file in the destination directory and moved into place with
  `os.replace()` only after a clean close — an interrupted run never leaves a truncated file that
  looks like a valid backup.

## Where configurations live

Three levels, layered the way systemd and `tmpfiles.d` layer their drop-ins:

| Level | Path | Owner |
| --- | --- | --- |
| Examples | `/usr/share/config-saver/configs` (`<prefix>/share/…` for a pip install) | the package |
| System policy | `/etc/config-saver/configs` | the administrator, or a declarative installer |
| Yours | `~/.config/config-saver/configs.d` | you |

Running `config-saver --compress` with no `--input` **merges the two lower levels**, and the more
specific one wins: with `/etc/config-saver/configs/zsh.json` and
`~/.config/config-saver/configs.d/zsh.yaml` present, yours is used and the system one is ignored —
not both. Configurations are matched by name without extension, so a `.yaml` of yours overrides a
`.json` of the system's.

**The examples are never used on their own.** With nothing configured at either active level the
run stops with exit `6` and tells you what to copy:

```console
$ config-saver --compress
No configurations found.
  system policy : /etc/config-saver/configs
  yours         : /home/you/.config/config-saver/configs.d
  examples      : /usr/share/config-saver/configs (never used on their own)
Copy an example to your own directory to get started:
  mkdir -p /home/you/.config/config-saver/configs.d && cp /usr/share/config-saver/configs/zsh.yaml /home/you/.config/config-saver/configs.d/
```

That is deliberate. Falling back to the shipped examples would mean a fresh install quietly backs
up whatever they happen to list — the default example reaches `~/.ssh` and `~/.config/rclone` — on a
daily timer, because a package was installed. Backups of things nobody chose are a surprise with
security weight; one `cp` is not a burden. The examples stay one explicit flag away:
`config-saver --compress --input /usr/share/config-saver/configs`.

Because your level lives inside `$HOME`, any configuration that backs up your home directory also
carries **the configurations themselves**: restoring such an archive on a clean machine brings back
what to back up, not only the data.

`/etc` is different — see [System configurations in archives](#system-configurations-in-archives).

## Personal configurations from a private repository

`--input` is repeatable in directory mode, so a private repository of personal configurations sits
next to the system ones without copying anything:

```sh
git clone git@github.com:you/private-configs.git ~/repos/private-configs
config-saver --compress -i /etc/config-saver/configs -i ~/repos/private-configs/config-saver
```

Every directory contributes its configurations, they are compressed in parallel as usual, and each
still produces its own archive under `~/.config/config-saver/configs/<name>/`. A configuration name
defined in two directories is rejected (exit `6`) rather than silently overwritten — rename one.

Keeping the YAML files in the private repo is the low-friction part: they are the definitions, so a
`git pull` on a new machine is the whole setup. Two ways to make the scheduled run see them:

```sh
# 1. Point the user timer at both directories
systemctl --user edit config-saver.service
# [Service]
# ExecStart=
# ExecStart=/usr/bin/config-saver --compress \
#     --input /etc/config-saver/configs \
#     --input %h/repos/private-configs/config-saver \
#     --description "Automated backup by systemd timer"

# 2. Or symlink them into the system directory, once
sudo ln -s ~/repos/private-configs/config-saver/*.yaml /etc/config-saver/configs/
```

If the private repo also holds *files* you want in the backup (not just definitions), point a
configuration at them like any other path — and remember that a repository working tree includes
`.git`, so list the subdirectories you actually want:

```yaml
directories:
  - source: "$HOME/repos/private-configs"
    files:
      - dotfiles
      - ssh-config
```

Anything sensitive in there deserves [encryption](#encryption-optional); the repository being
private protects the remote, not the archive sitting on your disk.

## System configurations in archives

`/etc/config-saver/configs` is **not** archived by default, and members that would land there are
**not** restored by default either:

```sh
config-saver --compress --include-system-configs      # archive them
config-saver --decompress -i backup.tar.gz --restore-system-configs   # restore them
```

The reason is ownership. On a machine managed by a declarative installer (`dasik` writes that
directory from its own JSON), a restore that overwrote `/etc` would leave the machine differing
from what the installer declares, and the next `plan` would show changes after every restore —
exactly what an idempotent installer must not do. If nothing else manages `/etc` on your machine,
the two flags are there and they are explicit.

Skipped members are reported, never silent:

```console
$ config-saver --decompress -i backup.tar.gz
Skipped 3 system configuration file(s) under /etc/config-saver/configs. Pass
--restore-system-configs to write them; leave it alone if a declarative installer owns that
directory.
```

Two details worth knowing:

- `--include-system-configs` as a normal user hits the root-owned skip: those files belong to
  `root`, so they are reported as skipped unless you run `sudo config-saver --compress
  --include-system-configs`.
- Extraction into a directory (`--output`) is unaffected: nothing there can reach the real `/etc`,
  so the members are extracted normally.

## Encryption (optional)

Archives can be encrypted with **`age`** or **`gpg`**. config-saver does not implement any crypto:
it writes the archive, hands it to the binary you already trust, and deletes the plaintext before
anything appears under the final name. The result is an ordinary `age`/`gpg` file — `age -d
backup.tar.gz.age > backup.tar.gz` gives you exactly the archive you would have had without
encryption.

Per configuration (this is what a systemd timer uses, no flags involved):

```yaml
encrypt:
  method: age            # or gpg
  recipients:
    - age1qz...          # age public key, or a gpg key id / user id
directories:
  - "$HOME/.ssh"
```

Or from the command line, which overrides whatever the config says:

```sh
config-saver --compress -i secrets.yaml --encrypt-to age1qz...
config-saver --compress --encrypt-to me@example.com --encrypt-method gpg
```

The archive gains a `.age` or `.gpg` suffix (`zsh-20260811-120000.tar.gz.age`) and is still listed,
exported and restored like any other:

```sh
config-saver --decompress -i zsh-20260811-120000.tar.gz.age --identity ~/.config/age/key.txt
config-saver --decompress -i zsh-20260811-120000.tar.gz.gpg   # gpg uses its own agent/keyring
```

Notes:

- `age` needs `--identity` to decrypt; `gpg` uses its agent, so `--identity` is optional there.
- Recipients are **public** keys: the machine taking backups never needs the private key.
- The plaintext archive is written to a `0600` temporary file in the destination directory and
  removed as soon as the encrypted file is in place; a failed run leaves neither.
- Encryption failures exit with code `9` and never leave a partial archive.
- What is *not* protected: the file name, its size and its timestamp.

## User-independent path normalization

Config-saver automatically makes your backups portable across different users by normalizing both **file paths** and **file contents**.

### Path normalization

When compressing files from your home directory (e.g., `/home/andres/.fonts`), the tool normalizes the paths by replacing your username with a generic placeholder `home/user/`.

**During compression:**

- `/home/andres/.fonts/myfont.ttf` → stored as `home/user/.fonts/myfont.ttf` in the archive

**During decompression:**

- `home/user/.fonts/myfont.ttf` → extracted to `/home/currentuser/.fonts/myfont.ttf`

### Content normalization

Additionally, config-saver scans **text files** (configuration files, scripts, etc.) and replaces hardcoded home directory paths in their content:

**During compression (user `andres`):**

```text
Original file content:
  cache_location = /home/andres/.cache/myapp
  data_path = /home/andres/Documents/data.db

Stored in archive:
  cache_location = <<<HOME_PLACEHOLDER>>>/.cache/myapp
  data_path = <<<HOME_PLACEHOLDER>>>/Documents/data.db
```

**During decompression (user `maria`):**

```text
Extracted file content:
  cache_location = /home/maria/.cache/myapp
  data_path = /home/maria/Documents/data.db
```

This means:

- You can create a backup as user `andres`
- Share the `.tar.gz` file with another user (e.g., `maria`)
- When `maria` decompresses it, files go to `/home/maria/` automatically
- **Config files with hardcoded paths** are automatically updated to reference `/home/maria/`

**Note:**

- Files outside the home directory (e.g., `/etc/`, `/opt/`) are stored with their absolute paths and will be restored to the same locations.
- Binary files are preserved as-is; only text files have their content normalized.
- The placeholder `<<<HOME_PLACEHOLDER>>>` is used internally and is automatically replaced during extraction.

## Path variable expansion

You can use variables in your YAML paths, for example:

```yaml
directories:
  - "$CONFIG_DIR/.fonts"
  - source: "$HOME/Downloads"
    files:
      - WSDL.pdf
      - WSDL-1.pdf
```

When processing the YAML, these variables are automatically expanded:

- `$HOME` → `/home/youruser`
- `$CONFIG_DIR` → `/home/youruser/.config`
- `$SHARE_DIR` → `/home/youruser/.local/share`
- `$BIN_DIR` → `/home/youruser/.local/bin`

For example, the entry:

```yaml
directories:
  - "$CONFIG_DIR/.fonts"
```

Will be expanded to:

```text
/home/youruser/.config/.fonts
```

You can also use advanced placeholders:

- `${ENDS_WITH=".default-release"}` to find folders ending with that text.
- `${BEGINS_WITH="prefix"}` to find folders starting with that text.

Matching is **deterministic**: candidates are sorted, so directory order on disk never changes the
result, and when several entries match, the first sorted one wins. A placeholder that matches
nothing is left unexpanded and reported as a missing input (and fails the run under `--strict`)
instead of disappearing silently.

Example:

```yaml
directories:
  - "$HOME/.mozilla/firefox/${ENDS_WITH='.default-release'}"
```

Will be expanded to:

```text
/home/youruser/.mozilla/firefox/abcd1234.default-release
```

Configuration files must go to ```/etc/config-saver/configs/```, by default there is a sample config at ```/etc/config-saver/configs/default-config.yaml```, which you can modify, delete or rename it.

An example YAML configuration file:

```yaml
directories:
    - /home/andres/.fonts
    - source: /home/andres/Downloads
      files:
        - WSDL.pdf
        - WSDL-1.pdf
```

### Root-only configurations (optional)

Some configurations may require root privileges to read system files (e.g., `/etc`, `/var/log`, system service configurations). You can restrict a YAML configuration to only run as root by adding the `only_root_user: true` option:

```yaml
only_root_user: true
directories:
    - /etc/systemd/system
    - /etc/nginx
    - /var/log/apache2
```

**When enabled**, the tool will:

- Check if the current user is root (`uid == 0`) before processing
- Reject execution with a clear error message (exit code `4`) if run by a non-root user
- Allow the **compression** of that configuration only when executed with `sudo` or as root

The option gates *compression*, which is what reads the configuration file. `--decompress` takes an
archive, not a config, so `only_root_user` does not apply to it: restoring files into root-owned
locations succeeds or fails on the filesystem permissions of the process doing it.

**Important behaviors:**

1. **Root can execute any configuration**: The root user can always execute any YAML configuration, regardless of whether `only_root_user` is set to `true` or `false`.

2. **Non-root users skip root-owned files**: When `only_root_user: false` (or not set) and a non-root user executes the configuration, any files owned by root (uid=0 or gid=0) will be automatically skipped during compression. This prevents permission errors when backing up mixed-ownership directories.

   **A warning will be displayed at the end** if any root-owned files were skipped, showing:
   - The total number of skipped files
   - Suggestions on how to include them (set `only_root_user: true` or change ownership)
   - A list of the skipped files (up to 10 files shown)

3. **Batch processing skips root-only configs**: When processing a directory with multiple YAML files (e.g., `/etc/config-saver/configs`), if a non-root user runs the command, any YAML with `only_root_user: true` will be skipped with a warning, and processing will continue with the remaining configs. At the end, a summary shows which configs were skipped and suggests running with `sudo` to process them.

**Example:**

```bash
# As a non-root user, this will fail
config-saver --compress --input /etc/config-saver/configs/system-root-only.yaml

# Run with sudo to succeed
sudo config-saver --compress --input /etc/config-saver/configs/system-root-only.yaml

# As a non-root user with a normal config containing some root-owned files
# The root-owned files will be automatically skipped with a warning
config-saver --compress --input ~/.config/my-config.yaml
# Output during compression (if --progress is used):
# "Skipping root-owned file (only_root_user=false): /some/root/file"
#
# Output at the end:
# ⚠ Warning: 3 root-owned file(s) were skipped because 'only_root_user' is not set to true.
#   To include these files, either:
#   1. Set 'only_root_user: true' in your YAML config and run with sudo
#   2. Change ownership of the files to your user
#
#   Skipped files:
#     - /home/user/.config/some-root-file.conf
#     - /home/user/.local/share/root-owned-data
#     - /home/user/.cache/elevated-cache
```

**Notes:**

- This option is **disabled by default** (`only_root_user: false`)
- Use this for system-level backups that require elevated privileges
- Regular user configs should not use this option
- Root-owned files are identified by checking if `uid == 0` or `gid == 0`

### Content normalization (optional)

By default, config-saver only normalizes **file paths** in the archive (e.g., `/home/andres/.fonts` → `home/user/.fonts`).

If you want to also normalize **file contents** (replace hardcoded home paths inside text files), add the `normalize_content: true` option to your YAML:

```yaml
normalize_content: true
directories:
    - source: "$SHARE_DIR"
      files:
        - konsole  # Will normalize bookmarks.xml and other text files inside
```

**When enabled**, the tool will:

- Scan text files (config files, XML, scripts, etc.) for paths containing your home directory
- Replace them with a placeholder (`<<<HOME_PLACEHOLDER>>>`) during compression
- Restore them to the current user's home during decompression

**Example:**

With `normalize_content: true`, a file like `~/.local/share/konsole/bookmarks.xml`:

```xml
<bookmark href="file:///home/andres/Downloads" >
  <title>Downloads</title>
</bookmark>
```

Will be stored as:

```xml
<bookmark href="file://<<<HOME_PLACEHOLDER>>>/Downloads" >
  <title>Downloads</title>
</bookmark>
```

And when user `maria` decompresses it, it becomes:

```xml
<bookmark href="file:///home/maria/Downloads" >
  <title>Downloads</title>
</bookmark>
```

**Notes:**

- Binary files (images, fonts, executables) are never modified
- Only UTF-8 and Latin-1 encoded text files are processed
- This option is **disabled by default** for safety

## Systemd units and timers

This repository contains example systemd unit and timer files to run `config-saver` periodically.

Files included in `contrib/systemd/`:

- `config-saver.service` - a oneshot service that executes the compression of all configs using the system-wide YAML directory (`/etc/config-saver/configs`).
- `config-saver.timer` - a user-level timer that triggers the service daily at 03:00 with a randomized delay.
- `config-saver@.service` - templated system-wide service. When instantiated as `config-saver@alice.service` it will run as user `alice`, so archives are written to that user's home.
- `config-saver@.timer` - templated system timer that triggers `config-saver@<user>.service` on schedule.

### Scheduling

Both timers run **daily at 03:00**, and a backup the machine slept through runs **as soon as the
timer starts again** — not at the next 03:00:

```ini
OnCalendar=*-*-* 03:00:00
Persistent=true        # catch up a missed run
RandomizedDelaySec=0   # ...without spreading it over ten minutes
AccuracySec=1s         # ...and without the default one-minute coalescing slack
```

Check what is actually scheduled:

```sh
systemctl list-timers 'config-saver*' --all     # system template
systemctl --user list-timers 'config-saver*'    # user unit
```

**An empty `NEXT` column means the timer will never fire again.** That is the signature of the
pre-3.2.0 unit, which used `OnActiveSec=3h`: it fires once, three hours after activation, and then
nothing. Upgrade, reload, and confirm `NEXT` is populated:

```sh
sudo systemctl daemon-reload          # or `systemctl --user daemon-reload`
systemctl list-timers 'config-saver*' --all
```

The user timer only fires while your user manager is running. If you want it to run on a machine
you are not logged into, enable lingering: `loginctl enable-linger $USER`. The system template does
not need this — it runs from the system manager as `User=%i`.

### Install (user-level)

1. Copy the files to your user systemd unit directory:

   ```bash
     ~/.config/systemd/user/
   ```

2. Reload user systemd units:

   ```bash
     systemctl --user daemon-reload
   ```

3. Enable and start the timer (it will activate the service on schedule):

   ```bash
     systemctl --user enable --now config-saver.timer
   ```

4. Check the timer and last run:

   ```bash
     systemctl --user list-timers --all
     journalctl --user -u config-saver.service --since "1 hour ago"
   ```

### Install (system-wide)

If you prefer to run the timer as a system service (e.g., root-managed), copy the files to `/etc/systemd/system/` and use `systemctl daemon-reload` and `systemctl enable --now config-saver.timer` as root.

### Templated system-wide timers (per-user)

The repository also includes templated units that allow a root-managed timer to run the job as a specific non-root user. This is useful if a sysadmin wants to schedule backups for a given account while preserving that user's $HOME as the saves dir.

1. Copy the templated units to `/etc/systemd/system/`:

   ```bash
     sudo cp contrib/systemd/system/config-saver@.service /etc/systemd/system/
     sudo cp contrib/systemd/system/config-saver@.timer /etc/systemd/system/
   ```

2. Reload systemd and enable the timer for user `alice` (example):

   ```bash
     sudo systemctl daemon-reload
     sudo systemctl enable --now config-saver@alice.timer
   ```

3. Check timer and service logs:

  ```bash
    sudo systemctl status config-saver@alice.timer
    sudo journalctl -u config-saver@alice.service
  ```

Notes:

- The templated service sets `User=%i` and lets systemd derive `HOME`/`USER` from the user record,
  so outputs land in that user's `~/.config/config-saver` even with a non-standard home directory.
- The user units are plain (non-templated) units: they set no `User=`, no `HOME=`, and install into
  `default.target`.
- Both timers use `OnCalendar=*-*-* 03:00:00` with `Persistent=true`, i.e. daily at 03:00, and a
  backup the machine slept through runs **as soon as the timer starts again**, not at the next
  03:00. `RandomizedDelaySec=0` and `AccuracySec=1s` keep that catch-up immediate; raise the former
  if you instantiate the system template for many users and would rather spread the load.
  (They previously used `OnActiveSec=3h`, which fires **once**, 3 h after activation, and never
  again — check `systemctl list-timers 'config-saver*'`: an empty `NEXT` column means you are still
  running that version.)
- Both services set `UMask=0077` so scheduled runs never create world-readable archives.
- For virtualenv usage, change `ExecStart` to the absolute python path in the venv.
- To make the scheduled run also pick up configurations kept outside `/etc/config-saver/configs`
  (a private repository, for instance), override `ExecStart` with several `--input` values — see
  [Personal configurations from a private repository](#personal-configurations-from-a-private-repository).

## Credits

Developed by amt911. Inspired by best practices for CLI and configuration management in Python.
