
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

## Credits

Developed by amt911. Inspired by best practices for CLI and configuration management in Python.
