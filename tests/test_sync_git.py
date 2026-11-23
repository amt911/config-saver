import os
import subprocess

import pytest

from config_saver.lib.errors import SyncNotSetUpError, SyncTransportError
from config_saver.lib.sync.git_repo import GitRepo


@pytest.fixture(autouse=True)
def isolated_git(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)


def git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def remote(tmp_path):
    url = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(url)], check=True, capture_output=True)
    return str(url)


def seeded(tmp_path, remote):
    a = GitRepo(str(tmp_path / "a"))
    a.init(remote)
    (tmp_path / "a" / "base.txt").write_text("base\n")
    assert a.commit_all()
    assert a.push()
    return a


def test_two_clones_diverge_then_merge(tmp_path, remote):
    a = seeded(tmp_path, remote)
    b = GitRepo(str(tmp_path / "b"))
    b.clone(remote)
    (tmp_path / "a" / "a.txt").write_text("a")
    a.commit_all()
    (tmp_path / "b" / "b.txt").write_text("b")
    b.commit_all()
    assert a.push() is True
    assert b.push() is False
    b.pull()
    assert b.push() is True
    assert (tmp_path / "b" / "a.txt").exists()


def test_conflict_aborts_merge(tmp_path, remote):
    a = seeded(tmp_path, remote)
    b = GitRepo(str(tmp_path / "b"))
    b.clone(remote)
    (tmp_path / "a" / "base.txt").write_text("from a\n")
    a.commit_all()
    a.push()
    (tmp_path / "b" / "base.txt").write_text("from b\n")
    b.commit_all()
    with pytest.raises(SyncTransportError):
        b.pull()
    assert not os.path.exists(tmp_path / "b" / ".git" / "MERGE_HEAD")
    assert (tmp_path / "b" / "base.txt").read_text() == "from b\n"


def test_commit_all_nothing_to_commit(tmp_path, remote):
    a = seeded(tmp_path, remote)
    assert a.commit_all() is False


def test_init_gitignore_and_staging_not_committed(tmp_path, remote):
    a = GitRepo(str(tmp_path / "a"))
    a.init(remote)
    lines = (tmp_path / "a" / ".gitignore").read_text().splitlines()
    assert lines == ["restic/locks/", "restic-store.json", ".staging/", ".lock"]
    (tmp_path / "a" / ".staging").mkdir()
    (tmp_path / "a" / ".staging" / "secret").write_text("plaintext")
    (tmp_path / "a" / "ok.txt").write_text("x")
    a.commit_all()
    assert git(tmp_path / "a", "ls-files").split() == [".gitignore", "ok.txt"]
    assert git(tmp_path / "a", "branch", "--show-current").strip() == "main"
    assert git(tmp_path / "a", "remote", "get-url", "origin").strip() == remote


def test_pull_on_empty_remote_is_noop(tmp_path, remote):
    a = GitRepo(str(tmp_path / "a"))
    a.init(remote)
    a.pull()


