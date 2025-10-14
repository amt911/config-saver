
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

Compress a directory or file:

```sh
config-saver --compress folder/ archive.tar.gz
# With progress bar
config-saver --compress folder/ archive.tar.gz --progress
```

### Decompression

Decompress a tar.gz archive:

```sh
config-saver --decompress archive.tar.gz --output destination_folder/
# With progress bar
config-saver --decompress archive.tar.gz --output destination_folder/ --progress
```

## Main CLI Options

- `--compress` / `-c`: Compress file/directory.
- `--decompress` / `-d`: Decompress tar.gz archive.
- `--output` / `-o`: Output directory for decompression.
- `--progress` / `-P`: Show progress bar.
- `--validate`: Validate configuration file.
- `--convert`: Convert between YAML and JSON.

## Project Structure

```
config-saver/
├── __main__.py           # CLI entry point
├── lib/
│   ├── json_parser/      # JSON parser and validator
│   ├── models/           # Pydantic models
│   └── tar_compressor/   # Tar compressor and decompressor
├── configs/
│   └── default/
│       └── config.yaml   # Example configuration
├── dependencies/         # Requirements
└── README.md
```

## Example YAML Configuration

```yaml
# configs/default/config.yaml
config:
	name: example
	version: 1.0
	files:
		- path: /path/file1
			type: json
		- path: /path/file2
			type: yaml
```

## Credits

Developed by amt911. Inspired by best practices for CLI and configuration management in Python.