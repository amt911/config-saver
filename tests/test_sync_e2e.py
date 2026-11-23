"""--sync-init / --sync / --sync-resolve through the real command, two machines, one bare remote.

End to end on purpose: what is under test is the process exit code, stdout and the
real files on disk, which no in-process test can answer. Everything finer (exit
mapping per error, path conversion) is a unit test in tests/test_cli.py.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import write_config

pytestmark = [
    pytest.mark.restic,
    pytest.mark.skipif(
        any(shutil.which(b) is None for b in ("restic", "age", "age-keygen", "git")),
        reason="needs restic, age, age-keygen and git on PATH",
    ),
]

REPO_ROOT = str(Path(__file__).resolve().parent.parent)


class Machine:
    def __init__(self, root: Path, name: str, key: Path):
        self.home = root / name
        self.home.mkdir()
        self.key = key
        self.cfg_dir = root / f"{name}-configs"
        self.cfg_dir.mkdir()
        self.file = self.home / "app" / "settings.txt"
        write_config(self.cfg_dir / "app.yaml", [str(self.home / "app")], sync=True)
        self.env = {
            "PATH": os.environ["PATH"],
            "HOME": str(self.home),
            "PYTHONPATH": REPO_ROOT,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "RESTIC_CACHE_DIR": str(root / f"{name}-cache"),
        }

    def cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "config_saver", *args, "--identity", str(self.key)],
            env=self.env,
            capture_output=True,
            text=True,
            cwd=str(self.home),
            timeout=300,
        )

    def sync(self, *args: str) -> subprocess.CompletedProcess[str]:
        return self.cli("--input", str(self.cfg_dir), *args)

    def write(self, text: str) -> None:
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self.file.write_text(text, encoding="utf-8")


@pytest.fixture
def machines(tmp_path: Path) -> tuple[Machine, Machine, str]:
    key = tmp_path / "key.txt"
    subprocess.run(["age-keygen", "-o", str(key)], check=True, capture_output=True)
    recipient = subprocess.run(
        ["age-keygen", "-y", str(key)], check=True, capture_output=True, text=True
    ).stdout.strip()
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)], check=True, capture_output=True)
    return Machine(tmp_path, "a", key), Machine(tmp_path, "b", key), f"{remote}|{recipient}"


def test_two_machines_sync_conflict_and_resolve(machines: tuple[Machine, Machine, str]) -> None:
    a, b, remote_and_recipient = machines
    remote, recipient = remote_and_recipient.split("|")

    a.write("one\n")
    init_a = a.sync("--sync-init", remote, "--encrypt-to", recipient)
    assert init_a.returncode == 0, init_a.stderr + init_a.stdout
    init_b = b.sync("--sync-init", remote)  # the remote has content now: this clones
    assert init_b.returncode == 0, init_b.stderr + init_b.stdout

    assert a.sync("--sync").returncode == 0
    got = b.sync("--sync")
    assert got.returncode == 0, got.stderr + got.stdout
    assert b.file.read_text() == "one\n"

    # A changes and publishes, B picks it up.
    a.write("two\n")
    assert a.sync("--sync").returncode == 0
    assert b.sync("--sync").returncode == 0
    assert b.file.read_text() == "two\n"

    # Both change the same file: A publishes, B must stop with 11 and name the file.
    a.write("from a\n")
    b.write("from b\n")
    assert a.sync("--sync").returncode == 0
    conflict = b.sync("--sync")
    assert conflict.returncode == 11, conflict.stderr + conflict.stdout
    assert "app: ~/app/settings.txt" in conflict.stdout
    assert "Resolve with: config-saver --sync-resolve local|remote [--path P]" in conflict.stdout
    assert b.file.read_text() == "from b\n"  # nothing was written
    assert b.sync("--sync-status").returncode == 11

    resolved = b.sync("--sync-resolve", "remote")
    assert resolved.returncode == 0, resolved.stderr + resolved.stdout
    assert b.file.read_text() == "from a\n"
    assert b.sync("--sync-status").returncode == 0
