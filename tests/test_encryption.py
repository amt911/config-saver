"""Optional archive encryption.

The round-trip tests drive the real binaries: a mocked `age` would prove nothing
about whether config-saver produces a file `age` can actually decrypt. Each is
skipped when its backend is not installed, and CI installs both.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tarfile
from pathlib import Path

import pytest
import yaml

from config_saver.lib import crypto
from config_saver.lib.backup_manager.backup_manager import BackupManager
from config_saver.lib.cli.cli import CLI, EXIT_ENCRYPTION, EXIT_OK, EXIT_USAGE
from config_saver.lib.errors import EncryptionError
from config_saver.lib.models.encryption_model import EncryptionModel
from config_saver.lib.tar_compressor.tar_compressor import TarCompressor
from config_saver.lib.tar_compressor.tar_decompressor import TarDecompressor

from .conftest import make_model, write_config

age_only = pytest.mark.skipif(shutil.which("age") is None, reason="the age binary is not installed")
gpg_only = pytest.mark.skipif(shutil.which("gpg") is None, reason="the gpg binary is not installed")


@pytest.fixture
def age_identity(tmp_path: Path) -> tuple[str, str]:
    """Return (identity file, public recipient) for a throwaway age key."""
    identity = tmp_path / "age.key"
    generated = subprocess.run(["age-keygen", "-o", str(identity)], capture_output=True, text=True, check=True)
    # age-keygen prints "Public key: age1..." on stderr.
    recipient = next(
        line.split(":", 1)[1].strip() for line in generated.stderr.splitlines() if "public key" in line.lower()
    )
    return str(identity), recipient


@pytest.fixture
def gpg_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """A throwaway GNUPGHOME holding one unprotected key; returns its recipient."""
    home = tmp_path / "gnupg"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("GNUPGHOME", str(home))
    subprocess.run(
        # "default default" gives a key with an *encryption* subkey; an rsa-only
        # key is sign-capable and gpg refuses to encrypt to it.
        [
            "gpg",
            "--batch",
            "--yes",
            "--passphrase",
            "",
            "--quick-generate-key",
            "config-saver-test",
            "default",
            "default",
            "never",
        ],
        capture_output=True,
        check=True,
    )
    return "config-saver-test"


def _tree(home: Path) -> Path:
    tree = home / "data"
    tree.mkdir()
    (tree / "secret.conf").write_text("token = hunter2\n", encoding="utf-8")
    return tree


# ------------------------------------------------------------------- helpers


def test_suffixes_and_detection() -> None:
    assert crypto.suffix_for("age") == ".age"
    assert crypto.suffix_for("gpg") == ".gpg"
    assert crypto.detect_method("backup.tar.gz.age") == "age"
    assert crypto.detect_method("backup.tar.gz.gpg") == "gpg"
    assert crypto.detect_method("backup.tar.gz") is None


def test_unknown_method_is_rejected() -> None:
    with pytest.raises(EncryptionError, match="Unknown encryption method"):
        crypto.suffix_for("rot13")
    with pytest.raises(EncryptionError, match="Unknown encryption method"):
        crypto.require_binary("rot13")


def test_a_missing_backend_says_what_to_install(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    with pytest.raises(EncryptionError, match="not on PATH"):
        crypto.require_binary("age")


def test_recipients_are_required() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        EncryptionModel(method="age", recipients=[])


def test_missing_backend_fails_before_any_work(
    tmp_path: Path, fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No half-written archive when the backend is not installed."""
    tree = _tree(fake_home)
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    out = tmp_path / "out.tar.gz"

    model = make_model([str(tree)], encrypt={"method": "age", "recipients": ["age1whatever"]})
    with pytest.raises(EncryptionError):
        TarCompressor(model, str(out)).compress()

    assert list(tmp_path.glob("*.tar.gz*")) == []


# --------------------------------------------------------------- round trips


@age_only
def test_age_round_trip(tmp_path: Path, fake_home: Path, age_identity: tuple[str, str]) -> None:
    identity, recipient = age_identity
    tree = _tree(fake_home)
    out = tmp_path / "out.tar.gz"

    model = make_model([str(tree)], encrypt={"method": "age", "recipients": [recipient]})
    result = TarCompressor(model, str(out)).compress()

    encrypted = Path(result.output_path)
    assert encrypted.name == "out.tar.gz.age"
    assert not out.exists(), "the plaintext archive must not survive"
    assert stat.S_IMODE(encrypted.stat().st_mode) == 0o600
    # It really is encrypted: tarfile cannot make sense of it.
    with pytest.raises(tarfile.TarError), tarfile.open(encrypted, "r:gz"):
        pass

    restored = tmp_path / "restored"
    outcome = TarDecompressor(str(encrypted), str(restored), identity=identity).decompress()
    assert outcome.decrypted_with == "age"
    assert (restored / "home" / "user" / "data" / "secret.conf").read_text() == "token = hunter2\n"


