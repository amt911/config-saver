"""Edge cases and error paths that the happy-path suite does not reach.

These are the branches that only run when something is unusual: a progress bar
attached, a metadata member that cannot be parsed, a saves directory that cannot
be created, a link member that is legitimate. They are exactly the paths that
break unnoticed.
"""

from __future__ import annotations

import io
import json
import os
import stat
import tarfile
from pathlib import Path

import pytest
import yaml

from config_saver.lib.backup_manager.backup_manager import BackupManager
from config_saver.lib.cli.cli import CLI, EXIT_OK, EXIT_PERMISSION, EXIT_RUNTIME
from config_saver.lib.errors import UnsafeArchiveError
from config_saver.lib.parser.parser import Parser
from config_saver.lib.tar_compressor.tar_compressor import (
    HOME_CONTENT_PLACEHOLDER,
    METADATA_MEMBER,
    TarCompressor,
)
from config_saver.lib.tar_compressor.tar_decompressor import TarDecompressor

from .conftest import archive_names, make_model, write_config

# --------------------------------------------------------------- compressor


def test_progress_mode_compresses_the_same_files(tmp_path: Path, fake_home: Path) -> None:
    tree = fake_home / "tree"
    (tree / "sub").mkdir(parents=True)
    (tree / "sub" / "f.txt").write_text("data", encoding="utf-8")

    plain = tmp_path / "plain.tar.gz"
    with_bar = tmp_path / "bar.tar.gz"
    TarCompressor(make_model([str(tree)]), str(plain)).compress()
    result = TarCompressor(make_model([str(tree)]), str(with_bar), show_progress=True).compress()

    assert archive_names(plain) == archive_names(with_bar)
    assert result.added == len(archive_names(with_bar))


