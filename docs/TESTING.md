# Testing

Three layers, in increasing cost. The first two run in CI; the third is a local
tool you reach for when you change the pure logic.

```sh
pip install -e '.[dev]'
```

## 1. The suite

```sh
pytest                                     # everything
pytest --cov=config_saver --cov-report=term-missing   # CI gate: 90%
```

Layout, one file per concern:

| File | Covers |
| --- | --- |
| `test_roundtrip.py` | the product's core claim: compress → decompress reproduces the tree |
| `test_extraction_security.py` | one regression per malicious-archive vector |
| `test_compressor.py` / `test_backup_manager.py` | archiving, missing inputs, permissions, batch mode |
| `test_parser.py` / `test_path_expander.py` | configuration validation and path expansion |
| `test_cli.py` | every action flag and every documented exit code |
| `test_edge_cases.py` | error paths: progress bars, unreadable files, broken metadata, failed writes |
| `test_properties.py` | property-based tests (below) |
| `test_systemd_units.py` / `test_packaging.py` | the shipped units and the packaging metadata |

Rules that are not negotiable:

- **Never touch the real `$HOME` or `/etc`.** The `fake_home` fixture points
  `$HOME` at `tmp_path`; a test that writes outside it is a bug in the test.
- **Test over mock.** This tool's job is real filesystem behaviour; a mocked
  `tarfile` proves nothing. Build a tree and assert on it.
- **A security fix ships with the malicious input as a test.**

## 2. Property-based tests (Hypothesis)

`tests/test_properties.py` states the invariants and lets Hypothesis look for
counter-examples with filenames, encodings and nesting nobody writes by hand:

```text
decompress(compress(tree)) == tree          # contents, structure, modes
denormalize(normalize(path)) == path        # inside and outside the home
expand(expand(p)) == expand(p)              # the expander is pure
```

Useful flags while working on them:

```sh
pytest tests/test_properties.py --hypothesis-show-statistics
pytest tests/test_properties.py --hypothesis-seed=<n>     # reproduce a failure
```

A shrunk counter-example is a bug report: copy it into the regular suite as a
plain regression test, then fix the code.

## 3. Mutation testing (mutmut)

Coverage says how much code ran, not whether it is verified. Mutation testing
answers the second question: it changes the code and checks whether a test
notices.

**Always run it inside the memory cgroup, with the child count capped.** An
uncapped run once sized its worker pool from the core count and took the whole
machine down; worse, the starved run scored 139 of 142 mutants as "killed"
because they timed out, which reads as a great result and means nothing.

```sh
systemd-run --user --scope --quiet \
  -p MemoryHigh=5G -p MemoryMax=6G -p MemorySwapMax=0 -- \
  mutmut run --max-children 4 \
    'config_saver.lib.utils.path_expander.*' \
    'config_saver.lib.tar_compressor.tar_compressor.xǁTarCompressorǁ_normalize_path*' \
    'config_saver.lib.tar_compressor.tar_decompressor.xǁTarDecompressorǁ_validate*'

mutmut results                       # what survived
mutmut show '<mutant name>'          # the exact diff that survived
```

Scope it to the **pure logic** — path normalization, the expander, member
validation, content classification. Mutating the I/O shells mostly produces
timeouts, and a timeout counts as a kill, so the number it gives you is
flattering and useless.

### How to read a survivor

A surviving mutant is code that is covered but not verified. Three outcomes:

1. **A missing test** — the usual case. Write it. Real examples from this
   repo: renaming the `$BIN_DIR` variable and widening the "more than one
   candidate" check both survived until the tests below existed.
2. **An equivalent mutant** — the change cannot alter behaviour. Leave the code
   alone and note why.
3. **Dead code** — nothing depends on it. Delete it.

Mutation testing is not a CI gate: it is slow, and its number is only meaningful
when a human reads the survivors.

### Where this repo stands

The last run over the pure logic (expander, path normalization, member
validation, content classification) killed **431 of 460 mutants (93.7%)** and
leaves **29 survivors**, all reviewed and all equivalent for this project's
contract:

| Survivor class | Why it is left alone |
| --- | --- |
| Error *message* text (`"XX…XX"`, upper-cased strings) | Messages are presentation. Control flow keys off the exception **type**, and the typed attributes (`member_name`) are asserted where they matter. |
| `member.ischr() or member.isblk() or member.isfifo() or member.isdev()` with `and` swapped in | `isdev()` already covers all three types, so the disjunction cannot change the outcome. |
| `read(8192)` → `read(8193)`, `content[:8192]` → `[:8193]` | A one-byte change to a sniffing window that has no semantic boundary there. |
| `rstrip(os.sep)` → `rstrip(None)`, `lstrip(os.sep)` → `lstrip(None)` | Differs only for paths made of whitespace, which the checks above already reject. |
| Backslash handling (`"\\"` mutated) | Windows separators; irrelevant on the only platform this tool supports. |
| `"utf-8"` → `"UTF-8"` | Python codec lookup is case-insensitive. |

Everything else that survived a previous run became a test. The ones worth
naming, because they were real gaps:

- Every `$VARIABLE` in `PathExpander` except `$HOME`, `$CONFIG_DIR` and
  `$SHARE_DIR` could be renamed without a test noticing.
- The "more than one candidate" check could be widened to "at least one" or
  narrowed to "more than two" — the ambiguity report was not pinned.
- `$HOME` resolved through the environment, so the expander's own `HOME` entry
  was never exercised (a systemd service without `HOME` set is exactly that case).
- Every entry of the binary-extension list except `.png` was unverified.
- `_is_within(path, "/")` — the shortcut that makes restore-in-place possible —
  could return `False` and no test failed.
- Two rejection paths (`parent directory escapes…`, `resolves outside…`) had no
  test at all.
