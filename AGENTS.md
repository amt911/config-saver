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

## Agent compatibility — Codex and Claude Code

This file is `AGENTS.md`: the **one** instruction file for every coding agent in this repo. Codex reads
it directly; Claude Code reads `CLAUDE.md`, which only imports this file (`@AGENTS.md`) and holds what
applies to Claude alone. **Edit rules here, never in `CLAUDE.md`** — two copies of a rule drift apart
on the first edit, and each agent then obeys a different one.

| Concern | Claude Code | Codex |
| --- | --- | --- |
| Instruction file | `CLAUDE.md` → imports `AGENTS.md` | `AGENTS.md` (root down to the working directory) |
| Invoke a skill | `Skill` tool, or `/<skill>` | mention it (`$<skill>`), or let it trigger from its description |
| Skills on disk | `~/.claude/skills` (links into `~/.agents/skills`) | `.agents/skills`, then `~/.agents/skills` |
| superpowers | `superpowers@claude-plugins-official` (`/plugin install`) | `superpowers@openai-curated` (install from `/plugins`; that id is its key in `~/.codex/config.toml`) |
| MCP servers | `claude mcp add -s user <name> -- <cmd>` | `codex mcp add <name> -- <cmd>` (`~/.codex/config.toml`) |
| File size | imports load whole | `project_doc_max_bytes`, **32 KiB by default** — raise it when this file is bigger, or the tail is silently dropped |

- **Install shared skills once, for both agents:** `npx skills add <owner/repo> -g --skill <name>`
  writes to `~/.agents/skills` and links it for Claude Code, so both run the same version.
- **Names in this file are capabilities, not one agent's syntax.** "Invoke the `X` skill" means the
  `Skill` tool in Claude Code and a skill mention in Codex. An MCP server named here is used when it is
  registered for the agent you are running in; its absence never blocks ordinary work.
- **Modes, model caps and Git rules bind both agents.** "lite mode", "normal mode" and "modo
  desatendido" mean the same in Codex; a cap written as "no model above Sonnet" means "no model above
  the mid tier" there.
- **Claude-only commands** (`/graphify` and other slash commands that are not skills) are skipped by
  Codex unless the same capability is installed as a skill in `~/.agents/skills`.

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

Confirm the switch briefly when it happens.

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

## Quality beyond coverage

**Coverage measures how much code runs, not whether it's correct.** This is especially treacherous
with AI: it tends to write the test *and* the code in one move, so if it misread the requirement,
both encode the same mistake and the test passes happily. These gates attack that blind spot.

- **Property-based testing** *(highest priority here)* — **Hypothesis**. This codebase is unusually
  well suited to it: `decompress(compress(tree)) == tree` is a textbook round-trip property, and
  `PathExpander.expand` is a pure function over strings. Let it generate the filenames, encodings and
  nesting nobody thinks of by hand.
- **Mutation testing** — **mutmut**, scoped to the pure logic (path normalization, the expander,
  member validation), not to the I/O shells. A surviving mutant means the code is *covered but not
  verified*. **Now a gate, not advice:** `scripts/mutation-gate.sh` runs the scoped set inside the
  memory cgroup and fails under **60%** (killed / killed+survived — timeouts deliberately do NOT
  count as kills: a starved run once scored 139 of 142 mutants "killed" purely by timing out, which
  reads as a triumph and means nothing). Blocking in `pre-push`, advisory in CI until the baseline
  is measured. **De dónde sale el veredicto, y por qué no de `mutmut results`:** en esta versión
  `mutmut results` lista **solo los supervivientes**, así que leerlo como si fuera el recuento
  completo da "0 muertos / 18 vivos = 0%" sobre una corrida que mató 243. El recuento bueno es el
  marcador que `mutmut run` imprime al terminar (`261/2533 🎉 243 … 🙁 18`), que es lo que parsea el
  script — y si no encuentra marcador, **falla**: una puerta que no puntúa nada no está limpia,
  está rota.
