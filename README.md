
# Config Saver

Python CLI tool for compressing and decompressing directories or files by using configuration files (YAML/JSON), with optional progress bar.

## Main Features

- Validate YAML and JSON files using Pydantic models.
- Compress files and directories into `.tar.gz` archives.
- Decompress `.tar.gz` archives, preserving the original structure.
- Optional progress bar for compression/decompression (`--progress`/`-P`).
- Robust error handling and clear messages.

## Installation

### Production dependencies

Install the package and its main dependencies:

```sh
pip install .
```

### Development dependencies

Install the package along with development tools (type checking, linters, etc.):

```sh
pip install '.[dev]'
```

This will install `mypy` and type stubs.


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

Compress configuration to custom location:

```sh
config-saver --compress --output archive.tar.gz
# With progress bar
config-saver --progress --compress --output archive.tar.gz
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

Decompress a tar.gz archive:

```sh
config-saver --decompress archive.tar.gz
# With progress bar
config-saver --progress --decompress archive.tar.gz
```

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
`--input`/`-i INPUT`: Input YAML config (for compress) or tar file (for decompress)
`--output`/`-o OUTPUT`: Output tar file (for compress), extraction directory (for decompress), or destination directory (for export-all-configs)
- `--progress`/`-P`: Show progress bar during compression/decompression
- `--version`/`-v`: Show program version and exit

- `--description`/`-m DESCRIPTION`: Optional short description to save alongside a created archive. When provided, the CLI will create a per-config timestamp directory and store both the `.tar.gz` and a `description.txt` file inside:

```text
~/.config/config-saver/configs/<cfgname>/<timestamp>/
  <cfgname>-<timestamp>.tar.gz
  description.txt  # contains the provided description (UTF-8)
```

If no `--description` is given, archives are stored in the original (backwards-compatible) locations.

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
- Reject execution with a clear error message if run by a non-root user
- Allow compression/decompression only when executed with `sudo` or as root

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

- The templated service sets `User=%i` and `HOME=/home/%i` so outputs are written to `/home/alice/.config/config-saver` by default.
- If the target user has a non-standard home directory, adjust the `Environment=HOME=...` and `WorkingDirectory=` lines in the installed unit.
- For virtualenv usage, change `ExecStart` to the absolute python path in the venv.

## Credits

Developed by amt911. Inspired by best practices for CLI and configuration management in Python.
