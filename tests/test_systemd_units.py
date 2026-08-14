"""The shipped systemd units must be valid for the manager they target."""

from __future__ import annotations

import configparser
from pathlib import Path

import pytest

CONTRIB = Path(__file__).resolve().parent.parent / "contrib" / "systemd"


def _unit(path: Path) -> configparser.RawConfigParser:
    parser = configparser.RawConfigParser(strict=False)
    parser.optionxform = str  # systemd keys are case-sensitive
    parser.read(path, encoding="utf-8")
    return parser


@pytest.mark.parametrize(
    "path",
    sorted(CONTRIB.rglob("*.service")) + sorted(CONTRIB.rglob("*.timer")),
    ids=lambda p: str(p.relative_to(CONTRIB)),
)
def test_units_parse(path: Path) -> None:
    assert _unit(path).sections()


def test_user_service_is_a_real_user_unit() -> None:
    """A user unit may not set User=, and %i is empty in a non-template unit."""
    unit = _unit(CONTRIB / "user" / "config-saver.service")
    assert not unit.has_option("Service", "User")
    assert not unit.has_option("Service", "Environment=HOME")
    text = (CONTRIB / "user" / "config-saver.service").read_text()
    assert "%i" not in text
    assert "/home/" not in text
    assert unit.get("Install", "WantedBy") == "default.target"


def test_user_timer_points_at_the_user_service() -> None:
    unit = _unit(CONTRIB / "user" / "config-saver.timer")
    assert unit.get("Timer", "Unit") == "config-saver.service"


def test_system_template_targets_the_instance() -> None:
    service = _unit(CONTRIB / "system" / "config-saver@.service")
    assert service.get("Service", "User") == "%i"
    timer = _unit(CONTRIB / "system" / "config-saver@.timer")
    assert timer.get("Timer", "Unit") == "config-saver@%i.service"
    assert timer.get("Install", "WantedBy") == "timers.target"


@pytest.mark.parametrize(
    "path",
    [CONTRIB / "user" / "config-saver.timer", CONTRIB / "system" / "config-saver@.timer"],
    ids=["user", "system"],
)
def test_timers_are_actually_recurring(path: Path) -> None:
    """OnActiveSec= fires once, 3h after activation: it is not a daily schedule."""
    timer = _unit(path)
    assert not timer.has_option("Timer", "OnActiveSec")
    assert timer.get("Timer", "OnCalendar") == "*-*-* 03:00:00"
    assert timer.get("Timer", "Persistent") == "true"


@pytest.mark.parametrize(
    "path",
    [CONTRIB / "user" / "config-saver.timer", CONTRIB / "system" / "config-saver@.timer"],
    ids=["user", "system"],
)
def test_a_missed_backup_runs_immediately(path: Path) -> None:
    """Persistent= catches up a backup the machine slept through, but a
    randomized delay or the default one-minute accuracy would make that
    catch-up wait. Both are pinned to zero on purpose."""
    timer = _unit(path)
    assert timer.get("Timer", "RandomizedDelaySec") == "0"
    assert timer.get("Timer", "AccuracySec") == "1s"


@pytest.mark.parametrize(
    "path",
    sorted(CONTRIB.rglob("*.service")),
    ids=lambda p: str(p.relative_to(CONTRIB)),
)
def test_services_create_private_files(path: Path) -> None:
    assert _unit(path).get("Service", "UMask") == "0077"
