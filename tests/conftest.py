"""Shared fixtures for the Íris test suite.

Everything runs against a throwaway config/data/state directory and a fake ACP
agent, so tests never touch your real settings nor spend model quota.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# Must happen before `toad` is imported: paths are resolved from these variables.
_sandbox = Path(tempfile.mkdtemp(prefix="iris-tests-"))
for _name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
    os.environ[_name] = str(_sandbox / _name.lower())

import pytest  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FAKE_AGENT = ROOT / "tools" / "fake_agent.py"


def agent_data(
    name: str = "Claude Code", identity: str = "claude.com", args: str = ""
) -> dict:
    """Agent definition that launches the fake ACP agent (`args` e.g. "--models")."""
    return {
        "identity": identity,
        "name": name,
        "short_name": name.split()[0].lower(),
        "url": "",
        "protocol": "acp",
        "type": "coding",
        "author_name": "",
        "author_url": "",
        "publisher_name": "",
        "publisher_url": "",
        "description": "",
        "tags": [],
        "help": "",
        "run_command": {"*": f'"{sys.executable}" "{FAKE_AGENT}" {args}'.strip()},
        "actions": {},
    }


@pytest.fixture
def spoken(monkeypatch) -> list[str]:
    """Replace the iris-voz process with a recorder of what would have been said.

    Applied to the class, so it's in place before the app starts.
    """
    from toad.iris_voice import IrisVoice

    said: list[str] = []
    original_init = IrisVoice.__init__

    def init(self, app) -> None:
        original_init(self, app)
        self.ready = True

    async def no_sync(self) -> None:
        return None

    monkeypatch.setattr(IrisVoice, "__init__", init)
    monkeypatch.setattr(IrisVoice, "sync", no_sync)
    monkeypatch.setattr(IrisVoice, "say", lambda self, text, cache=False: said.append(text))
    return said


def write_settings(settings: dict) -> None:
    """Pre-seed settings before the app starts."""
    import json

    from toad import paths

    (paths.get_config() / "toad.json").write_text(json.dumps(settings), encoding="utf-8")


@pytest.fixture
def sandbox() -> Path:
    return _sandbox


@pytest.fixture(autouse=True)
def fresh_settings():
    """Each test starts from default settings."""
    from toad import paths

    files = [
        paths.get_config() / "toad.json",
        paths.get_config() / "iris-projetos.json",
        paths.get_state() / "iris-usage.json",
    ]
    for file in files:
        file.unlink(missing_ok=True)
    yield
    for file in files:
        file.unlink(missing_ok=True)


async def wait_until(pilot, predicate, timeout: float = 20.0) -> bool:
    """Pause the pilot until `predicate()` is true (or time runs out)."""
    steps = int(timeout / 0.1)
    for _ in range(steps):
        if predicate():
            return True
        await pilot.pause(0.1)
    return predicate()


@pytest.fixture(autouse=True)
def empty_robot_registry():
    """Hefesto: every test starts without registered controllers."""
    from toad.hefesto_robot import registry_path

    registry_path().unlink(missing_ok=True)
    yield
