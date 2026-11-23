"""Thin port over the git CLI for the sync working tree."""

from __future__ import annotations

import os
import subprocess

from config_saver.lib.errors import SyncNotSetUpError, SyncTransportError

BRANCH = "main"
GITIGNORE = ("restic/locks/", "restic-store.json", ".staging/", ".lock")  # .staging/ holds plaintext
# Git metadata is visible to the remote's host: every commit carries this fixed identity and
# message, whatever the user's git config says, so it never leaks a name, host or path.
_IDENTITY = {"NAME": "config-saver", "EMAIL": "config-saver@localhost"}
COMMIT_MESSAGE = "sync"
_STALE_MARKERS = ("non-fast-forward", "fetch first")
TIMEOUT_SECONDS = 300


class GitRepo:
    def __init__(self, path: str):
        self.path = path

    def _run(self, *args: str, cwd: bool = True, pre: list[str] | None = None, check: bool = True):
        cmd = ["git", *(["-C", self.path] if cwd else []), *(pre or []), *args]
        # LC_ALL=C: we match git's output, which must not be translated.
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C", "LANGUAGE": "C"}
        env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
        for role in ("AUTHOR", "COMMITTER"):
            for key, value in _IDENTITY.items():
                env[f"GIT_{role}_{key}"] = value
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=TIMEOUT_SECONDS)
        except FileNotFoundError as exc:
            raise SyncNotSetUpError("git is not installed; multi-machine sync needs it") from exc
        except subprocess.TimeoutExpired as exc:
            raise SyncTransportError(f"git {args[0]} timed out after {TIMEOUT_SECONDS}s") from exc
        if check and proc.returncode != 0:
            raise SyncTransportError(f"git {args[0]} failed: {(proc.stderr or proc.stdout).strip()}")
        return proc

    def remote_has_branch(self, url: str) -> bool:
        """True when ``url`` already has the sync branch (someone ran init there)."""
        return bool(self._run("ls-remote", "--heads", url, BRANCH, cwd=False).stdout.strip())

    def clone(self, url: str) -> None:
        os.makedirs(self.path, mode=0o700, exist_ok=True)
        self._run("clone", url, self.path, cwd=False)

    def init(self, url: str) -> None:
        os.makedirs(self.path, mode=0o700, exist_ok=True)
        self._run("init", "-b", BRANCH)
        with open(os.path.join(self.path, ".gitignore"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(GITIGNORE) + "\n")
        self._run("remote", "add", "origin", url)

    def pull(self) -> None:
        heads = self._run("ls-remote", "--heads", "origin", BRANCH)
        if not heads.stdout.strip():
            return  # remote branch does not exist yet
        # fetch + merge (not `git pull`) so a merge commit gets our fixed message, not the remote URL.
        self._run("fetch", "origin", BRANCH)
        proc = self._run("merge", "--no-edit", "-m", COMMIT_MESSAGE, "FETCH_HEAD", check=False)
        if proc.returncode != 0:
            self._run("merge", "--abort", check=False)
            raise SyncTransportError(f"git pull failed: {(proc.stderr or proc.stdout).strip()}")

    def commit_all(self) -> bool:
        self._run("add", "-A")
        if not self._run("status", "--porcelain").stdout.strip():
            return False
        self._run("commit", "--no-verify", "-m", COMMIT_MESSAGE, pre=["-c", "commit.gpgsign=false"])
        return True

    def push(self) -> bool:
        proc = self._run("push", "-u", "origin", BRANCH, check=False)
        if proc.returncode == 0:
            return True
        for line in proc.stderr.splitlines():
            if "[rejected]" in line and any(m in line for m in _STALE_MARKERS):
                return False
        raise SyncTransportError(f"git push failed: {proc.stderr.strip()}")

    def reset_to_upstream(self) -> None:
        self._run("reset", "--hard", "@{u}")
        self._run("clean", "-fd", "restic/")
