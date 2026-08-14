#!/usr/bin/env python3
"""Benchmark content normalization: streaming pass vs the previous read-it-all one.

Run it inside the memory cgroup:

    systemd-run --user --scope --quiet -p MemoryHigh=5G -p MemoryMax=6G \
        -p MemorySwapMax=0 -- python scripts/bench_normalization.py

The "legacy" implementation below is the code this replaced, kept here so the
comparison is reproducible instead of remembered.
"""

from __future__ import annotations

import os
import shutil
import statistics
import tempfile
import time
import tracemalloc
from pathlib import Path

from config_saver.lib.models.model import Model
from config_saver.lib.tar_compressor.tar_compressor import (
    HOME_CONTENT_PLACEHOLDER,
    TarCompressor,
)

REPEATS = 5


def legacy_normalize(compressor: TarCompressor, file_path: str) -> bytes | None:
    """The pre-streaming implementation: sniff, then read the whole file, decode,
    replace, re-encode — three copies of the content in memory."""
    if os.path.islink(file_path) or not compressor._is_text_file(file_path):
        return None
    try:
        with open(file_path, "rb") as f:
            content = f.read()
    except OSError:
        return None
    for encoding in ("utf-8", "latin-1"):
        try:
            text = content.decode(encoding)
        except UnicodeDecodeError:
            continue
        if compressor.user_home in text:
            return text.replace(compressor.user_home, HOME_CONTENT_PLACEHOLDER).encode(encoding)
        return None
    return None


def build_corpus(root: Path, home: str, *, files: int, size_kb: int, match_ratio: float) -> list[str]:
    """Text files of `size_kb`; `match_ratio` of them mention the home path."""
    root.mkdir(parents=True, exist_ok=True)
    paths: list[str] = []
    line_with = f"cache={home}/.cache/app\n"
    line_without = "cache=/var/cache/app\n"
    for index in range(files):
        line = line_with if index < files * match_ratio else line_without
        body = line * max(1, (size_kb * 1024) // len(line))
        path = root / f"file{index}.conf"
        path.write_text(body, encoding="utf-8")
        paths.append(str(path))
    return paths


def measure(fn, paths: list[str]) -> tuple[float, int]:
    """Return (seconds, peak bytes) for one pass over the corpus."""
    tracemalloc.start()
    start = time.perf_counter()
    for path in paths:
        result = fn(path)
        if result is not None and not isinstance(result, bytes):
            stream, _size = result
            with stream:
                while stream.read(1 << 20):
                    pass
    elapsed = time.perf_counter() - start
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return elapsed, peak


def run_case(label: str, *, files: int, size_kb: int, match_ratio: float) -> None:
    home = os.path.expanduser("~")
    workdir = Path(tempfile.mkdtemp(prefix="config-saver-bench-"))
    try:
        paths = build_corpus(workdir / "corpus", home, files=files, size_kb=size_kb, match_ratio=match_ratio)
        compressor = TarCompressor(Model.model_validate({"directories": [], "normalize_content": True}), "unused")

        legacy_times, legacy_peaks, new_times, new_peaks = [], [], [], []
        for _ in range(REPEATS):
            elapsed, peak = measure(lambda p: legacy_normalize(compressor, p), paths)
            legacy_times.append(elapsed)
            legacy_peaks.append(peak)
            elapsed, peak = measure(compressor._normalized_stream, paths)
            new_times.append(elapsed)
            new_peaks.append(peak)

        print(
            f"{label:<34} "
            f"legacy {statistics.median(legacy_times) * 1000:7.1f} ms / {max(legacy_peaks) / 1e6:7.1f} MB peak   "
            f"streaming {statistics.median(new_times) * 1000:7.1f} ms / {max(new_peaks) / 1e6:7.1f} MB peak"
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def main() -> None:
    print(f"content normalization, median of {REPEATS} runs\n")
    run_case("500 x 4 KB, 50% match", files=500, size_kb=4, match_ratio=0.5)
    run_case("500 x 4 KB, no match", files=500, size_kb=4, match_ratio=0.0)
    run_case("20 x 8 MB, 50% match", files=20, size_kb=8 * 1024, match_ratio=0.5)
    run_case("20 x 8 MB, no match", files=20, size_kb=8 * 1024, match_ratio=0.0)
    run_case("4 x 64 MB, 50% match", files=4, size_kb=64 * 1024, match_ratio=0.5)


if __name__ == "__main__":
    main()
