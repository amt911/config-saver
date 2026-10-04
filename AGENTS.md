# config-saver — Agent Guide

Python CLI that compresses/decompresses directories and files driven by YAML/JSON config files,
with Pydantic validation and an optional progress bar. Installable as a package and shipped as an
AUR package (see the sibling **`config-saver-aur`** repo) with systemd timer units for periodic
backups.

## Start here

- **Run `/graphify` before each session.** The persistent graph at `graphify-out/graph.json`
  summarizes architecture, dependencies and cross-cutting concepts without re-reading the repo.
- **Read `docs/FINDINGS.md` before debugging or touching the build** — non-obvious gotchas.
  **Convention:** when you discover something non-obvious that cost time and isn't deducible from the
  code, add a short entry to `docs/FINDINGS.md`.
- **`README.md` is the user-facing contract** — CLI flags, path/content normalization, path-variable
  expansion, `only_root_user`, systemd units. When you change behaviour, update it in the same change.
- **This tool writes to arbitrary filesystem locations and archives secrets.** Restoring extracts to
  absolute paths and the default config includes `~/.ssh` and `~/.config/rclone`. Treat any change to
  `tar_compressor/` or `configs/` as security-relevant; see the open security issues before touching
  extraction.
- **`pytest` is the gate** (`tests/`, ~85% coverage, CI fails under 80%). "It type-checks" is not
  evidence: run `pytest`, and for compress/restore changes also run the CLI against a scratch config
  and inspect the resulting archive/tree.

## ⚡ graphify — use every session

```text
/graphify            # first run (builds graph from scratch)
/graphify --update   # incremental update (only re-extracts changed files)
/graphify query "<question>"    # architecture questions instead of opening multiple files
/graphify explain "<name>"      # locate a concept or symbol
/graphify path "A" "B"          # dependency path between two modules
```

Outputs in `graphify-out/`: `graph.json` (source of truth), `GRAPH_REPORT.md` (god nodes,
communities, surprising connections), `graph.html` (interactive view).

Run `/graphify --update` at end of session if you touched docs (code changes rebuild via hook if
installed).

## Superpowers — use whenever applicable

Always prefer **superpowers** skills over ad-hoc approaches. If there's even a small chance a
skill applies, invoke it via the `Skill` tool before acting (including before clarifying
questions).

- **Process skills first** — `brainstorming` before creative/feature work, `systematic-debugging`
  before fixing bugs, `test-driven-development` before writing implementation.
- **Then implementation skills** — domain-specific skills guide execution.
- **Verify before claiming done** — `verification-before-completion` / `requesting-code-review`.

User instructions always take precedence over skills; skills override default behavior.

### Mode switch

- **"lite mode"** — fully disables superpowers: no skill is invoked, not even the applicability
  check, until **"normal mode"** is said.
- **"normal mode"** (default) — standard superpowers behavior, plus: when delegating coding work,
  dispatch at most 1 **implementation** agent at a time (a read-only review agent runs alongside it — see **Agent orchestration**), and never use a model above Sonnet (no Opus).
- **"modo desatendido"** (unattended mode) — the user is away and delegates autonomy: work
  without waiting for confirmations and decide yourself instead of asking. You MAY **`git push`
  the feature branches you create** and **open PRs via `gh`**. The hard limits still hold:
  **never merge anything** (no `git merge`, no fast-forward, no `gh pr merge`), **never push to
  `main`**/protected, never `--force`. Deliver branches + PRs for the user to merge. Reverts to
  defaults on **"normal mode"**.
  **Pace in this mode** (2026-10-04): intermediate tasks run only the tests of what they touched
  (`pytest tests/test_<module>.py`); commits pile up locally and the branch is pushed **once, at the
  end** — the push that runs the full `pre-push` hook (pytest + mutation gate), preceded by the final
  full-suite pass. Each intermediate push paid the whole hook (minutes) to report nothing the next
  one would not.

Confirm the switch briefly when it happens.

## Rules by topic — what always binds, and where the detail lives

