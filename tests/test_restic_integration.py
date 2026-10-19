"""Real restic + age round trip. Skipped unless both binaries are on PATH."""

from __future__ import annotations

import filecmp
import gzip
import os
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from config_saver.lib.errors import StoreError
from config_saver.lib.models.retention_model import RetentionModel
from config_saver.lib.restic_store import ResticStore
from config_saver.lib.tar_compressor.tar_compressor import TarCompressor
from config_saver.lib.tar_compressor.tar_decompressor import TarDecompressor
from tests.conftest import make_model

pytestmark = [
    pytest.mark.restic,
    pytest.mark.skipif(
        any(shutil.which(b) is None for b in ("restic", "age", "age-keygen")),
        reason="needs restic, age and age-keygen on PATH",
    ),
]


def _identity(tmp_path: Path) -> tuple[str, str]:
    key = tmp_path / "key.txt"
    subprocess.run(["age-keygen", "-o", str(key)], check=True, capture_output=True)
    public = subprocess.run(["age-keygen", "-y", str(key)], check=True, capture_output=True, text=True)
    return str(key), public.stdout.strip()


def test_real_restic_roundtrip(tmp_path: Path, sample_tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RESTIC_CACHE_DIR", str(tmp_path / "cache"))
    # Incompressible payload big enough that metadata does not dominate the ratio.
    for i in range(4):
        (sample_tree / f"blob{i}.bin").write_bytes(os.urandom(1024 * 1024))
    key, recipient = _identity(tmp_path)
    saves = tmp_path / "saves"
    store = ResticStore(str(saves), identity=key)
    store.init(recipient)
    assert (saves / "restic-password.age").stat().st_mode & 0o777 == 0o600
    assert (saves / "restic-store.json").stat().st_mode & 0o777 == 0o600
    assert (saves / "restic").stat().st_mode & 0o777 == 0o700

    compressor = TarCompressor(make_model([str(sample_tree)]), "")
    first_time = datetime(2026, 10, 1, 3, 0, 0)
    first, _, first_added = store.backup(compressor.compress_to_stream, "my-cfg", first_time, "héllo, wörld")
    second_time = first_time + timedelta(days=1)
    second, result, second_added = store.backup(compressor.compress_to_stream, "my-cfg", second_time, None)
    assert result.added > 0
    assert second_added < first_added * 0.10

    snaps = store.snapshots("my-cfg")
    assert [s.id for s in snaps] == [first.id, second.id]
    assert snaps[0].time == first_time and snaps[1].time == second_time
    assert snaps[0].description == "héllo, wörld" and snaps[1].description is None
    assert store.snapshots("other") == []

    # TarDecompressor reads .tar.gz, so gzip the dumped tar the way an export will.
    tar_path = tmp_path / "latest.tar.gz"
    with gzip.open(tar_path, "wb") as out:
        store.dump(second.id, "my-cfg", out)
    restore_home = tmp_path / "restore"
    restore_home.mkdir()
    monkeypatch.setenv("HOME", str(restore_home))
    TarDecompressor(str(tar_path), output_dir=str(restore_home)).decompress()
    restored = next(restore_home.rglob("blob0.bin")).parent
    for name in ["blob0.bin", "blob3.bin", "plain.txt", "executable.sh"]:
        assert filecmp.cmp(sample_tree / name, restored / name, shallow=False)

    zero = RetentionModel(daily=0, weekly=0, monthly=0, yearly=0)
    assert [s.id for s in store.forget("my-cfg", zero, dry_run=True)] == [first.id]
    assert len(store.snapshots("my-cfg")) == 2
    assert [s.id for s in store.forget("my-cfg", zero, dry_run=False)] == [first.id]
    store.prune()
    assert [s.id for s in store.snapshots("my-cfg")] == [second.id]


def test_wrong_identity_gives_clear_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RESTIC_CACHE_DIR", str(tmp_path / "cache"))
    key, recipient = _identity(tmp_path)
    saves = tmp_path / "saves"
    ResticStore(str(saves), identity=key).init(recipient)
    other = tmp_path / "other.txt"
    subprocess.run(["age-keygen", "-o", str(other)], check=True, capture_output=True)
    with pytest.raises(StoreError, match="cannot decrypt the store password"):
        ResticStore(str(saves), identity=str(other)).snapshots()


def test_apostrophe_path_gives_clear_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RESTIC_CACHE_DIR", str(tmp_path / "cache"))
    key, recipient = _identity(tmp_path)
    weird = tmp_path / "it's"
    weird.mkdir()
    with pytest.raises(StoreError, match="cannot carry"):
        ResticStore(str(weird), identity=key).init(recipient)
    assert not (weird / "restic").exists()


def test_cli_compress_into_the_store_then_prune(
    tmp_path: Path, fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real restic through the CLI: two configs twice, then retention on one of them."""
    import yaml

    from config_saver.lib.cli import cli as cli_module
    from config_saver.lib.cli.cli import CLI, EXIT_OK

    monkeypatch.setenv("RESTIC_CACHE_DIR", str(tmp_path / "cache"))
    key, recipient = _identity(tmp_path)
    saves = fake_home / ".config" / "config-saver"
    ResticStore(str(saves), identity=key).init(recipient)

    data = fake_home / "data"
    data.mkdir()
    (data / "f.txt").write_text("hello\n")
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    (cfg_dir / "one.yaml").write_text(yaml.safe_dump({"directories": [str(data)]}))
    two = cfg_dir / "two.yaml"
    two.write_text(yaml.safe_dump({"directories": [str(data)]}))

    day = {"n": 0}

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[no-untyped-def]
            day["n"] += 1
            return datetime(2026, 10, day["n"], 3, 0, 0)

    monkeypatch.setattr(cli_module, "datetime", Clock)

    for _ in range(2):  # the default --jobs auto: both configs back up in parallel
        assert CLI(["--compress", "-i", str(cfg_dir)]).run() == EXIT_OK
    store = ResticStore(str(saves))
    assert len(store.snapshots("one")) == 2 and len(store.snapshots("two")) == 2
    assert not list(saves.rglob("*.tar.gz"))

    zero = {"daily": 0, "weekly": 0, "monthly": 0, "yearly": 0}
    two.write_text(yaml.safe_dump({"directories": [str(data)], "retention": zero}))
    assert CLI(["--compress", "-i", str(cfg_dir)]).run() == EXIT_OK

    assert len(store.snapshots("one")) == 3  # defaults keep every distinct day
    kept = store.snapshots("two")
    assert len(kept) == 1 and kept[0].time == datetime(2026, 10, 3, 3, 0, 0)


def test_cli_export_from_the_store_decrypts_back_to_the_source(
    tmp_path: Path, fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Compress with an `encrypt:` block, export, then --decompress --identity the export."""
    import yaml

    from config_saver.lib.cli.cli import CLI, EXIT_OK

    monkeypatch.setenv("RESTIC_CACHE_DIR", str(tmp_path / "cache"))
    key, recipient = _identity(tmp_path)
    saves = fake_home / ".config" / "config-saver"
    ResticStore(str(saves), identity=key).init(recipient)

    data = fake_home / "data"
    (data / "sub").mkdir(parents=True)
    (data / "f.txt").write_text("hello\n")
    (data / "sub" / "b.bin").write_bytes(os.urandom(4096))
    cfg_dir = fake_home / ".config" / "config-saver" / "configs.d"
    cfg_dir.mkdir()
    (cfg_dir / "mine.yaml").write_text(
        yaml.safe_dump({"directories": [str(data)], "encrypt": {"method": "age", "recipients": [recipient]}})
    )
    assert CLI(["--compress"]).run() == EXIT_OK

    assert CLI(["--list", "--identity", key]).run() == EXIT_OK
    out_dir = tmp_path / "exports"
    assert CLI(["--export-all-configs", "--identity", key, "--output", str(out_dir)]).run() == EXIT_OK
    [export] = list(out_dir.iterdir())
    assert export.name.startswith("mine-") and export.name.endswith(".tar.gz.age")
    assert b"hello" not in export.read_bytes()

    restore = tmp_path / "restore"
    restore.mkdir()
    assert CLI(["--decompress", "-i", str(export), "-o", str(restore), "--identity", key]).run() == EXIT_OK
    restored = next(restore.rglob("f.txt")).parent
    assert filecmp.dircmp(data, restored).diff_files == []
    assert (restored / "sub" / "b.bin").read_bytes() == (data / "sub" / "b.bin").read_bytes()


def test_cli_migrate_real_age_archives_then_delete_and_roundtrip(
    tmp_path: Path, fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real age + restic: two tar-mode .tar.gz.age archives migrate, re-run skips, delete, export == source."""
    from config_saver.lib.backup_manager.backup_manager import BackupManager
    from config_saver.lib.cli.cli import CLI, EXIT_OK
    from config_saver.lib.models.encryption_model import EncryptionModel
    from tests.conftest import write_config

    monkeypatch.setenv("RESTIC_CACHE_DIR", str(tmp_path / "cache"))
    key, recipient = _identity(tmp_path)
    saves = fake_home / ".config" / "config-saver"

    data = fake_home / "data"
    data.mkdir()
    cfg = write_config(tmp_path / "my-cfg.yaml", [str(data)])
    encryption = EncryptionModel(method="age", recipients=[recipient])
    manager = BackupManager(str(saves))
    stamps = ["20260101-030000", "20260102-040506"]
    for index, stamp in enumerate(stamps):
        (data / "f.txt").write_text(f"version {index}\n")
        (data / "blob.bin").write_bytes(os.urandom(2048))
        manager.compress_config_to_timestamp_dir(
            str(cfg), str(saves / "configs" / "my-cfg"), stamp, description=f"run {index}", encryption=encryption
        )
    final_blob = (data / "blob.bin").read_bytes()
    archives = sorted((saves / "configs").rglob("*.tar.gz.age"))
    assert len(archives) == 2

    assert CLI(["--restic-init", "--encrypt-to", recipient, "--identity", key]).run() == EXIT_OK
    store = ResticStore(str(saves))
    assert CLI(["--migrate", "--identity", key]).run() == EXIT_OK
    snaps = store.snapshots("my-cfg")
    assert [s.time for s in snaps] == [datetime(2026, 1, 1, 3, 0, 0), datetime(2026, 1, 2, 4, 5, 6)]
    assert [s.description for s in snaps] == ["run 0", "run 1"]
    assert all(a.exists() for a in archives)  # nothing deleted without the flag

    assert CLI(["--migrate", "--identity", key]).run() == EXIT_OK
    assert len(store.snapshots("my-cfg")) == 2  # idempotent

    assert CLI(["--migrate", "--delete-migrated", "--identity", key]).run() == EXIT_OK  # skipped ones verified
    assert not list((saves / "configs").rglob("*.tar.gz.age"))
    assert not (saves / "configs" / "my-cfg" / stamps[0]).exists()

    export = tmp_path / "export.tar.gz"
    assert CLI(["--export-config", "my-cfg", "--identity", key, "--output", str(export)]).run() == EXIT_OK
    restore = tmp_path / "restore"
    restore.mkdir()
    assert CLI(["--decompress", "-i", str(export), "-o", str(restore)]).run() == EXIT_OK
    restored = next(restore.rglob("f.txt")).parent
    assert (restored / "f.txt").read_text() == "version 1\n"
    assert (restored / "blob.bin").read_bytes() == final_blob
    assert CLI(["--prune", "--dry-run", "--identity", key]).run() == EXIT_OK
