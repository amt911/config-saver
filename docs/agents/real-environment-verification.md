# Real-environment verification

> Moved verbatim out of `AGENTS.md` on 2026-10-04 so that file fits the 32 KiB Codex reads
> by default. Its rules still bind: `AGENTS.md` lists the hard ones inline and says when to
> read this file. Edit the rule here, not a copy of it.

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