This file fits in the 32 KiB Codex reads by default (`wc -c AGENTS.md` ≤ 32768; when it grows, move
detail to `docs/agents/`, never raise the limit). The detail of each topic was moved verbatim to
`docs/agents/` on 2026-10-04. **The lines below bind even if you never open the document; open it
before working on that topic.** A rule is edited in its document, not here and there at once —
except for its one-line summary in this list.

- **Quality beyond coverage** →
  [docs/agents/quality-beyond-coverage.md](docs/agents/quality-beyond-coverage.md). Hypothesis
  property tests first; mutation gate scored from the `mutmut run` tally (timeouts are not kills);
  every archive member name is untrusted input; a smoke of the built wheel is mandatory; the AI never
  defines the acceptance criteria.
- **Real-environment verification** →
  [docs/agents/real-environment-verification.md](docs/agents/real-environment-verification.md). What
  no in-process test can prove (installed wheel, real filesystem, real systemd) gets a committed
  script; every new check is seen failing once; never assert on a count you cannot predict; a test
  never touches the real system.
- **CI & git hooks** → [docs/agents/ci-and-hooks.md](docs/agents/ci-and-hooks.md). Pre-push runs
  pytest, then the mutation gate; CI stays lean; `--no-verify` only in an emergency, and you own the
  breakage.
- **Agentic PR verification (mandatory)** → [docs/agents/pr-verification.md](docs/agents/pr-verification.md).
  Every PR gets the verdict of a smoke of the affected CLI path as a PR comment; it never merges.
- **Debugging** → [docs/agents/debugging.md](docs/agents/debugging.md), before chasing a bug. Measure
  before ablating; a review finding is not a reproduction; environment claims get measured or they
  don't get made.
- **Agent orchestration** → [docs/agents/agent-orchestration.md](docs/agents/agent-orchestration.md).
  Review in parallel with the next implementation; one shared facts file; plans carry contracts, not
  literal code; discretionary decisions batched; review is never cut.
- **Design principles (SOLID)** → [docs/agents/design-principles.md](docs/agents/design-principles.md).
  No abstraction without a second implementation, an IO boundary or a test seam.
- **Codex and Claude Code** → [docs/agents/agent-compatibility.md](docs/agents/agent-compatibility.md).
  Rules are edited in `AGENTS.md` (or its `docs/agents/` document), never in `CLAUDE.md`.

## 🧠 Heavy jobs run inside a memory cgroup (MANDATORY)

**No exceptions:** any long or parallel job started here — the full test suite, coverage, mutation
testing, a production build, a compress/restore run over a big tree, anything that spawns workers —
runs under a kernel-enforced memory ceiling:

```bash
systemd-run --user --scope --quiet -p MemoryHigh=5G -p MemoryMax=6G -p MemorySwapMax=0 -- <command>
```

**6 GB is the standing ceiling on this machine** (raised from 4 GB by the user on 2026-08-11); don't
exceed it without being told to. `MemoryHigh` throttles and reclaims, `MemoryMax` is the hard stop,
`MemorySwapMax=0` keeps the job from thrashing swap instead of respecting either. Verify it is
actually in force rather than assuming:
`systemctl --user show <scope> -p MemoryMax -p MemoryHigh -p MemoryCurrent`.

**Cap the tool too — but never *instead* of the cgroup.** Pass the tool's own concurrency limit
(`--concurrency`, `--maxWorkers`, `workers`, `-n` for `pytest-xdist`) so the job isn't throttled to a
crawl by the ceiling. A tool's default concurrency is not a budget, and an estimate of per-worker RSS
is not a ceiling. Only the cgroup is.

**Why this is a rule and not advice:** a mutation-testing run on this 24-core box sized its worker
pool from the core count and spawned **23 workers at ~2.3 GB each** — ~50 GB of demand on 31 GB of
RAM. It took the whole machine down hard enough that the user had to power-cycle it; `systemd-oomd`
did not save it. The run before that was wasted too: with the machine starving, **139 of the first
142 mutants "timed out"**, and a timeout is scored as *killed*, so the result came out inflated by
starvation and meant nothing. A job that OOMs the box doesn't merely fail — it also hands you
numbers you'd trust by mistake.

