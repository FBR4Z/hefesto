"""Hefesto: RAPID checks, risk review, taught points, history and the /rapid flow."""

from __future__ import annotations

import json

import pytest

from toad import hefesto_commands
from toad.app import ToadApp
from toad.hefesto_rapid import (
    History,
    check_syntax,
    insert_declaration,
    review_change,
    review_markdown,
    robtarget_declaration,
    templates,
)
from toad.hefesto_robot import Controller, Registry, apply_event, save_registry
from toad.widgets.hefesto_robots import RobotStatus

from conftest import agent_data, wait_until
from test_iris_features import SIZE, start_conversation

MODULE = """MODULE Celula
    CONST robtarget pPega:=[[100,0,50],[0,1,0,0],[0,0,0,0],[9E9,9E9,9E9,9E9,9E9,9E9]];
    PROC main()
        ! pegar
        MoveJ pPega,v1000,z50,tool0;
        MoveL pPega,v200,fine,tool0;
        SetDO doGarra,1;
        WaitTime 0.5;
    ENDPROC
ENDMODULE
"""


def test_syntax():
    assert check_syntax(MODULE) == []
    assert check_syntax(MODULE.replace("    ENDPROC\n", "")) == ["linha 3: PROC sem ENDPROC"]
    assert check_syntax(MODULE.replace("MoveL pPega,v200", "MoveL Offs(pPega,0,0,10,v200")) == ["linha 6: '(' sem fechar"]
    assert check_syntax('MODULE A\n PROC b()\n  TPWrite "oi;\n ENDPROC\nENDMODULE') == ["linha 3: aspas sem fechar"]
    assert check_syntax("MODULE A\n PROC b()\n  IF x THEN\n   y:=1;\n ENDPROC\nENDMODULE") == ["linha 3: IF sem ENDIF"]
    # Comments and strings don't count.
    assert check_syntax('MODULE A\n ! IF sem fim (\n PROC b()\n  TPWrite "ENDPROC (";\n ENDPROC\nENDMODULE') == []


def test_templates_are_valid():
    found = templates()
    assert {"pega_e_coloca", "paletizacao", "tratamento_erro", "handshake_clp"} <= set(found)
    for path in found.values():
        assert check_syntax(path.read_text(encoding="utf-8")) == [], path.name


def test_review_flags_what_moves_the_robot():
    new = (MODULE.replace("v200,fine", "vmax,z10")
           .replace("[[100,0,50]", "[[130,40,50]")
           .replace("        WaitTime 0.5;\n", "")
           .replace("        ! pegar\n", "        ! pegar a peça\n"))
    texts = [risk.text for risk in review_change(MODULE, new)]
    assert "robtarget pPega alterado (desloca 50.0 mm)" in texts
    assert "velocidade v200 → vmax" in texts
    assert "zona fine → z10" in texts
    assert any(text.startswith("removido: WaitTime") for text in texts)
    assert not any("pegar" in text for text in texts)  # a comment is not a risk
    assert review_change(MODULE, MODULE.replace("! pegar", "! apanhar")) == []
    markdown = review_markdown("Celula", MODULE, new)
    assert "🔴" in markdown and "```diff" in markdown and "+        MoveL pPega,vmax,z10,tool0;" in markdown


def test_review_new_moves_tools_and_settings():
    new = MODULE.replace(
        "        SetDO doGarra,1;\n",
        "        MoveL Offs(pPega,0,0,100),v500,z20,tool0;\n        AccSet 50,50;\n        SetDO doGarra,0;\n",
    ).replace("MoveJ pPega,v1000,z50,tool0;", "MoveJ pPega,v1000,z50,tGarra;")
    texts = [risk.text for risk in review_change(MODULE, new)]
    assert any(text.startswith("movimento novo: MoveL Offs") for text in texts)
    assert any(text.startswith("ajuste de movimento: AccSet") for text in texts)
    assert any(text.startswith("ferramenta/obra tool0 → tGarra") for text in texts)


