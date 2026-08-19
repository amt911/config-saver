"""The examples the package ships must not contradict the package.

`--include-system-configs` exists so that `/etc/config-saver/configs` — the
system level, which on a machine driven by a declarative installer is written
BY that installer — stays out of an archive unless somebody asks for it. An
example that lists the directory outright hands it back by the front door, and
examples are what people copy.
"""

from pathlib import Path

import pytest
import yaml

EXAMPLES = sorted((Path(__file__).resolve().parents[1] / "configs").glob("*.yaml"))
SYSTEM_LEVEL = ("$ETC_CONFIG_DIR", "/etc/config-saver/configs")


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda p: p.name)
def test_no_example_archives_the_system_config_level(example):
    entries = yaml.safe_load(example.read_text()).get("directories") or []
    listed = []
    for entry in entries:
        if isinstance(entry, str):
            listed.append(entry)
        elif isinstance(entry, dict) and "source" in entry:
            listed.append(str(entry["source"]))

    offenders = [d for d in listed if any(d.startswith(s) for s in SYSTEM_LEVEL)]
    assert not offenders, f"{example.name} archives {offenders}, which --include-system-configs is supposed to gate"