## Stack

- **Python** — packaged via `pyproject.toml` (`pip install .`). Dev extras (`.[dev]`) add
  **mypy** + type stubs.
- **Pydantic** — validates the YAML/JSON config models.
- **tarfile** — `.tar.gz` compression/decompression preserving the original structure.
- **systemd** units under `contrib/systemd/` (`config-saver@.timer`/`.service`, user + system).

## Layout

- `config_saver/__main__.py` — CLI entry (`python -m config_saver`).
- `config_saver/lib/cli/` — argument parsing (`--progress`/`-P`).
- `config_saver/lib/models/` — Pydantic models (`model.py`, `specific_files_model.py`).
- `config_saver/lib/parser/` — YAML/JSON parser.
- `config_saver/lib/tar_compressor/` — compress / decompress.
- `config_saver/lib/backup_manager/` — backup orchestration (renamed from the `backup_mapager` typo).
- `config_saver/lib/errors.py` — typed exceptions; control flow keys off types, never off messages.
- `configs/*.yaml` — example config files (also installed to `<prefix>/share/config-saver/configs`).
- `tests/` — pytest suite, one file per concern (round-trip, extraction security, CLI exit codes…).

## Commands

```bash
pip install .                  # install
pip install -e '.[dev]'        # + pytest, ruff, mypy, pre-commit and stubs
pre-commit install --hook-type pre-commit --hook-type pre-push
python -m config_saver         # run the CLI
pytest                         # test suite
pytest --cov=config_saver      # with coverage (gate: 80%)
ruff check . && ruff format --check .
mypy config_saver              # type check (--check-untyped-defs is on)
```

Wrap anything long-running in the memory cgroup from the section above — a compress run over a real
home directory is exactly the kind of job that is cheap to underestimate.

## Tests and quality

**Current state:** `tests/` covers round-trip integrity, extraction-security regressions (one per
attack vector), the parser/models, the path expander, the backup manager (including parallel batch
mode), CLI exit codes, the systemd units and the packaging metadata. `mypy` runs with
`--check-untyped-defs`. The rules below describe today, and the bar for anything new.

- **Runner:** `pytest` + `pytest-cov`, declared in `[project.optional-dependencies].dev`.
- **Layout:** `tests/` at the repo root, mirroring `config_saver/lib/` one file per module.
- **Filesystem work uses `tmp_path`** — never the real `$HOME`, never `/etc`, never the user's
  `~/.config/config-saver`. A test that writes outside its `tmp_path` is a bug in the test.
- **Coverage gate: 80%** (statements/branches), critical logic ≥90%. Don't lower it to ship —
  exclude a module in config with a written reason instead.
- **Mutation gate: 93%** sobre la lógica pura (`scripts/mutation-gate.sh`), bloqueante en `pre-push`
  y **advisory** en CI hasta verlo pasar dos veces. El 93 no es un deseo: es el score **medido** en
  CI el 2026-09-03 — **243 mutantes muertos / 18 vivos = 93,1%** — redondeado hacia abajo, sobre el
  suelo de 60 que fija la plantilla. La cobertura no puede ver un test sin asserts; esto
  sí. Es un **trinquete**: el umbral sube con el score real y no baja nunca para dejar pasar un push.
  Si la corrida se hace pesada, se estrecha el **scope** (`SCOPE` en el script), nunca el umbral.
  Detalle de cómo leer un superviviente: `docs/TESTING.md` § 3.

### The invariant that matters most

**compress → decompress must reproduce the original tree exactly.** Same bytes, same modes, same
symlink targets. This is the product's entire promise and it is the first test to write. Cover:
permissions, symlinks, binary files, UTF-8 *and* latin-1 text, empty files, names with spaces and
accents, nested directories.

### Run before declaring done