- **Runtime boundary validation** — **Pydantic** is already used for the YAML models; keep every new
  config shape a model. The other boundary is the **archive**, and it is currently unvalidated: every
  member name coming out of a tar is untrusted input and must be checked before use.
- **Strict types + static analysis** — `mypy` with `--check-untyped-defs` (and `--strict` as the
  target), plus **Semgrep** in CI. SAST matters because AI introduces exactly the class of bug this
  repo already has: unvalidated extraction paths and secrets written with loose permissions.
- **Smoke tests** *(mandatory, not a nice-to-have)* — build the wheel, install it into a scratch
  venv, run `--compress` then `--decompress` against a sample config. Code routinely passes every
  unit test while the packaged CLI won't start (a missing package, a bad entry point).
- **Dependency auditing** — `pip-audit` in CI. AI invents non-existent packages ("slopsquatting")
  and pulls vulnerable versions; verify every new dependency actually exists and is the one you
  think it is.
- **Dead-code elimination** — **vulture** (unused code) and **deptry** (unused/undeclared
  dependencies). Pruning dead code shrinks the surface every session has to reason about.

**Process rule (worth more than any tool): don't let the AI define the acceptance criteria.** You
write or review the important test cases yourself — at least the key asserts and the requirement's
edge cases — and have the AI implement against them.

## Real-environment verification — what no in-process test can prove

Some properties are invisible to the entire pytest suite no matter how many tests you add, because
**a monkeypatched `pathlib.Path` is not a filesystem and an installed wheel is not `pip install -e .`**.
This tool's whole job is to move real files on a real machine and to be woken by systemd — and none
of that exists inside the test process. A unit that parses is not a timer that fires; a `tarfile`
call that was made is not an archive that restores.

That layer needs a check that drives the **installed package on a real system** and asserts on what
is externally observable: exit codes, files on disk with their permissions intact, journal lines.

**Write that check as a script, commit it under `scripts/`, and name it here.** It runs by hand with
no arguments, prints a per-phase `PASS`/`FAIL`, and exits non-zero on the first failure. Run it in a
throwaway container or VM, never against your own `~/.config`.

What "real environment" means here, concretely:

- **The installed artifact**, not the source tree: build the wheel, install it the way the AUR
  package does (`python -m installer`), and invoke `config-saver` from `PATH`. An editable install
  hides missing package data, a wrong entry point and files the wheel never included.
- **A real filesystem with awkward contents.** Symlinks (including dangling ones), hardlinks, FIFOs
  and sockets, sparse files, files with no read permission, non-UTF-8 filenames, paths longer than
  255 bytes, and a file that changes size *while* the tar is being written. A mocked `tarfile` sees
  none of these; a backup tool meets all of them.
- **Real systemd.** `systemd-analyze verify contrib/systemd/config-saver@.service`, then actually
  start the unit and the timer and read the journal. A `.service` that parses can still fail on
  `%i` expansion, a missing `WorkingDirectory`, or a `User=` that cannot read the source.
- **A full round trip, compared byte for byte.** Compress a real tree, decompress into an empty
  directory, and `diff -r` the two *including* modes and mtimes. "The function returned without
  raising" is not a restore.

### The names, so you can ask for them by name

