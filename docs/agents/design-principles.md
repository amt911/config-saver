# Design principles — SOLID, applied with judgement

> Moved verbatim out of `AGENTS.md` on 2026-10-04 so that file fits the 32 KiB Codex reads
> by default. Its rules still bind: `AGENTS.md` lists the hard ones inline and says when to
> read this file. Edit the rule here, not a copy of it.

## Design principles — SOLID, applied with judgement

SOLID is a list of **symptoms to look for**, not a pattern to apply. Every one of the five exists to
keep a change local: the useful question is *how many files does the next plausible change touch, and
how many of them do you have to understand first?* Applied by rote it produces the opposite — an
interface per class, a factory for one product, an eight-file feature — so here it is bounded by YAGNI
and by **Reuse before you write** (see [Working rules](../../AGENTS.md#working-rules)).

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
