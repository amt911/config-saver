# Releasing

One version, three places. They drifted once (`pyproject.toml` said `3.0.3` while the newest tag and
the AUR `PKGBUILD` said `3.1.1`), which made `config-saver --version` lie in released builds.

## Steps

1. Update `project.version` in `pyproject.toml` (SemVer).
2. Move the `## [Unreleased]` entries in `CHANGELOG.md` under the new version and date.
3. Commit, then tag with the **bare version**, matching the existing tags
   (`3.0.3`, `3.1.0`, `3.1.1`): `git tag <version> && git push origin <version>`.
   The AUR `PKGBUILD` downloads `/archive/refs/tags/$pkgver.tar.gz`, so a `v`
   prefix would 404 there. The `release-consistency` CI job runs on any tag,
   strips an optional leading `v`, and fails if what is left ≠ `project.version`.
4. In the sibling **`config-saver-aur`** repo: bump `pkgver`, refresh the checksum with
   `updpkgsums` (it can only be computed once the tag exists), regenerate `.SRCINFO` with
   `makepkg --printsrcinfo > .SRCINFO`, and push to the AUR.

### Runtime dependencies that are not Python packages

Archive encryption shells out to `age` or `gnupg`. They are **optional**: without them the tool
works exactly as before, and a config that asks for encryption fails with a message naming the
package. That is why they are `optdepends` in the PKGBUILD and not in `project.dependencies`.

## Notes

- The version is static on purpose: `setuptools_scm` cannot resolve a version from the `.git`-less
  release tarball the AUR package builds from (see `docs/FINDINGS.md`).
- `config_saver.__version__` reads the installed distribution metadata, so it reports `unknown` when
  running from a source tree that was never installed.
- The `packaging` CI job builds the wheel and the sdist and installs both into clean environments;
  a release that cannot be installed fails before the tag does anything.
