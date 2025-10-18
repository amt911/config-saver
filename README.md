
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

Compress configuration to default location:

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

### Decompression

Decompress a tar.gz archive:

```sh
config-saver --decompress archive.tar.gz
# With progress bar
config-saver --progress --decompress archive.tar.gz
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

## Example YAML Configuration

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