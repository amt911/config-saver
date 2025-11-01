
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

- `-h`/`--help`: Show this help message and exit
- `--compress`/`-c`: Compress files/directories from YAML config
- `--decompress`/`-d`: Decompress a tar file
- `--list`/`-l`: List saved config-saver tar.gz files
- `--input`/`-i INPUT`: Input YAML config (for compress) or tar file (for decompress)
- `--output`/`-o OUTPUT`  Output tar file (for compress) or extraction directory (for decompress, optional)
- `--progress`/`-P`: Show progress bar during compression/decompression
- `--version`/`-v`: Show program version and exit

- `--description`/`-m DESCRIPTION`: Optional short description to save alongside a created archive. When provided, the CLI will create a per-config timestamp directory and store both the `.tar.gz` and a `description.txt` file inside:

```text
~/.config/config-saver/configs/<cfgname>/<timestamp>/
  <cfgname>-<timestamp>.tar.gz
  description.txt  # contains the provided description (UTF-8)
```

If no `--description` is given, archives are stored in the original (backwards-compatible) locations.

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

## Credits

Developed by amt911. Inspired by best practices for CLI and configuration management in Python.