| Name | What it means here |
| --- | --- |
| **E2E / on-system acceptance test** | Runs the installed `config-saver` against a real directory tree in a throwaway container and asserts on observable results — exit code, the archive on disk, the restored tree, the journal — never on internals. |
| **Contract test** | Checks that assumptions about a boundary you don't own actually hold. Two matter here: **the config people actually write** (does the Pydantic model accept the YAML in `/etc/config-saver/configs/`, or only the fixtures?) and **the standard library** (`tarfile` extraction filters changed default behaviour in recent CPython — a member path that used to extract now raises, or vice versa; absolute paths and `..` components are handled by the *runtime*, not by you). Pin what you assume and test it against the interpreter you actually ship on. |
| **Mutation testing** (on system: by hand) | Revert the fix, re-run the check, confirm it goes red, restore. `mutmut`/`cosmic-ray` automate this in process; against a real filesystem you do it manually. **A check that has never failed has not been tested** — a restore check that has never seen a corrupt archive proves nothing. |
| **State-invariant test** | Asserts a relationship **between two things** no unit test owns: an archive and the manifest that describes it; a timer's `OnCalendar` and the last-run stamp it writes; a backup and the schema/version of the config that produced it. Each side is individually fine; the pair is what breaks. |
| **Test pollution / isolation leak** | A test writing to real state. For a backup/restore tool this is the dangerous one: a test that restores into the user's actual `~/.config`, or installs a system unit, does damage that no assertion will report. Run destructive paths **only** in a container or VM, and restore anything machine-global in a teardown that runs even when the test fails. |

### Rules that came out of real bugs, not theory

- **Prove every new check can fail before you trust it green.** Revert the fix, re-run, watch it go
  red, restore. Applies to unit tests written after the fact *and* to on-system checks. A green you
  have never seen turn red is not evidence.
- **Never assert on a count you cannot predict.** "Backed up more than 5 files" or "the archive is
  under 2 MB" passes against a genuinely broken build as soon as the machine's config directory
  differs — the magnitude depends on the system, not on the bug. Assert the **invariant**: the
  restored tree equals the source tree (`diff -r`, modes included), a symlink stays a symlink, the
  archive contains no absolute paths, a second run over unchanged input produces an identical result.
- **A manifest, timestamp or version marker must die with the data it describes.** An archive kept
  after its manifest is regenerated, or a last-run stamp that survives a deleted backup, silently
  makes the next restore restore the wrong thing — no crash, no log.
- **Never let a test touch the real system.** No writes outside a temp dir, no `systemctl` against
  the user's session, no restore into a real home. Container or VM for anything destructive.
- **Run the suite the way that actually works on this machine** — `.[dev]` is not always installed,
  so `--cov` may be unavailable until you install it — and run heavy steps under the memory cgroup
  (see *Heavy jobs*):

  ```bash
  systemd-run --user --scope --quiet -p MemoryHigh=5G -p MemoryMax=6G -p MemorySwapMax=0 -- \
    pytest --cov=config_saver
  scripts/verify-<flow>-on-system.sh   # installed wheel, real FS, real systemd, in a container
  ```

## CI & git hooks

**Policy — heavy checks run locally on push, CI stays lean.**

- **Pre-push** runs the full local gate via `pre-commit` (`pytest`, then the **mutation gate at
  60%** — el paso más lento va el último, y no se muta sobre una suite roja), on top of the
  per-commit `ruff check`, `ruff format` and `mypy`. Emergency bypass only via `--no-verify`, and
  then you own the breakage.
- **GitHub Actions** (`.github/workflows/ci.yml`) — only the cheap, important checks:
  - `lint` → `ruff check` + `ruff format --check`.
  - `test` → `mypy` + `pytest --cov --cov-fail-under=80` on a Python 3.10–3.13 matrix.
  - `packaging` → build wheel + sdist, install each into a clean venv, smoke the CLI.
  - `systemd` → `systemd-analyze verify` on the shipped units.
  - `release-consistency` → on a `v*` tag, the tag must equal `project.version`.
  - `sast` → **Semgrep** `p/python`, currently blocking (`--error`). Keep it that way.
  - `mutation` → `scripts/mutation-gate.sh` (93% sobre la lógica pura, medido), **solo en PRs** y con
    `continue-on-error: true` mientras no haya baseline medido. Se promueve a bloqueante cuando el
    score supere el umbral en dos runs seguidos; anota aquí la fecha, o "advisory" será permanente.
  - `audit` → **`pip-audit`**, currently `continue-on-error: true`. Promote it to a blocking gate
    once the findings are triaged, and fix a failure by bumping the dependency, never by relaxing the
    threshold.