def test_points():
    declaration = robtarget_declaration("pNovo", "[[1, 2, 3],[1,0,0,0],[0,0,0,0],[9E9,9E9,9E9,9E9,9E9,9E9]]", "tGarra", "wMesa")
    assert declaration.strip().startswith("CONST robtarget pNovo:=[[1,2,3],")
    assert "tool tGarra, wobj wMesa" in declaration
    source = insert_declaration(MODULE, declaration, "pNovo")
    lines = source.splitlines()
    assert lines[2].strip().startswith("CONST robtarget pNovo")  # after the last declaration
    assert check_syntax(source) == []
    again = insert_declaration(source, declaration.replace("[[1,2,3]", "[[9,9,9]"), "pNovo")
    assert again.count("pNovo:=") == 1 and "[[9,9,9]" in again
    with pytest.raises(ValueError):
        robtarget_declaration("1ruim", "[[0,0,0]]", "tool0", "wobj0")


def test_history(tmp_path):
    history = History(tmp_path / "celula3")
    first = history.save("Celula", MODULE, "Lido")
    assert first and history.save("Celula", MODULE, "Igual") is None
    second = history.save("Celula", MODULE.replace("v200", "v300"), "Aplicado")
    log = history.log("Celula")
    assert [entry[0] for entry in log] == [second, first]
    assert history.show("Celula", first) == MODULE


def test_rapid_highlighting():
    from textual.highlight import highlight

    from toad.hefesto_rapid import register_lexer

    register_lexer()
    text = highlight("MoveL pPega,v200,fine,tool0; ! comentário", language="rapid")
    assert len(text.spans) > 3


def test_monitor_pointer_io_cycles_and_errors():
    status = RobotStatus()
    apply_event(status, {"type": "snapshot", "pp": "MainModule/main:33", "task": "T_ROB1"}, now=0)
    assert status.pp == "MainModule/main:33"
    # Cycle by program start → stop.
    apply_event(status, {"type": "exec", "value": "Running"}, now=10)
    apply_event(status, {"type": "exec", "value": "Stopped"}, now=22.5)
    assert status.cycles == [12.5]
    # Cycle by a signal's rising edges.
    status = RobotStatus()
    for when, value in ((0, "0"), (1, "1"), (2, "0"), (9, "1"), (10, "0"), (17.5, "1")):
        apply_event(status, {"type": "io", "name": "DO_CICLO", "value": value}, now=when, cycle_signal="DO_CICLO")
    assert status.cycles == [8, 8.5]
    apply_event(status, {"type": "pp", "value": "Celula/main:6"})
    apply_event(status, {"type": "log", "severity": "error", "code": "1-50024", "title": "Falha", "body": "Causa"})
    assert status.last_error == {"code": "1-50024", "title": "Falha", "body": "Causa", "pp": "Celula/main:6"}


def test_voice_robot_commands():
    from toad.iris_commands import parse_command

    command = parse_command("Vulcano, status da célula 3")
    assert command.name == "robo_status" and "3" in command.argument
    assert parse_command("Vulcano, qual foi o último erro?").name == "robo_erro"
    assert parse_command("Vulcano, tempo de ciclo").name == "robo_ciclo"
    assert parse_command("Vulcano, status").name == "status"  # Íris' own status still works


class FakeAbbctl:
    """Stands in for `abbctl`: the module source, loads and the TCP position."""

    def __init__(self):
        self.modules = {"Celula": MODULE}
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    async def __call__(self, registry, controller, *args, timeout=120):
        self.calls.append((controller.apelido, args))
        if args[:2] == ("prog", "cat"):
            return 0, self.modules[args[2]], ""
        if args[:2] == ("prog", "load"):
            from pathlib import Path

            source = Path(args[2]).read_text(encoding="utf-8")
            self.modules["Celula"] = source
            return 0, f"loaded {args[2]} into T_ROB1 (Replace)", ""
        if args[:2] == ("pos", "--rapid"):
            return 0, json.dumps({"tool": "tool0", "wobj": "wobj0",
                                  "robtarget": "[[364.35,0,594],[0.5,0,0.866,0],[0,0,0,0],[9E9,9E9,9E9,9E9,9E9,9E9]]"}), ""
        return 1, "", "não simulado"


async def press(pilot, button_id: str) -> None:
    from textual.widgets import Button

    from toad.hefesto_commands import ConfirmModal

    app = pilot.app
    assert await wait_until(pilot, lambda: isinstance(app.screen, ConfirmModal))
    modal = app.screen
    modal.query_one(f"#{button_id}", Button).press()
    # Another confirmation may follow right away (twin, then the real one).
    assert await wait_until(pilot, lambda: app.screen is not modal)


