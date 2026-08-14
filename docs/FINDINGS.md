# Findings

Non-obvious things that cost time and are not deducible from the code. Add an entry when you hit
one; keep each entry short and dated.

## 2026-08-11 — `os.makedirs(mode=…)` only applies to the leaf directory

Since Python 3.7, the `mode` argument of `os.makedirs()` is ignored for the intermediate
directories it creates. `os.makedirs("<saves>/configs/<name>/<ts>", mode=0o700)` therefore left
`configs/` and `<name>/` at the umask default while only the timestamp directory was private.
`backup_manager._ensure_private_dir()` creates each missing component itself with `os.mkdir(mode)`
for this reason — do not "simplify" it back into a single `makedirs`.

## 2026-08-11 — Python's tar extraction filters do not protect a manual `open()`

`tarfile.extract(..., filter="data")` (3.12+, backported to 3.10.12+) only guards members that go
through `TarFile.extract`. The decompressor writes regular files itself (`open(dest, "wb")`) so it
could bypass every stdlib guard — which is exactly how the traversal in issue #14 worked. The
containment checks in `TarDecompressor._validate_member()` are the real protection; keep them in
front of every write path, including symlink and hardlink creation.

## 2026-08-11 — `[tool.setuptools.data-files]` is kebab-case in pyproject.toml

The `pyproject.toml` spelling is `data-files` (the `setup.py` keyword is `data_files`). With the
underscore, setuptools silently ignores the section and the example configs never ship. Files land
in `<sys.prefix>/share/config-saver/configs`, which is what `cli.default_config_dir()` looks for
after `/etc/config-saver/configs`.

## 2026-08-11 — setuptools_scm was removed on purpose

The AUR package builds from a release tarball with no `.git`, where setuptools_scm cannot resolve a
version. The single source of truth is `project.version` in `pyproject.toml`; the CI job
`release-consistency` fails a `v*` tag whose name does not match it. See `docs/RELEASING.md`.

## 2026-08-11 — `_is_text_file()` gates the latin-1 fallback

Content normalization has a latin-1 fallback, but the classifier used to reject anything that did
not decode as UTF-8, so the fallback was unreachable. The classifier now accepts latin-1-decodable
data (null bytes and known binary extensions still mean "binary"). Changing one without the other
silently changes which files get their contents rewritten.

## 2026-08-11 — Archives written before the metadata member exist in the wild

`.config-saver-metadata.json` records whether `normalize_content` was on. Archives without it are
assumed to be normalized, which preserves the historical behaviour. Do not flip that default; a
restore of an old archive would then leave `<<<HOME_PLACEHOLDER>>>` in place.

## 2026-08-11 — The pre-push hook uses the interpreter on `PATH`

The `pytest` hook is `language: system` (the suite needs the project and its dependencies
installed). It resolves `python` from the shell you push from, so `pip install -e '.[dev]'` must be
active there; otherwise the hook fails with "Executable `python` not found" style errors rather
than a test failure.

## 2026-08-14 — Batch workers can see a stale environment on Python 3.14

Python 3.14 made `forkserver` the default multiprocessing start method on Linux
(it was `fork` up to 3.13). A forkserver process is created once and its
environment is frozen at that moment, so workers do **not** see a later change
to `$HOME`. A test that points `$HOME` at a temporary directory and then runs
batch mode in parallel gets workers still resolving `~` to the *previous*
test's home — the archive comes out containing another test's files, which
looks like data corruption and is not.

Production is unaffected: the environment of a CLI run does not change while it
runs. Forcing `spawn` would make it uniform but costs an interpreter startup per
worker, and measured *slower than sequential* for many small configurations
(×0.44 on the 12-config corpus in `docs/BENCHMARKS.md`), so the interpreter
default stays. Tests that move `$HOME` run batch mode with `--jobs 1`.