- **Git hooks:** `.pre-commit-config.yaml`; install with
  `pre-commit install --hook-type pre-commit --hook-type pre-push`.

## Debugging — keep the loop from running away

What a bug costs is not the fix. It is how many times you go around
`build → deploy → reach the state → observe` before you know what to fix, times what one lap costs.
Every rule below carries the number it came from; the ones this repo has not measured are marked
`<!-- pendiente de medir -->` until someone does.

- **Measure before you ablate.** Ablation costs one lap per hypothesis and answers yes/no;
  instrumentation costs one lap total and answers *what is actually happening*. **Measured: 28
  ablations over 1 h 42 min moved nothing; one 13-min batch of probes changed the question and the
  bug fell on the next round.** The rule that came out of it: **if a pipeline completes every phase
  with non-empty output, the output exists** — stop asking "why doesn't it appear" and ask "where
  does it appear". Here that pipeline is `load/parse → validation → transform → write the result`.
- **Budget the lap, then attack the dominant term.** Time the four phases once and write the real
  seconds in; one dominates and the rest are noise. **If a bug needs more than three reproductions,
  write the shortcut before the fourth** — here that means
  a fixture that lands the tree/state already built, a VM snapshot, or a dev subcommand that skips
  the earlier steps.
  Commit it as `<scripts/repro-<bug>.sh>` and name it in `docs/FINDINGS.md`.

  | Lap phase | Command here | Measured |
  | --- | --- | --- |
  | build / install | `<pip install -e . · none>` | `<n s>` |
  | deploy | `<copy to the VM · none>` | `<n s>` |
  | reach the state | `<sample config · VM at the starting state>` | `<n s>` |
  | observe | `<stdout · resulting files · journalctl>` | `<n s>` |

- **A review finding is not a reproduction.** Whoever reviewed read the code; they did not run it.
  Reproduce it yourself before sending anyone to fix it, and **if the implementer says they cannot
  reproduce it, believe the implementer** — one of them has the thing running. **Measured: 1 h 25 min
  chasing a bug that did not exist.**
- **A test that refuses to go red is data, not a failure.** The fourth failed attempt to pin down
  that non-existent bug is what uncovered the real one, pointing the opposite way. "I cannot make
  this fail" is a result and it gets reported; a green test papered over it throws the signal away.
- **Before demanding a red, ask whether the mechanism can produce one.** If another layer masks the
  effect there will be no red however hard you push, and the time goes into the test instead of the
  bug. **Measured: over 1 h on two structurally impossible reds.**
- **Assertions that are inert by construction** — none of these shows up as a failure, a warning or
  a coverage drop. **Every assertion is watched failing once**, and expected values are written by
  hand:

  | Inert by | What it looks like here |
  | --- | --- |
  | `assert` under `-O` | `python -O` / `PYTHONOPTIMIZE` strips them from the bytecode: the test passes checking nothing |
  | an unawaited coroutine | leaves a `RuntimeWarning: coroutine was never awaited` and the test stays green |
  | snapshots with `--snapshot-update` | `syrupy` / `pytest-snapshot` record the current output as the reference |
  | `MagicMock` | returns another `MagicMock` for any attribute: everything is truthy and everything looks called |
  | expectation computed alike | the expected value is recomputed with the same function under test |

- **Verify the resource limit reaches the process doing the work.** A job wrapped in a memory scope
  can hand the work to a daemon or worker pool living outside it, and the tool still reports the
  limit as applied — over a process that is idle. Check the **worker's** cgroup
  (`cat /proc/<worker-pid>/cgroup`), not the scope's.
- **Environment claims get measured or they don't get made.** "That heap sounds low" produced a
  recommendation that was simply wrong; measuring it — three runs per setting, not one — gave a
  **0.4% difference, below the run-to-run variance**. No performance tuning lands without a
  before/after over more than one run.
- **Locate which layer owns a rule before deciding which side gives.** A rule that lives in one
  layer and isn't shared by the others fails where the assumption breaks, not where it is written,
  which is why the fix keeps landing in the innocent layer.
