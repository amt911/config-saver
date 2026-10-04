# CI & git hooks

> Moved verbatim out of `AGENTS.md` on 2026-10-04 so that file fits the 32 KiB Codex reads
> by default. Its rules still bind: `AGENTS.md` lists the hard ones inline and says when to
> read this file. Edit the rule here, not a copy of it.

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
