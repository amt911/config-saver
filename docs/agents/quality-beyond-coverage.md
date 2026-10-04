# Quality beyond coverage

> Moved verbatim out of `AGENTS.md` on 2026-10-04 so that file fits the 32 KiB Codex reads
> by default. Its rules still bind: `AGENTS.md` lists the hard ones inline and says when to
> read this file. Edit the rule here, not a copy of it.

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