- **Replacing a component can remove capabilities in silence.** When you swap one API for another,
  enumerate what the old one did that the new one does not, and say it in the PR — nothing will fail
  to compile. An optional parameter that defaults to off is a capability that only exists if the
  caller remembers it.

## Agent orchestration — parallel where it's free, batched where it's yours

Delegating to agents moves the bottleneck to **scheduling**: what waits on what, what each agent
re-derives, and which decisions quietly stop being yours. Same convention — every rule carries its
measured number.

- **Review is not on the critical path.** Reviewing task N and starting N+1 are independent when
  they touch different files. Serialized, review is **10-15% of the wall clock** and blocks
  everything behind it; in parallel it is free. **On receiving an implementation report, dispatch
  its review and the next implementation in the same turn.** This is the one exception to
  *"at most 1 agent at a time"*: the cap counts **implementation** agents — a review agent reads and
  reports, it writes nothing, so it cannot race the implementer. **The exclusive resource here is:**
  the VM or target directory, and any real database or block device
  — at most one agent touching it.
- **Keep one shared facts file.** Every fresh agent re-derives the same things: the real selector,
  which fake exists, what that helper accepts. Keep `docs/FACTS.md`, have each agent append to it
  when it finishes, and hand it to the next one in its dispatch. Only **facts verified against the
  repo or the running system**, with how they were verified. It is not the gotchas log: that holds
  what is *not* deducible from the code and outlives the branch; this holds what is perfectly
  deducible and merely expensive to look up, and it may die with the branch.
- **Plans carry contracts, not literal code.** The agent **trusts** the code in the plan; code you
  never compiled is an error wearing authority. **Measured: 4 wrong blocks, 15-40 min of detour
  each.** Write exact names, exact signatures and "mirror the shape of `<X>`" — claims the agent can
  check against the repo — and reserve literal code for what you have run.
- **Batch the discretionary decisions.** Work that appears along the way — a capability being
  dropped, a missing script, an adjacent bug — added **5-6 h of 15**. Each was justified; deciding
  them on the fly is what takes them away from you. Accumulate and ask **once per batch, with the
  estimated cost**. In **"modo desatendido"** the batch goes in the PR body instead, with its costs.
- **What never gets cut.** Review was **1.5 h of 15** and found a `create()` silently discarding
  fields, a 404 caused by SQL deduplication, a silent merge that corrupted data, a
  delete-and-recreate with no transaction, and several inert assertions. **Cutting review does not
  give time back; it defers it to production.** Cut reproduction (write the shortcut) and
  serialization (dispatch review in parallel) instead.

### Day one — the numbers that fill the blanks

1. **The lap** — time `build → deploy → reach the state → observe` once and write the seconds into
   the table above. The dominant phase gets the shortcut script; the rest stay unoptimized.
2. **The exclusive resource** — confirm the one named above is really the only one.
3. **The inert assertions** — break one assertion on purpose and run the suite; anything still green
   is inert. Then prune the table above to what this stack can actually produce.

## Design principles — SOLID, applied with judgement

SOLID is a list of **symptoms to look for**, not a pattern to apply. Every one of the five exists to
keep a change local: the useful question is *how many files does the next plausible change touch, and
how many of them do you have to understand first?* Applied by rote it produces the opposite — an
interface per class, a factory for one product, an eight-file feature — so here it is bounded by YAGNI
and by **Reuse before you write** (see *Working rules* below).