def test_progress_mode_reports_skipped_root_owned_files(
    tmp_path: Path, fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (fake_home / "rooted.txt").write_text("x")
    monkeypatch.setattr(TarCompressor, "_is_root_owned", lambda _self, _path: True)

    out = tmp_path / "out.tar.gz"
    result = TarCompressor(make_model([str(fake_home / "rooted.txt")]), str(out), show_progress=True).compress()
    assert result.skipped_root_owned == [str(fake_home / "rooted.txt")]


def test_root_owned_check_survives_a_vanished_file(fake_home: Path) -> None:
    compressor = TarCompressor(make_model([]), "unused.tar.gz")
    assert compressor._is_root_owned(str(fake_home / "gone")) is False


def test_unreadable_file_is_not_normalized(fake_home: Path) -> None:
    """A file we cannot read is archived as-is rather than crashing the run."""
    secret = fake_home / "secret.conf"
    secret.write_text(f"home={fake_home}\n", encoding="utf-8")
    os.chmod(secret, 0o000)
    try:
        compressor = TarCompressor(make_model([], normalize_content=True), "unused.tar.gz")
        assert compressor._is_text_file(str(secret)) is False
        assert compressor._normalize_file_content(str(secret)) is None
    finally:
        os.chmod(secret, 0o600)


def test_top_level_symlink_entry_is_archived_as_a_link(tmp_path: Path, fake_home: Path) -> None:
    target = fake_home / "real.txt"
    target.write_text("x")
    link = fake_home / "link.txt"
    link.symlink_to(target)

    out = tmp_path / "out.tar.gz"
    TarCompressor(make_model([str(link)]), str(out)).compress()
    with tarfile.open(out, "r:gz") as tar:
        member = tar.getmember(os.path.join("home", "user", "link.txt"))
    assert member.issym()


def test_base_dir_defaults_to_the_working_directory(fake_home: Path) -> None:
    assert TarCompressor(make_model([]), "unused.tar.gz").base_dir == os.getcwd()


# ------------------------------------------------------------- decompressor


def test_progress_mode_extracts_the_same_members(tmp_path: Path, fake_home: Path) -> None:
    tree = fake_home / "tree"
    tree.mkdir()
    (tree / "f.txt").write_text("data", encoding="utf-8")
    archive = tmp_path / "a.tar.gz"
    TarCompressor(make_model([str(tree)]), str(archive)).compress()

    out = tmp_path / "out"
    result = TarDecompressor(str(archive), str(out), show_progress=True).decompress()
    assert result.extracted > 0
    assert (out / "home" / "user" / "tree" / "f.txt").read_text() == "data"


def test_empty_member_name_is_refused(tmp_path: Path, fake_home: Path) -> None:
    archive = tmp_path / "a.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo(".")
        tar.addfile(info, io.BytesIO(b""))
    with pytest.raises(UnsafeArchiveError, match="empty member name") as excinfo:
        TarDecompressor(str(archive), str(tmp_path / "out")).decompress()
    assert excinfo.value.member_name == "."


def test_unparsable_metadata_falls_back_to_legacy_behaviour(tmp_path: Path, fake_home: Path) -> None:
    """A metadata member we cannot read must not make the restore fail."""
    archive = tmp_path / "a.tar.gz"
    payload = f"path={HOME_CONTENT_PLACEHOLDER}/x\n".encode()
    with tarfile.open(archive, "w:gz") as tar:
        broken = b"{not json"
        info = tarfile.TarInfo(METADATA_MEMBER)
        info.size = len(broken)
        tar.addfile(info, io.BytesIO(broken))
        info = tarfile.TarInfo("home/user/conf")
        info.size = len(payload)
        info.mode = 0o600
        tar.addfile(info, io.BytesIO(payload))

    out = tmp_path / "out"
    result = TarDecompressor(str(archive), str(out)).decompress()
    assert result.normalized_content is True
    assert (out / "home" / "user" / "conf").read_text() == f"path={fake_home}/x\n"


def test_metadata_that_is_not_an_object_is_ignored(tmp_path: Path, fake_home: Path) -> None:
    archive = tmp_path / "a.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        payload = json.dumps([1, 2, 3]).encode()
        info = tarfile.TarInfo(METADATA_MEMBER)
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    assert TarDecompressor(str(archive), str(tmp_path / "out")).decompress().normalized_content is True


def test_metadata_disables_content_denormalization(tmp_path: Path, fake_home: Path) -> None:
    tree = fake_home / "tree"
    tree.mkdir()
    body = f"literal {HOME_CONTENT_PLACEHOLDER} stays\n"
    (tree / "conf").write_text(body, encoding="utf-8")

    archive = tmp_path / "a.tar.gz"
    TarCompressor(make_model([str(tree)], normalize_content=False), str(archive)).compress()
    out = tmp_path / "out"
    result = TarDecompressor(str(archive), str(out)).decompress()

    assert result.normalized_content is False
    assert (out / "home" / "user" / "tree" / "conf").read_text() == body


def test_binary_content_is_never_denormalized(fake_home: Path) -> None:
    decompressor = TarDecompressor("unused.tar.gz")
    content = b"\x00\x01" + HOME_CONTENT_PLACEHOLDER.encode()
    assert decompressor._denormalize_file_content(content) == content


def test_latin1_content_is_denormalized(fake_home: Path) -> None:
    decompressor = TarDecompressor("unused.tar.gz")
    content = f"café {HOME_CONTENT_PLACEHOLDER}".encode("latin-1")
    restored = decompressor._denormalize_file_content(content).decode("latin-1")
    assert restored == f"café {fake_home}"


def test_valid_hardlink_inside_the_archive_is_restored(tmp_path: Path, fake_home: Path) -> None:
    archive = tmp_path / "a.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        data = b"shared"
        info = tarfile.TarInfo("home/user/original")
        info.size = len(data)
        info.mode = 0o644
        tar.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo("home/user/hard")
        link.type = tarfile.LNKTYPE
        link.linkname = "home/user/original"
        tar.addfile(link)

    out = tmp_path / "out"
    TarDecompressor(str(archive), str(out)).decompress()
    original = out / "home" / "user" / "original"
    hard = out / "home" / "user" / "hard"
    assert hard.read_bytes() == b"shared"
    assert original.stat().st_ino == hard.stat().st_ino


def test_existing_symlink_at_the_destination_is_replaced_not_followed(tmp_path: Path, fake_home: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("untouched", encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    (out / "link").symlink_to(outside)

    archive = tmp_path / "a.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo("link")
        info.type = tarfile.SYMTYPE
        info.linkname = "elsewhere"
        tar.addfile(info)

    TarDecompressor(str(archive), str(out)).decompress()
    assert os.readlink(out / "link") == "elsewhere"
    assert outside.read_text() == "untouched"


def test_a_file_landing_on_an_existing_symlink_is_refused(tmp_path: Path, fake_home: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("untouched", encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    (out / "payload").symlink_to(outside)

    archive = tmp_path / "a.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        data = b"PWNED"
        info = tarfile.TarInfo("payload")
        info.size = len(data)
        info.mode = 0o644
        tar.addfile(info, io.BytesIO(data))

    with pytest.raises(UnsafeArchiveError, match="symlink"):
        TarDecompressor(str(archive), str(out)).decompress()
    assert outside.read_text() == "untouched"


def test_directory_modes_are_restored(tmp_path: Path, fake_home: Path) -> None:
    tree = fake_home / "tree"
    (tree / "private").mkdir(parents=True)
    os.chmod(tree / "private", 0o700)

    archive = tmp_path / "a.tar.gz"
    TarCompressor(make_model([str(tree)]), str(archive)).compress()
    out = tmp_path / "out"
    TarDecompressor(str(archive), str(out)).decompress()
    assert stat.S_IMODE((out / "home" / "user" / "tree" / "private").stat().st_mode) == 0o700


# ------------------------------------------------------------------ manager


def test_saves_dir_falls_back_when_it_cannot_be_created(
    tmp_path: Path, fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read-only $HOME/.config must fall back to the XDG data dir, not crash."""
    blocked = str(tmp_path / "blocked")
    real_mkdir = os.mkdir

    def guarded(path, mode=0o777, *args, **kwargs):
        if str(path).startswith(blocked):
            raise PermissionError(13, "Permission denied", str(path))
        return real_mkdir(path, mode, *args, **kwargs)

    monkeypatch.setattr(os, "mkdir", guarded)
    manager = BackupManager(blocked)
    resolved = manager.ensure_saves_dir()

    assert resolved == str(fake_home / ".local" / "share" / "config-saver" / "saves")
    assert Path(resolved).is_dir()


def test_description_write_failure_is_not_swallowed(
    tmp_path: Path, fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = fake_home / "d"
    data.mkdir()
    (data / "f").write_text("x")
    cfg = write_config(tmp_path / "c.yaml", [str(data)])
    dest = tmp_path / "dest"

    real_open = os.open

    def guarded(path, flags, mode=0o777, *args, **kwargs):
        if str(path).endswith("description.txt"):
            raise PermissionError(13, "Permission denied", str(path))
        return real_open(path, flags, mode, *args, **kwargs)

    monkeypatch.setattr(os, "open", guarded)
    with pytest.raises(PermissionError):
        BackupManager(str(tmp_path / "saves")).compress_config_to_directory(
            str(cfg), str(dest), "a.tar.gz", description="note"
        )


def test_backwards_compatible_aliases_still_exist() -> None:
    assert BackupManager.compress_yaml_file is BackupManager.compress_config_file
    assert BackupManager.compress_yaml_to_timestamp_dir is BackupManager.compress_config_to_timestamp_dir
    assert BackupManager.compress_directory_of_yamls is BackupManager.compress_directory_of_configs


def test_description_lookup_returns_none_without_a_file(tmp_path: Path) -> None:
    manager = BackupManager(str(tmp_path))
    assert manager.get_description_for_archive("") is None
    assert manager.get_description_for_archive(str(tmp_path / "nope.tar.gz")) is None


# ------------------------------------------------------------------- parser


def test_save_and_export_locations_are_expanded(tmp_path: Path, fake_home: Path) -> None:
    """The parser expands `location` fields too, not only `directories`."""
    cfg = tmp_path / "c.yaml"
    cfg.write_text(yaml.safe_dump({"directories": ["$HOME/x"]}))
    parser = Parser(str(cfg))
    expanded = parser._expand_dict(
        {"save": {"main": {"location": "$HOME/saves"}}, "export": {"main": {"location": "$CONFIG_DIR/e"}}},
        __import__("config_saver.lib.utils.path_expander", fromlist=["PathExpander"]).PathExpander(),
    )
    assert expanded["save"]["main"]["location"] == str(fake_home / "saves")
    assert expanded["export"]["main"]["location"] == str(fake_home / ".config" / "e")


def test_get_data_returns_the_expanded_dictionary(tmp_path: Path, fake_home: Path) -> None:
    cfg = tmp_path / "c.yaml"
    cfg.write_text(yaml.safe_dump({"directories": ["$HOME/x"], "normalize_content": True}))
    data = Parser(str(cfg)).get_data()
    assert data["directories"] == [str(fake_home / "x")]
    assert data["normalize_content"] is True
    assert Parser(str(cfg)).get_attr("missing-key") is None


# ---------------------------------------------------------------------- CLI


def test_progress_flag_end_to_end(tmp_path: Path, fake_home: Path) -> None:
    data = fake_home / "d"
    data.mkdir()
    (data / "f.txt").write_text("x")
    cfg = write_config(tmp_path / "c.yaml", [str(data)])
    archive = tmp_path / "a.tar.gz"

    assert CLI(["--compress", "-P", "--input", str(cfg), "--output", str(archive)]).run() == EXIT_OK
    assert CLI(["--decompress", "-P", "--input", str(archive), "--output", str(tmp_path / "out")]).run() == EXIT_OK


def test_jobs_auto_is_accepted(tmp_path: Path, fake_home: Path) -> None:
    data = fake_home / "d"
    data.mkdir()
    (data / "f.txt").write_text("x")
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    write_config(cfg_dir / "one.yaml", [str(data)])

    assert CLI._resolve_jobs("auto") >= 1
    assert CLI(["--compress", "--input", str(cfg_dir), "--jobs", "auto"]).run() == EXIT_OK


def test_batch_failure_exits_runtime(tmp_path: Path, fake_home: Path) -> None:
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    (cfg_dir / "bad.yaml").write_text(yaml.safe_dump({"directoriez": ["/tmp"]}))
    assert CLI(["--compress", "--input", str(cfg_dir)]).run() == EXIT_RUNTIME


def test_long_missing_and_skipped_lists_are_truncated(
    tmp_path: Path, fake_home: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = [str(fake_home / f"missing-{i}") for i in range(15)]
    present = fake_home / "present"
    present.write_text("x")
    cfg = write_config(tmp_path / "c.yaml", [*missing, str(present)])
    monkeypatch.setattr(TarCompressor, "_is_root_owned", lambda _self, _path: True)

    assert CLI(["--compress", "--input", str(cfg), "--output", str(tmp_path / "a.tar.gz")]).run() == EXIT_OK
    out = capsys.readouterr().out
    assert "15 configured path(s) were missing" in out
    assert "and 5 more" in out


def test_export_all_configs_cannot_create_the_destination(
    tmp_path: Path, fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = fake_home / ".config" / "config-saver" / "configs" / "zsh" / "20260101-000000"
    directory.mkdir(parents=True)
    with tarfile.open(directory / "zsh-20260101-000000.tar.gz", "w:gz"):
        pass

    def blocked(*_args, **_kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(os, "makedirs", blocked)
    assert CLI(["--export-all-configs", "--output", str(tmp_path / "nope")]).run() != EXIT_OK


def test_export_all_configs_reports_a_failed_copy(
    tmp_path: Path, fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = fake_home / ".config" / "config-saver" / "configs" / "zsh" / "20260101-000000"
    directory.mkdir(parents=True)
    with tarfile.open(directory / "zsh-20260101-000000.tar.gz", "w:gz"):
        pass

    def blocked(*_args, **_kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr("shutil.copy2", blocked)
    assert CLI(["--export-all-configs", "--output", str(tmp_path / "exports")]).run() == EXIT_PERMISSION


def test_legacy_archive_without_timestamp_is_still_listed(
    tmp_path: Path, fake_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    saves = fake_home / ".config" / "config-saver"
    saves.mkdir(parents=True)
    with tarfile.open(saves / "legacy.tar.gz", "w:gz"):
        pass
    assert CLI(["--list"]).run() == EXIT_OK
    assert "legacy" in capsys.readouterr().out
