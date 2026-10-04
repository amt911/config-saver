# Agentic PR verification

> Moved verbatim out of `AGENTS.md` on 2026-10-04 so that file fits the 32 KiB Codex reads
> by default. Its rules still bind: `AGENTS.md` lists the hard ones inline and says when to
> read this file. Edit the rule here, not a copy of it.

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
  [Design principles](design-principles.md#design-principles--solid-applied-with-judgement): a new violation (business
  logic importing `tarfile`/`os` directly instead of going through the existing module, one more
  branch in a growing `if`/`elif` chain) or a new speculative abstraction. Findings, not a veto —
  like the rest of the pass.
- **Hard limits.** The verdict awaits your close; the agent never merges.