| Change touches | Run before claiming success |
| --- | --- |
| `tar_compressor/` (compress or decompress) | round-trip test + a manual compress/restore into `tmp_path`, then diff the trees |
| `parser/`, `models/` | parser tests + validate every file under `configs/` |
| `utils/path_expander.py` | expander tests (pure, deterministic — no excuse) |
| `cli/` | CLI-by-subprocess tests, including the **exit code** for the affected path |
| `backup_manager/` | manager tests + `--list` / `--show-configs` / `--export-*` by hand |
| anything ambiguous or large | full suite + install the wheel in a scratch venv and smoke the CLI |

### The pyramid per feature — one end-to-end test per journey, the rest one layer down

**Rule since 2026-10-04**, ported from the Android client, where E2E ate days of agent time. A new
feature gets **one end-to-end test per main journey**: the real command, run as a subprocess against
a real tree in `tmp_path` and a temp `HOME` (compress → look at the archive → restore → diff the
trees, or the `--list` / `--export-*` round trip). Everything else — each parser error, each model
validation, every edge value of the path expander, each exit code's branch — goes one layer down, in
unit tests of the pure functions and modules (`tests/test_parser.py`, `tests/test_path_expander.py`…),
which cost milliseconds and touch no real filesystem outside `tmp_path`.

- **When one more end-to-end test is right:** what no in-process test can answer — the real exit
  code and stderr of the process, the installed entry point or packaging metadata, the systemd
  units, a malicious archive that must be refused when the CLI extracts it, behaviour that depends
  on real file modes, symlinks or ownership. The test says in a comment why it is not a unit test.
- **A bug still gets its failing test first**, at the lowest layer that reproduces it.
- **Existing tests are not migrated for this rule.** It applies to new work and to what a change touches.

### Running the suites — the whole suite once at the end, only the reds in between

- **While working:** only the tests of what you touched — `pytest tests/test_<module>.py`, or
  `pytest -k <expression>`; leave out the slow ones with `pytest -m "not slow"` (the `slow` marker
  is declared in `pyproject.toml`).
- **The full suite runs once, at the end of the branch, alone** — in the background while you write
  the PR, under `timeout --kill-after=60s <limit>` inside the memory cgroup above. Push and PR only
  after it is green.
- **Red pass → only the reds** (`pytest --lf`) until they are green or proven red on the base commit
  too; then **one** full confirmation pass, the one that catches a fix breaking another test.
- **Three reds in a row on one test → stop** and read the evidence (the assertion diff, the
  archive, the tree on disk) before a fourth change.
- **No fixed sleeps** — wait on the state. **Every heavy command** (the full suite, coverage,
  `scripts/mutation-gate.sh`) runs under `timeout --kill-after=60s <limit>`, inside the cgroup.

### What to test per module

| Module | What |
| --- | --- |
| `utils/path_expander.py` | `$HOME`, `$CONFIG_DIR`, `${ENDS_WITH="…"}`, `${BEGINS_WITH="…"}`, unknown variable, no candidate match, several placeholders in one path |
| `parser/parser.py` | invalid YAML, missing required fields, `only_root_user: true` as non-root, `directories` mixing plain strings and `{source, files}` |
| `tar_compressor/tar_compressor.py` | path normalization (including sibling home dirs), text/binary classification, content normalization, root-owned skip |
| `tar_compressor/tar_decompressor.py` | round-trip, **and one regression per malicious-archive vector**: `../` member, absolute member name, symlink escaping the root, hardlink, device node |
| `backup_manager/backup_manager.py` | archive listing, per-config timestamp dirs, description round-trip, XDG fallback on `PermissionError` |
| `cli/cli.py` | every flag, `--version`, and the documented exit codes (2, 3, 4, 5, 6, 7, 10) |

### TDD — required for new logic

1. **Red** — write a failing test that describes the behaviour.
2. **Green** — implement the minimum to pass.
3. **Refactor** — clean up under green tests.

Exceptions: pure docs/comment changes and spikes — but add tests before merging.

### Hard rules (no exceptions)

- **Never claim done without showing test output.** "`mypy` passes" is not "it works".
- **A bug fix needs a failing regression test first**, then the fix (see `systematic-debugging`).
- **A security fix ships with the malicious input as a test** — the archive that escaped, the path
  that traversed. Without it the fix regresses the next refactor.