| Principle | Checkable smell | Usual fix |
| --- | --- | --- |
| **S — Single responsibility**: one reason to change | the description needs "and"; the file changes in PRs about unrelated features; a test mocks things unrelated to what it asserts; a component both fetches and lays out | split along the reason to change — IO, decision, presentation |
| **O — Open/closed**: extend without editing | adding a case edits a growing `switch`/`if` chain in several places; one boolean prop per variant | a variants map, strategy, slot or registry — introduced at the second real case, not the first |
| **L — Liskov substitution**: subtypes keep the contract | an override throws "not supported"; callers check the concrete type before calling; a variant drops the base's disabled, focus or semantics | narrow the base contract, or stop inheriting and compose |
| **I — Interface segregation**: clients see only what they use | a fake implements methods the test never calls; a whole entity is passed to read two fields; a `Service` with fifteen methods | split by client need; pass the fields, not the bag |
| **D — Dependency inversion**: policy does not import mechanism | domain or UI code imports `fetch`, the ORM, Retrofit, `Date.now()` or `fs` directly; a unit test needs a network or a database | depend on a port the caller owns (interface, function, hook); wire the adapter at the edge |

### Where the seams go, per stack

| Stack | Seams |
| --- | --- |
| Python | pure functions for decisions; IO at the edges (CLI entry point, adapters); a `Protocol` only when a second implementation or a test fake needs it |
| Shell | one function per job; side effects (`rm`, package managers, network) isolated in named functions a dry-run flag can skip |

### Where SOLID stops

- **No interface, abstract class or factory without one of:** a second real implementation, an IO
  boundary (network, database, filesystem, clock, randomness, OS), or a test that cannot be written
  without the seam. "We might swap it later" is not on the list.
- **Reuse first beats speculative extension points:** add the parameter to the existing thing before
  inventing a plugin system for it.
- **Speculative abstraction is a review finding**, exactly like a violation: an interface with one
  implementation and no IO behind it gets inlined.
- **Refactor toward SOLID when a change hurts**, in the PR that felt the pain — not as a drive-by
  rewrite of code nobody is changing.
- **Repos without their own executable code** (packaging, fonts, LaTeX, configuration data,
  byte-matching decompilation) state the exemption in one line under *Working rules*.

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
  [Design principles](#design-principles--solid-applied-with-judgement).
- **Keep `--progress` optional** — the tool must run headless (systemd timer) without a TTY.
- **Type-clean** — `mypy` must pass; the dev extra installs the stubs.
- **Round-trip integrity** — compress → decompress must reproduce the original tree exactly.
- **AUR packaging lives in `config-saver-aur`** — bump it when releasing.
- **Instrument before you ablate, budget the lap, and dispatch review in parallel** — a pipeline that completes with non-empty output produced output; more than three reproductions means you owe a shortcut script; a review finding is not a reproduction; and the review of task N runs alongside the implementation of N+1. See **Debugging** and **Agent orchestration** above.

## Agentic PR verification (MANDATORY on every PR)

**Every PR MUST be verified end-to-end before merge, and the verdict MUST be posted as a PR
comment** via `gh pr comment`. A headless agent (`claude -p`, local) builds/installs the CLI and
runs a smoke of the affected path (e.g. `pip install .` then `python -m config_saver --compress
--input configs/<sample>.yaml --output /tmp/out.tar.gz` and a round-trip `--decompress`), then
posts the verdict; it **never merges** — it waits for you. Running the pass and posting the
verdict comment is **not optional**. It catches what the diff and `mypy` miss: a CLI flag that no
longer parses, a config that fails to validate, a broken round-trip.

- **Engine.** CLI (no browser, no service) → build/install the package into a scratch venv and run
  a smoke of the affected command(s) against a sample config under `configs/`, inspecting stdout
  and the resulting archive/output tree.
- **Two layers.** `mypy` (and any tests) stay the hard merge gate; the agentic pass is advisory and
  never vetoes a merge on its own — but running it and posting the verdict comment is mandatory.
- **The verdict reads structure too.** Besides driving the CLI, it names what the diff does to the
  [Design principles](#design-principles--solid-applied-with-judgement): a new violation (business
  logic importing `tarfile`/`os` directly instead of going through the existing module, one more
  branch in a growing `if`/`elif` chain) or a new speculative abstraction. Findings, not a veto —
  like the rest of the pass.
- **Hard limits.** The verdict awaits your close; the agent never merges.

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