@age_only
def test_age_needs_an_identity_to_decrypt(tmp_path: Path, fake_home: Path, age_identity: tuple[str, str]) -> None:
    _identity, recipient = age_identity
    tree = _tree(fake_home)
    model = make_model([str(tree)], encrypt={"method": "age", "recipients": [recipient]})
    result = TarCompressor(model, str(tmp_path / "out.tar.gz")).compress()

    with pytest.raises(EncryptionError, match="identity file"):
        TarDecompressor(result.output_path, str(tmp_path / "restored")).decompress()


@age_only
def test_cli_encrypt_to_and_identity(tmp_path: Path, fake_home: Path, age_identity: tuple[str, str]) -> None:
    identity, recipient = age_identity
    tree = _tree(fake_home)
    cfg = write_config(tmp_path / "c.yaml", [str(tree)])
    archive = tmp_path / "cli.tar.gz"

    assert CLI(["--compress", "-i", str(cfg), "-o", str(archive), "--encrypt-to", recipient]).run() == EXIT_OK
    encrypted = tmp_path / "cli.tar.gz.age"
    assert encrypted.is_file() and not archive.exists()

    restored = tmp_path / "restored"
    assert CLI(["--decompress", "-i", str(encrypted), "-o", str(restored), "--identity", identity]).run() == EXIT_OK
    assert (restored / "home" / "user" / "data" / "secret.conf").is_file()


@gpg_only
def test_gpg_round_trip(tmp_path: Path, fake_home: Path, gpg_home: str) -> None:
    tree = _tree(fake_home)
    out = tmp_path / "out.tar.gz"

    model = make_model([str(tree)], encrypt={"method": "gpg", "recipients": [gpg_home]})
    result = TarCompressor(model, str(out)).compress()

    encrypted = Path(result.output_path)
    assert encrypted.name == "out.tar.gz.gpg"
    assert not out.exists()
    assert result.encryption is not None and result.encryption.method == "gpg"

    restored = tmp_path / "restored"
    outcome = TarDecompressor(str(encrypted), str(restored)).decompress()
    assert outcome.decrypted_with == "gpg"
    assert (restored / "home" / "user" / "data" / "secret.conf").read_text() == "token = hunter2\n"


@gpg_only
def test_encryption_declared_in_the_configuration_file(tmp_path: Path, fake_home: Path, gpg_home: str) -> None:
    """`encrypt:` in the config is what a systemd timer uses; no flags involved."""
    tree = _tree(fake_home)
    cfg = tmp_path / "secrets.yaml"
    cfg.write_text(
        yaml.safe_dump({"directories": [str(tree)], "encrypt": {"method": "gpg", "recipients": [gpg_home]}}),
        encoding="utf-8",
    )

    manager = BackupManager(str(tmp_path / "saves"))
    archive, result = manager.compress_config_to_timestamp_dir(
        str(cfg), str(tmp_path / "saves" / "configs" / "secrets"), "20260101-000000"
    )
    assert archive.endswith("secrets-20260101-000000.tar.gz.gpg")
    assert result.encryption is not None
    assert os.path.isfile(archive)


@gpg_only
def test_encrypted_archives_are_listed_and_exported(tmp_path: Path, fake_home: Path, gpg_home: str) -> None:
    """An encrypted archive must not disappear from --list / --export-*."""
    tree = _tree(fake_home)
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    write_config(cfg_dir / "secrets.yaml", [str(tree)])

    assert CLI(["--compress", "-i", str(cfg_dir), "--encrypt-to", gpg_home, "--encrypt-method", "gpg"]).run() == EXIT_OK

    manager = BackupManager()
    archives = manager.list_archives()
    assert archives and all(path.endswith(".tar.gz.gpg") for path in archives)

    exports = tmp_path / "exports"
    assert CLI(["--export-all-configs", "--output", str(exports)]).run() == EXIT_OK
    assert [p.name for p in exports.iterdir()] == [Path(archives[0]).name]


# ------------------------------------------------------------------- the CLI


def test_encrypt_method_without_recipient_is_a_usage_error(tmp_path: Path, fake_home: Path) -> None:
    cfg = write_config(tmp_path / "c.yaml", [str(fake_home)])
    assert CLI(["--compress", "-i", str(cfg), "-o", str(tmp_path / "a.tar.gz"), "--encrypt-method", "gpg"]).run() == (
        EXIT_USAGE
    )


def test_encryption_failure_has_its_own_exit_code(
    tmp_path: Path, fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = write_config(tmp_path / "c.yaml", [str(_tree(fake_home))])
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    code = CLI(["--compress", "-i", str(cfg), "-o", str(tmp_path / "a.tar.gz"), "--encrypt-to", "age1nobody"]).run()
    assert code == EXIT_ENCRYPTION
