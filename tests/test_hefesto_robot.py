"""Hefesto: controller registry, live monitor, MCP servers per controller, agent context."""

from __future__ import annotations

import sys
import textwrap

from click.testing import CliRunner

from toad import hefesto_robot
from toad.app import ToadApp
from toad.hefesto_robot import (
    Controller,
    Registry,
    apply_event,
    context_prompt,
    load_registry,
    mcp_servers,
    save_registry,
)
from toad.widgets.hefesto_robots import RobotStatus

from conftest import agent_data, wait_until
from test_iris_features import SIZE, agent_log, start_conversation, submit

SNAPSHOT = {
    "type": "snapshot", "systemName": "120-510158_1", "ip": "127.0.0.1", "virtual": True,
    "robotwareVersion": "6.15.0.8", "operatingMode": "Auto", "state": "MotorsOn",
    "executionStatus": "Stopped", "speedRatio": 100, "robot": "ROB_1",
    "joints": [0, 0, 0, 0, 30, 0],
}


def test_registry_round_trip(tmp_path):
    path = tmp_path / "controladores.json"
    registry = Registry(abbctl="C:/x/abbctl.exe")
    registry.add(Controller("celula3", "192.168.125.1", real=True, descricao="IRB 6700"))
    registry.add(Controller("bancada", "127.0.0.1", real=False))
    save_registry(registry, path)
    loaded = load_registry(path)
    assert [c.apelido for c in loaded.controladores] == ["celula3", "bancada"]
    assert loaded.find("CELULA3").real and not loaded.find("celula3").writable
    assert loaded.find("bancada").writable  # virtual: always
    assert loaded.abbctl == "C:/x/abbctl.exe"
    assert [c.apelido for c in loaded.select(["bancada"])] == ["bancada"]
    assert len(loaded.select([])) == 2


def test_apply_events_and_context():
    status = RobotStatus()
    apply_event(status, SNAPSHOT, now=1.0)
    assert status.connected and status.opmode == "Auto" and status.speed == 100
    assert status.joints == (0, 0, 0, 0, 30, 0)
    apply_event(status, {"type": "exec", "value": "Running"})
    apply_event(status, {"type": "joints", "value": [10, 20, -5, 0, 45, 0]})
    apply_event(status, {"type": "log", "severity": "error", "code": "1-50024", "title": "Corner path failure"}, now=2.0)
    assert status.running and status.joints[1] == 20 and status.events == [(2.0, "error")]

    real = Controller("celula3", "192.168.125.1", real=True)
    text = context_prompt([real], {"celula3": status})
    assert "celula3 · REAL" in text and "programa rodando" in text and "velocidade 100%" in text
    assert "SOMENTE LEITURA" in text and "1-50024 Corner path failure" in text
    # Declared real, but the controller says it's virtual: the agent is warned.
    assert "cadastrado como REAL" in text

    apply_event(status, {"type": "disconnected", "value": "x"})
    assert not status.connected


def test_mcp_server_per_controller(tmp_path):
    tool = tmp_path / "abbctl-mcp.exe"
    tool.write_text("")
    registry = Registry(abbctl_mcp=str(tool))
    registry.add(Controller("celula3", "192.168.125.1", real=True))
    registry.add(Controller("bancada", "127.0.0.1", real=False))
    servers = {server["name"]: server for server in mcp_servers(registry, registry.controladores)}
    assert set(servers) == {"abb-celula3", "abb-bancada"}
    env = {item["name"]: item["value"] for item in servers["abb-celula3"]["env"]}
    assert env == {"ABBCTL_CONTROLLER": "192.168.125.1", "ABBCTL_READONLY": "1",
                   "ABBCTL_LOCKED": "1", "HEFESTO_REAL": "1"}
    env = {item["name"]: item["value"] for item in servers["abb-bancada"]["env"]}
    assert env["ABBCTL_READONLY"] == "0" and env["HEFESTO_REAL"] == "0"


def test_cli_registry():
    runner = CliRunner()
    from toad.cli import main

    result = runner.invoke(main, ["robo", "adicionar", "bancada", "127.0.0.1"])
    assert result.exit_code != 0 and "--real ou --virtual" in result.output
    assert runner.invoke(main, ["robo", "adicionar", "celula3", "10.0.0.5", "--real"]).exit_code == 0
    assert not load_registry().find("celula3").escrita
    result = runner.invoke(main, ["robo", "escrita", "celula3", "liberar"], input="s\n")
    assert result.exit_code == 0 and load_registry().find("celula3").escrita
    assert runner.invoke(main, ["robo", "escrita", "celula3", "bloquear"]).exit_code == 0
    assert "celula3" in runner.invoke(main, ["robo", "listar"]).output
    assert runner.invoke(main, ["robo", "remover", "celula3"]).exit_code == 0
    assert load_registry().controladores == []


async def test_monitor_feeds_ui_context_and_mcp(tmp_path, monkeypatch):
    """A fake `abbctl monitor` feeds the panel, the stack light and the first prompt."""
    script = tmp_path / "fake_monitor.py"
    script.write_text(textwrap.dedent(f"""
        import json, sys, time
        print(json.dumps({SNAPSHOT!r}), flush=True)
        print(json.dumps({{"type": "exec", "value": "Running"}}), flush=True)
        time.sleep(60)
    """), encoding="utf-8")
    monkeypatch.setattr(hefesto_robot, "monitor_command", lambda registry, c: [sys.executable, str(script)])
    tool = tmp_path / "abbctl-mcp.exe"
    tool.write_text("")
    registry = Registry(abbctl_mcp=str(tool))
    registry.add(Controller("bancada", "127.0.0.1", real=False, descricao="estação de testes"))
    save_registry(registry)

    folder = tmp_path / "celula"
    folder.mkdir()
    app = ToadApp(agent_data=agent_data(), project_dir=str(folder))
    async with app.run_test(size=SIZE) as pilot:
        conversation = await start_conversation(pilot)
        status = app.hefesto_monitor.statuses["bancada"]
        assert await wait_until(pilot, lambda: status.connected and status.running, timeout=20)
        assert app.hefesto_robot_status is status
        from toad.widgets.hefesto_panel import RobotPanel

        panel = app.screen.query_one(RobotPanel)
        assert await wait_until(pilot, lambda: "rodando" in panel.render().plain)
        assert "bancada" in panel.render().plain and "VIRTUAL" in panel.render().plain

        await submit(pilot, conversation, "eco oi")
        log = agent_log(conversation)
        new_session = next(line for line in log.splitlines() if "session/new" in line)
        assert "abb-bancada" in new_session and "ABBCTL_LOCKED" in new_session
        first = next(line for line in log.splitlines() if "eco oi" in line and "[client]" in line)
        assert "Contexto dos robôs ABB" in first and "bancada · VIRTUAL" in first
        assert "programa rodando" in first
    assert all(process.returncode is not None for process in app.hefesto_monitor._processes)
