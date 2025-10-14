
# Config Saver

Python CLI tool for validating, converting, and managing configuration files (YAML/JSON), as well as compressing/decompressing directories or files with structure preservation and visual feedback.

## Main Features

- Validate YAML and JSON files using Pydantic models.
- Compress files and directories into `.tar.gz` archives.
- Decompress `.tar.gz` archives, preserving the original structure.
- Optional progress bar for compression/decompression (`--progress`/`-P`).
- Robust error handling and clear messages.

## Installation

1. Clone the repository:

	 ```sh
	 git clone https://github.com/amt911/config-saver.git
	 cd config-saver
	 ```

2. Install dependencies:

	 ```sh
	 pip install -r dependencies/requirements.txt
	 ```

## Usage

### Compression

Compress a directory or file:

```sh
python -m config-saver --compress folder/ archive.tar.gz
# With progress bar
python -m config-saver --compress folder/ archive.tar.gz --progress
```

### Decompression

Decompress a tar.gz archive:

```sh
python -m config-saver --decompress archive.tar.gz --output destination_folder/
# With progress bar
python -m config-saver --decompress archive.tar.gz --output destination_folder/ --progress
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