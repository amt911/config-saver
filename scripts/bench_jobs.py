#!/usr/bin/env python3
"""Benchmark `--jobs`: how much parallel batch compression actually buys.

Run it inside the memory cgroup:

    systemd-run --user --scope --quiet -p MemoryHigh=5G -p MemoryMax=6G \
        -p MemorySwapMax=0 -- python scripts/bench_jobs.py

Each configuration produces an independent archive, so the work parallelises
across processes; gzip is CPU-bound, which is why this is a process pool and not
threads. The numbers this prints are what `docs/BENCHMARKS.md` records.
"""

from __future__ import annotations

import os
import shutil
import statistics
import tempfile
import time
from pathlib import Path

import yaml

from config_saver.lib.backup_manager.backup_manager import BackupManager

REPEATS = 3


def build_corpus(root: Path, *, configs: int, files_per_config: int, file_kb: int) -> Path:
    """One directory of data plus one config per archive."""
    data_root = root / "data"
    config_dir = root / "configs"
    config_dir.mkdir(parents=True)

    # Compressible text, which is what a real config tree mostly is.
    body = ("setting = value  # a line of configuration\n" * ((file_kb * 1024) // 42))[: file_kb * 1024]
    for index in range(configs):
        directory = data_root / f"app{index}"
        directory.mkdir(parents=True)
        for file_index in range(files_per_config):
            (directory / f"file{file_index}.conf").write_text(body, encoding="utf-8")
        (config_dir / f"app{index}.yaml").write_text(
            yaml.safe_dump({"directories": [str(directory)]}), encoding="utf-8"
        )
    return config_dir


def run_case(label: str, *, configs: int, files_per_config: int, file_kb: int, job_counts: list[int]) -> None:
    root = Path(tempfile.mkdtemp(prefix="config-saver-jobs-"))
    try:
        config_dir = build_corpus(root, configs=configs, files_per_config=files_per_config, file_kb=file_kb)
        payload_mb = configs * files_per_config * file_kb / 1024
        print(f"\n{label}  ({configs} configs x {files_per_config} files x {file_kb} KB = {payload_mb:.0f} MB)")

        baseline = None
        for jobs in job_counts:
            timings = []
            for repeat in range(REPEATS):
                saves = root / f"saves-{jobs}-{repeat}"
                manager = BackupManager(str(saves))
                start = time.perf_counter()
                batch = manager.compress_directory_of_configs(str(config_dir), "20260101-000000", jobs=jobs)
                timings.append(time.perf_counter() - start)
                assert len(batch.created) == configs, batch.failures
                shutil.rmtree(saves, ignore_errors=True)

            median = statistics.median(timings)
            baseline = baseline if baseline is not None else median
            print(f"  --jobs {jobs:<5} {median:6.2f} s   speedup x{baseline / median:.2f}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main() -> None:
    cpus = os.cpu_count() or 1
    print(f"parallel batch compression, median of {REPEATS} runs, {cpus} CPUs")
    job_counts = [1, 2, 4, 8, min(16, cpus), cpus]
    job_counts = sorted(set(job_counts))

    run_case("many small configs", configs=12, files_per_config=40, file_kb=16, job_counts=job_counts)
    run_case("few large configs", configs=4, files_per_config=20, file_kb=1024, job_counts=job_counts)
    run_case("one config (no parallelism available)", configs=1, files_per_config=200, file_kb=64, job_counts=[1, 4])


if __name__ == "__main__":
    main()