def test_commits_use_fixed_identity_and_message(tmp_path, remote, monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Leaky Name")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "leak@example.org")
    a = seeded(tmp_path, remote)
    git(tmp_path / "a", "config", "user.email", "me@example.org")
    git(tmp_path / "a", "config", "user.name", "Me")
    (tmp_path / "a" / "n.txt").write_text("n")
    a.commit_all()
    # a diverging merge commit as well: its message must not name the remote URL
    b = GitRepo(str(tmp_path / "b"))
    b.clone(remote)
    (tmp_path / "b" / "b.txt").write_text("b")
    b.commit_all()
    assert b.push()
    a.pull()
    fmt = "%an|%ae|%cn|%ce|%s"
    for line in git(tmp_path / "a", "log", f"--format={fmt}").splitlines():
        assert line == "config-saver|config-saver@localhost|config-saver|config-saver@localhost|sync"


def test_push_other_failure_is_transport_error(tmp_path):
    a = GitRepo(str(tmp_path / "a"))
    a.init(str(tmp_path / "does-not-exist.git"))
    (tmp_path / "a" / "f").write_text("x")
    a.commit_all()
    with pytest.raises(SyncTransportError):
        a.push()


def test_clone_failure_is_transport_error(tmp_path):
    with pytest.raises(SyncTransportError):
        GitRepo(str(tmp_path / "c")).clone(str(tmp_path / "missing.git"))


def test_reset_to_upstream(tmp_path, remote):
    a = seeded(tmp_path, remote)
    (tmp_path / "a" / "base.txt").write_text("dirty")
    (tmp_path / "a" / "restic").mkdir()
    (tmp_path / "a" / "restic" / "junk").write_text("j")
    (tmp_path / "a" / "keep.txt").write_text("k")
    a.reset_to_upstream()
    assert (tmp_path / "a" / "base.txt").read_text() == "base\n"
    assert not (tmp_path / "a" / "restic" / "junk").exists()
    assert (tmp_path / "a" / "keep.txt").exists()


def test_missing_git_is_not_set_up(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(SyncNotSetUpError):
        GitRepo(str(tmp_path / "a")).pull()


def _installed_locale():
    out = subprocess.run(["locale", "-a"], capture_output=True, text=True).stdout.split()
    norm = {name.lower().replace("utf8", "utf-8"): name for name in out}
    for want in ("es_es.utf-8", "de_de.utf-8"):
        if want in norm:
            return norm[want]
    return None


def test_push_rejection_detected_in_foreign_locale(tmp_path, remote, monkeypatch):
    loc = _installed_locale()
    if loc is None:
        pytest.skip("no es_ES/de_DE locale generated")
    monkeypatch.setenv("LC_ALL", loc)
    monkeypatch.setenv("LANGUAGE", loc.split("_")[0])
    a = seeded(tmp_path, remote)
    b = GitRepo(str(tmp_path / "b"))
    b.clone(remote)
    (tmp_path / "a" / "x").write_text("x")
    a.commit_all()
    a.push()
    (tmp_path / "b" / "y").write_text("y")
    b.commit_all()
    assert b.push() is False


def test_remote_rejection_is_transport_error(tmp_path, remote):
    a = seeded(tmp_path, remote)
    hook = tmp_path / "remote.git" / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho no >&2\nexit 1\n")
    hook.chmod(0o755)
    (tmp_path / "a" / "z").write_text("z")
    a.commit_all()
    with pytest.raises(SyncTransportError):
        a.push()


def test_commit_ignores_hooks_and_gpgsign(tmp_path, remote):
    a = seeded(tmp_path, remote)
    git(tmp_path / "a", "config", "commit.gpgsign", "true")
    hook = tmp_path / "a" / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    (tmp_path / "a" / "h").write_text("h")
    assert a.commit_all() is True


def test_clone_target_is_private(tmp_path, remote):
    seeded(tmp_path, remote)
    b = GitRepo(str(tmp_path / "b"))
    b.clone(remote)
    assert (tmp_path / "b").stat().st_mode & 0o777 == 0o700


def test_timeout_is_transport_error(tmp_path, monkeypatch):
    monkeypatch.setattr("config_saver.lib.sync.git_repo.TIMEOUT_SECONDS", 0.001)
    with pytest.raises(SyncTransportError):
        GitRepo(str(tmp_path / "a")).clone(str(tmp_path / "x.git"))


def test_git_runs_with_untranslated_locale(tmp_path, remote, monkeypatch):
    # Git's "! [rejected] ... (fetch first)" status words are not translated today, but
    # its error lines are (es_ES); the port pins LC_ALL=C so nothing we parse can change.
    real = subprocess.run(["which", "git"], capture_output=True, text=True).stdout.strip()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "locale.log"
    shim = bindir / "git"
    shim.write_text(f'#!/bin/sh\necho "$LC_ALL $LANGUAGE" >> {log}\nexec {real} "$@"\n')
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    monkeypatch.setenv("LC_ALL", "es_ES.UTF-8")
    monkeypatch.setenv("LANGUAGE", "es")
    GitRepo(str(tmp_path / "a")).init(remote)
    assert set(log.read_text().split("\n")) == {"C C", ""}


def _env_spy(tmp_path, monkeypatch):
    real = subprocess.run(["which", "git"], capture_output=True, text=True).stdout.strip()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "ssh.log"
    shim = bindir / "git"
    shim.write_text(f'#!/bin/sh\necho "[${{GIT_SSH_COMMAND-unset}}]" >> {log}\nexec {real} "$@"\n')
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    return log


def _ssh_values(log):
    return {line for line in log.read_text().splitlines() if line}


def test_ssh_batchmode_is_the_default(tmp_path, remote, monkeypatch):
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    monkeypatch.delenv("GIT_SSH", raising=False)
    log = _env_spy(tmp_path, monkeypatch)
    GitRepo(str(tmp_path / "a")).init(remote)
    # `git config --get` sees the same env, so the shim logs it too: every call has the default
    assert _ssh_values(log) == {"[unset]", "[ssh -o BatchMode=yes]"}


def test_core_ssh_command_is_not_overridden(tmp_path, remote, monkeypatch):
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    monkeypatch.delenv("GIT_SSH", raising=False)
    (tmp_path / "home" / ".gitconfig").write_text("[core]\n\tsshCommand = ssh -i /my/key\n")
    log = _env_spy(tmp_path, monkeypatch)
    GitRepo(str(tmp_path / "a")).init(remote)
    assert "[ssh -o BatchMode=yes]" not in _ssh_values(log)


def test_git_ssh_command_env_is_untouched(tmp_path, remote, monkeypatch):
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -i /env/key")
    log = _env_spy(tmp_path, monkeypatch)
    GitRepo(str(tmp_path / "a")).init(remote)
    assert _ssh_values(log) == {"[ssh -i /env/key]"}


def test_git_ssh_env_is_not_overridden(tmp_path, remote, monkeypatch):
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    monkeypatch.setenv("GIT_SSH", "/usr/bin/true")
    log = _env_spy(tmp_path, monkeypatch)
    GitRepo(str(tmp_path / "a")).init(remote)
    assert "[ssh -o BatchMode=yes]" not in _ssh_values(log)
