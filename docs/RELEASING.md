# Releasing

One version, three places. They drifted once (`pyproject.toml` said `3.0.3` while the newest tag and
the AUR `PKGBUILD` said `3.1.1`), which made `config-saver --version` lie in released builds.

## Steps

1. Update `project.version` in `pyproject.toml` (SemVer).
2. Move the `## [Unreleased]` entries in `CHANGELOG.md` under the new version and date.
3. Commit, then tag: `git tag v<version> && git push --tags`.
   The `release-consistency` CI job fails the tag if `v<version>` ≠ `project.version`.
4. Bump `pkgver` (and the checksum) in the sibling **`config-saver-aur`** repo's `PKGBUILD`.

## Notes

- The version is static on purpose: `setuptools_scm` cannot resolve a version from the `.git`-less
  release tarball the AUR package builds from (see `docs/FINDINGS.md`).
- `config_saver.__version__` reads the installed distribution metadata, so it reports `unknown` when
  running from a source tree that was never installed.
- The `packaging` CI job builds the wheel and the sdist and installs both into clean environments;
  a release that cannot be installed fails before the tag does anything.