- **Never delete, `.skip` or `.xfail` a test to get green.** Fix the code or the test on purpose.
- **Never test against the real `$HOME` or `/etc`.** `tmp_path` or it doesn't run.
- **Test over mock** — this tool's whole job is real filesystem behaviour; mocking `tarfile` or `os`
  proves nothing. Build a real tree in `tmp_path` and assert on it.

## Working rules

- **Use superpowers skills whenever they apply** — invoke via `Skill` before acting; process skills
  before implementation skills.
- **New dependencies: ask first, then install** — adding a package is allowed when the task
  genuinely needs one, but ask before installing (which package, why, what it replaces) and wait
  for the go-ahead. Runtime deps are intentionally minimal, so check what is already in
  `pyproject.toml` first. Exception: obvious test dev extras.
- **Heavy or parallel jobs run inside a memory cgroup** — never launch a suite, build or
  compress/restore over a real tree on a bare estimate; wrap it in
  `systemd-run --user --scope -p MemoryHigh=5G -p MemoryMax=6G -p MemorySwapMax=0 -- <command>`
  and cap the tool's own concurrency too.
- **Archive contents are untrusted input** — every member name from a tar must be validated against
  the intended root before it is joined, opened or extracted. This is the tool's sharpest edge.
- **Archives hold secrets** — the default config includes `~/.ssh` and rclone credentials. Anything
  that creates an archive or a saves directory sets restrictive permissions; never widen them.
- **TDD for new logic. Don't merge logic without tests**, and don't lower the coverage gate —
  exclude with a written justification instead.
- **Config is validated with Pydantic** — add new config shapes as models; don't parse ad-hoc.
- **Reuse before you write** — search `config_saver/lib/` before adding a helper, model or exception
  (`rg -n "^(def|class) " config_saver/`). Every concern already owns a package (`cli/`, `models/`,
  `parser/`, `tar_compressor/`, `backup_manager/`, `errors.py`): a new path-validation or archive
  helper extends the one that exists instead of growing a private copy beside its caller, and tests
  reuse the fixtures in `tests/` rather than rebuilding a tree each time. At the third copy, extract
  into the owning package in the same PR, migrating the call sites. A second implementation of the
  extraction guard is a security bug waiting for the fix to land in only one of them.
- **SOLID where it pays, not by rote** — split by reason to change, extend through variants, slots or
  strategies, keep subtypes and variants honest, keep interfaces and props narrow, and push IO
  (network, database, clock, filesystem) behind ports at the edge. No abstraction without a second
  implementation, an IO boundary or a test seam. See
  [Design principles](docs/agents/design-principles.md#design-principles--solid-applied-with-judgement).
- **Keep `--progress` optional** — the tool must run headless (systemd timer) without a TTY.
- **Type-clean** — `mypy` must pass; the dev extra installs the stubs.
- **Round-trip integrity** — compress → decompress must reproduce the original tree exactly.
- **AUR packaging lives in `config-saver-aur`** — bump it when releasing.
- **Instrument before you ablate, budget the lap, and dispatch review in parallel** — a pipeline that completes with non-empty output produced output; more than three reproductions means you owe a shortcut script; a review finding is not a reproduction; and the review of task N runs alongside the implementation of N+1. See **Debugging** and **Agent orchestration** above.

## Git & GitHub

- **Commits and branches OK** — create commits and new branches whenever it makes sense, without
  asking first.
- **Never push** (default) — no `git push` under any circumstance, and never `git push --force` /
  `--force-with-lease`. Leave pushing to the user. **Exception:** with **"modo desatendido"**
  active, you may push the feature branches you create (never `main`/protected, never force).
- **Never merge unless the user asks for it in that conversation** — no `git merge`, no
  fast-forward integration, no `gh pr merge`, and no merging of any pull request. This holds in
  every mode, **"modo desatendido"** included: unattended autonomy covers branches and PRs, never
  merges. Only an explicit "merge this" from the user in the current session lifts it, and it
  covers exactly what they named — not the next PR.
- **GitHub via `gh`** — open PRs, issues, comments, and labels over branches already pushed.
