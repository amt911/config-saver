# Benchmarks

Numbers, not opinions. Both scripts live in `scripts/` and are meant to be
re-run after any change to the compression path — always inside the memory
cgroup:

```sh
systemd-run --user --scope --quiet -p MemoryHigh=5G -p MemoryMax=6G \
  -p MemorySwapMax=0 -- python scripts/bench_jobs.py
systemd-run --user --scope --quiet -p MemoryHigh=5G -p MemoryMax=6G \
  -p MemorySwapMax=0 -- python scripts/bench_normalization.py
```

**Machine used for the numbers below:** 24-core x86-64, NVMe SSD, Python 3.14,
`tmpfs`-free (everything on the real filesystem). **Not measured: spinning
disks.** On an HDD the I/O, not gzip, is likely the limit, so parallel workers
may help less or hurt; if you run this on rotating storage, add the numbers here.

## `--jobs`: parallel batch compression

`scripts/bench_jobs.py`, median of 3 runs.

| Corpus | `--jobs 1` | 2 | 4 | 8 | 16 | 24 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 12 configs × 40 × 16 KB (8 MB) | 0.08 s | ×1.45 | ×2.23 | **×2.67** | ×2.60 | ×2.62 |
| 4 configs × 20 × 1 MB (80 MB) | 0.31 s | ×1.85 | **×3.30** | ×3.31 | ×3.29 | ×3.26 |
| 1 config × 200 × 64 KB (12 MB) | 0.07 s | — | ×0.89 | — | — | — |

What the numbers say:

- **Parallelism pays whenever there is more than one configuration** — 2–3.3×,
  which is the whole point of the batch mode.
- **It stops paying past the number of configurations.** Four configs cannot use
  more than four workers; 8, 16 and 24 are identical to 4.
- **A pool for a single configuration is a 11% loss.** Process startup is not
  free and there is nothing to overlap.

Hence the shipped behaviour: `--jobs auto` (one worker per CPU) is the
**default**, and the worker count is **capped at the number of configurations**,
so the single-config case never pays for a pool. `--jobs 1` forces the old
sequential behaviour; `--progress` without an explicit `--jobs` also falls back
to sequential, because a per-file progress bar and several workers writing at
once are unreadable together.

A single `.tar.gz` is a sequential stream, so this is parallelism *across*
archives only. Speeding up one archive would mean a different backend
(e.g. `pigz`) and is not attempted here.

## Content normalization

`scripts/bench_normalization.py`, median of 5 runs. "legacy" is the previous
implementation (sniff, read the whole file, decode, replace, re-encode), kept in
the script so the comparison stays reproducible.

| Corpus | legacy time | legacy peak | streaming time | streaming peak |
| --- | ---: | ---: | ---: | ---: |
| 500 × 4 KB, half match | 15.8 ms | 0.1 MB | 18.2 ms | 0.1 MB |
| 500 × 4 KB, none match | 14.7 ms | 0.1 MB | 16.6 ms | 0.1 MB |
| 20 × 8 MB, half match | 287 ms | 50.3 MB | 285 ms | **9.3 MB** |
| 20 × 8 MB, none match | 98 ms | 16.8 MB | 95 ms | **9.0 MB** |
| 4 × 64 MB, half match | 862 ms | 402.7 MB | **522 ms** | **9.3 MB** |

- **Memory is the real win:** peak drops from ~6× the file size to a constant
  ~9 MB, because the content is scanned in chunks and only spills to a temporary
  file past 8 MB. Normalizing a 64 MB file used to allocate 400 MB.
- **Large files also get faster** (−40% at 64 MB): no full decode/re-encode of
  the whole file, just a bytes-level replacement.
- **Small files pay ~4 µs each** (about +12% on a 500-file corpus of 4 KB
  files). That is the cost of the chunk loop and the byte-level replacement
  bookkeeping; it is worth it for a backup tool that must not fall over on a
  large file.
- When nothing matches — the common case — the file is **not** copied at all:
  `_normalized_stream` returns `None` and the file is handed to `tar.add`.