async def test_rapid_flow_ai_edits_human_approves(tmp_path, monkeypatch):
    fake = FakeAbbctl()
    monkeypatch.setattr(hefesto_commands, "run_abbctl", fake)
    monkeypatch.setattr(hefesto_commands, "_open_in_editor", lambda path: None)
    registry = Registry()
    registry.add(Controller("celula3", "10.0.0.5", real=True))
    registry.add(Controller("gemeo3", "SIS_GEMEO", real=False))
    registry.find("celula3").gemeo = "gemeo3"
    save_registry(registry)

    app = ToadApp(agent_data=agent_data(), project_dir=str(tmp_path))
    async with app.run_test(size=SIZE) as pilot:
        conversation = await start_conversation(pilot)
        await conversation.slash_command("/rapid editar celula3 Celula ia")
        session = conversation.hefesto_edit
        assert session is not None and session.mode == "ia"
        assert "Edite a cópia de trabalho" in conversation.prompt.prompt_text_area.text
        conversation.prompt.prompt_text_area.text = ""

        # Applying before a review is refused.
        await conversation.slash_command("/rapid aplicar")
        await pilot.pause(0.3)
        assert not any(call[1][:2] == ("prog", "load") for call in fake.calls)

        # The "AI" edits; a taught point goes in too.
        session.path.write_text(MODULE.replace("v200,fine", "v100,fine"), encoding="utf-8")
        await conversation.slash_command("/ponto pNovo")
        assert "pNovo:=" in session.path.read_text(encoding="utf-8")
        await conversation.slash_command("/rapid revisar")
        await conversation.slash_command("/rapid aplicar")
        await press(pilot, "sim")  # the twin took it: go on to the real one
        await press(pilot, "sim")  # load on the REAL controller
        assert await wait_until(pilot, lambda: sum(c[1][:2] == ("prog", "load") for c in fake.calls) == 2)
        loads = [call for call in fake.calls if call[1][:2] == ("prog", "load")]
        assert [call[0] for call in loads] == ["gemeo3", "celula3"]  # twin first
        assert all("--replace" in call[1] for call in loads)
        assert "v100,fine" in fake.modules["Celula"]
        history = hefesto_commands._history(registry.find("celula3")).log("Celula")
        assert "humano aprovou" in history[0][2]

        # And back: the version read before editing.
        await conversation.slash_command(f"/rapid voltar {history[-1][0]}")
        await press(pilot, "sim")
        assert await wait_until(pilot, lambda: fake.modules["Celula"] == MODULE)


async def test_rapid_flow_human_edits_ai_must_approve(tmp_path, monkeypatch):
    fake = FakeAbbctl()
    monkeypatch.setattr(hefesto_commands, "run_abbctl", fake)
    monkeypatch.setattr(hefesto_commands, "_open_in_editor", lambda path: None)
    registry = Registry()
    registry.add(Controller("bancada", "SIS_BANCADA", real=False))
    save_registry(registry)

    app = ToadApp(agent_data=agent_data(), project_dir=str(tmp_path))
    async with app.run_test(size=SIZE) as pilot:
        conversation = await start_conversation(pilot)
        await conversation.slash_command("/rapid editar bancada Celula eu")
        session = conversation.hefesto_edit
        session.path.write_text(MODULE.replace("v1000", "v500"), encoding="utf-8")
        turns = conversation._turn_count
        await conversation.slash_command("/rapid revisar")
        # The review went to the AI; the fake agent echoes it, so its last verdict
        # word is "REPROVADO" (from the instructions): nothing may be loaded.
        assert await wait_until(pilot, lambda: conversation._turn_count > turns)
        await conversation.slash_command("/rapid aplicar")
        await pilot.pause(0.5)
        assert not any(call[1][:2] == ("prog", "load") for call in fake.calls)

        # An approval from the AI unlocks it.
        monkeypatch.setattr(hefesto_commands, "_last_agent_text", lambda conversation: "Ok.\nVEREDITO: APROVADO")
        await conversation.slash_command("/rapid aplicar")
        await press(pilot, "sim")
        assert await wait_until(pilot, lambda: "v500" in fake.modules["Celula"])
