# config-saver — Claude Guide

Python CLI that compresses/decompresses directories and files driven by YAML/JSON config files,
with Pydantic validation and an optional progress bar. Installable as a package and shipped as an
AUR package (see the sibling **`config-saver-aur`** repo) with systemd timer units for periodic
backups.

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
  dispatch at most 1 agent at a time, and never use a model above Sonnet (no Opus).
- **"modo desatendido"** (unattended mode) — the user is away and delegates autonomy: work
  without waiting for confirmations and decide yourself instead of asking. You MAY **`git push`
  the feature branches you create** and **open PRs via `gh`**. The hard limits still hold:
  **never merge anything** (no `git merge`, no fast-forward, no `gh pr merge`), **never push to
  `main`**/protected, never `--force`. Deliver branches + PRs for the user to merge. Reverts to
  defaults on **"normal mode"**.

Confirm the switch briefly when it happens.

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
- `config_saver/lib/backup_mapager/` — backup orchestration. (Dir is literally spelled
  `backup_mapager` — a typo baked into the import path; don't rename without fixing imports.)
- `configs/*.yaml` — example config files.

## Commands

```bash
pip install .            # install
pip install '.[dev]'     # + mypy and type stubs
python -m config_saver   # run the CLI
mypy config_saver        # type check
```

## Working rules

- **Config is validated with Pydantic** — add new config shapes as models; don't parse ad-hoc.
- **Keep `--progress` optional** — the tool must run headless (systemd timer) without a TTY.
- **Type-clean** — `mypy` must pass; the dev extra installs the stubs.
- **Round-trip integrity** — compress → decompress must reproduce the original tree exactly.
- **AUR packaging lives in `config-saver-aur`** — bump it when releasing.

## Git & GitHub

- **Commits and branches OK** — create commits and new branches whenever it makes sense, without
  asking first.
- **Never push** (default) — no `git push` under any circumstance, and never `git push --force` /
  `--force-with-lease`. Leave pushing to the user. **Exception:** with **"modo desatendido"**
  active, you may push the feature branches you create (never `main`/protected, never force).
- **Never merge — no permission** — no `git merge`, no fast-forward integration, no `gh pr merge`,
  and no merging of any pull request, in every mode incl. **"modo desatendido"**. Leave every
  merge to the user.
- **GitHub via `gh`** — open PRs, issues, comments, and labels over branches already pushed.
