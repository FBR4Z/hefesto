"""Model/effort options, the Sharingan orb, Íris projects (with MCP) and disconnects."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from toad.app import ToadApp
from toad.iris_config import (
    EFFORT,
    MODEL,
    by_category,
    find_choice,
    parse_session_options,
)
from toad.iris_projects import (
    Project,
    load_projects,
    new_project,
    parse_mcp_command,
    project_for_path,
    save_projects,
)
from toad.widgets.conversation import Conversation
from toad.widgets.iris_orb import IrisOrb, is_elevated_tool

from conftest import agent_data, wait_until, write_settings

SIZE = (130, 40)

CLAUDE_OPTIONS = {
    "configOptions": [
        {
            "id": "model",
            "name": "Model",
            "category": "model",
            "type": "select",
            "currentValue": "opus",
            "options": [
                {"value": "default", "name": "Default (recommended)"},
                {"value": "opus", "name": "Opus 5.5"},
                {"value": "sonnet", "name": "Sonnet 5"},
                {"value": "haiku", "name": "Haiku 4.5"},
            ],
        },
        {
            "id": "effort",
            "name": "Effort",
            "category": "thought_level",
            "type": "select",
            "currentValue": "default",
            "options": [{"value": "low", "name": "Low"}, {"value": "max", "name": "Max"}],
        },
        {"id": "notes", "name": "Notes", "type": "text"},
    ]
}


async def start_conversation(pilot) -> Conversation:
    app = pilot.app
    assert await wait_until(
        pilot, lambda: app.screen.query_one_optional(Conversation) is not None
    )
    conversation = app.screen.query_one(Conversation)
    assert await wait_until(
        pilot, lambda: conversation.agent_ready and conversation.config_options
    )
    return conversation


async def submit(pilot, conversation: Conversation, text: str) -> None:
    turns = conversation._turn_count
    conversation.prompt.append(text)
    conversation.prompt.prompt_text_area.action_submit()
    assert await wait_until(pilot, lambda: conversation._turn_count > turns)


def agent_log(conversation: Conversation) -> str:
    try:
        return conversation.agent._log_file_path.read_text(encoding="utf-8")
    except OSError:
        return ""


# ---------------------------------------------------------------------- units


def test_parse_claude_config_options():
    options = parse_session_options(CLAUDE_OPTIONS)
    assert [option.id for option in options] == ["model", "effort"]  # text option skipped
    model = by_category(options, MODEL)
    assert model.current_name == "Opus 5.5" and not model.via_models
    assert by_category(options, EFFORT).id == "effort"


def test_parse_gemini_models():
    (model,) = parse_session_options(
        {
            "models": {
                "availableModels": [
                    {"modelId": "auto", "name": "Auto"},
                    {"modelId": "gemini-2.5-pro", "name": "gemini-2.5-pro"},
                ],
                "currentModelId": "auto",
            }
        }
    )
    assert model.via_models and model.category == MODEL and model.current_name == "Auto"
    assert parse_session_options({"sessionId": "x"}) is None


def test_find_choice():
    model = by_category(parse_session_options(CLAUDE_OPTIONS), MODEL)
    assert find_choice(model, "sonnet").value == "sonnet"
    assert find_choice(model, "Haiku 4.5").value == "haiku"
    assert find_choice(model, "HAI").value == "haiku"  # unique prefix
    assert find_choice(model, "5") is None  # ambiguous
    assert find_choice(model, "gpt") is None


def test_elevated_tools():
    assert is_elevated_tool({"_meta": {"claudeCode": {"toolName": "Skill"}}})
    assert is_elevated_tool({"name": "Agent"})
    assert is_elevated_tool({"name": "activate_skill"})
    assert not is_elevated_tool({"_meta": {"claudeCode": {"toolName": "Read"}}, "name": "Read"})


def test_projects_round_trip(tmp_path):
    firmware = tmp_path / "firmware"
    (firmware / "main").mkdir(parents=True)
    notes = tmp_path / "pinagem.md"
    notes.write_text("GPIO", encoding="utf-8")
    other = tmp_path / "outro"
    other.mkdir()

    projects: list[Project] = []
    project = new_project(projects, "Estufa", "Controle da estufa", [firmware, notes])
    project.mcp.append(parse_mcp_command("esp-idf", ["eim", "run", "idf.py mcp-server"]))
    project.mcp.append(parse_mcp_command("docs", ["https://mcp.espressif.com/docs"]))
    new_project(projects, "Geral", "", [tmp_path])
    file = tmp_path / "projetos.json"
    save_projects(projects, file)

    loaded = load_projects(file)
    assert [p.nome for p in loaded] == ["Estufa", "Geral"]
    estufa = loaded[0]
    assert estufa.pastas == [str(firmware.resolve())]
    assert estufa.arquivos == [str(notes.resolve())]
    # The most specific folder wins; a preferred project wins if it matches.
    assert project_for_path(loaded, firmware / "main").nome == "Estufa"
    assert project_for_path(loaded, other).nome == "Geral"
    assert project_for_path(loaded, firmware, preferred="Geral").nome == "Geral"
    assert project_for_path(loaded, Path(tmp_path.anchor)) is None

    assert estufa.mcp_servers() == [
        {"name": "esp-idf", "command": "eim", "args": ["run", "idf.py mcp-server"], "env": []},
        {"type": "http", "name": "docs", "url": "https://mcp.espressif.com/docs", "headers": []},
    ]
    context = estufa.context_prompt()
    assert "Controle da estufa" in context and str(notes.resolve()) in context
    assert "esp-idf, docs" in context


def test_projeto_cli(tmp_path):
    from toad.cli import main
    from toad.iris_projects import find_project

    folder = tmp_path / "estufa"
    folder.mkdir()
    extra = tmp_path / "esquema.pdf"
    extra.write_bytes(b"%PDF")
    runner = CliRunner()
    result = runner.invoke(
        main, ["projeto", "novo", "Estufa", str(folder), "-d", "ESP32 da estufa", "-a", "claude"]
    )
    assert result.exit_code == 0, result.output
    assert runner.invoke(main, ["projeto", "adicionar", "Estufa", str(extra)]).exit_code == 0
    result = runner.invoke(
        main,
        ["projeto", "mcp", "Estufa", "esp-idf", "-e", "IDF_TARGET=esp32s3", "--",
         "eim", "run", "idf.py mcp-server"],
    )
    assert result.exit_code == 0, result.output
    project = find_project(load_projects(), "estufa")
    assert project.arquivos == [str(extra.resolve())] and project.agente == "claude"
    assert project.mcp == [
        {
            "name": "esp-idf",
            "command": "eim",
            "args": ["run", "idf.py mcp-server"],
            "env": {"IDF_TARGET": "esp32s3"},
        }
    ]
    result = runner.invoke(main, ["projeto", "listar"])
    assert "Estufa: ESP32 da estufa [1 pasta(s), 1 arquivo(s), 1 MCP]" in result.output
    runner.invoke(main, ["projeto", "mcp", "Estufa", "esp-idf", "--remover"])
    assert find_project(load_projects(), "Estufa").mcp == []


# ------------------------------------------------------------------------- UI


async def test_model_command_with_config_options():
    app = ToadApp(agent_data=agent_data(), project_dir=".")
    async with app.run_test(size=SIZE) as pilot:
        conversation = await start_conversation(pilot)
        orb = app.screen.query_one(IrisOrb)
        commands = {command.command for command in conversation.prompt.slash_commands}
        assert {"/model", "/modelo", "/esforco", "/modo", "/projeto"} <= commands
        assert await wait_until(pilot, lambda: "Opus 5.5" in orb.render().plain)

        assert await conversation.slash_command("/model sonnet")
        assert await wait_until(
            pilot, lambda: conversation.config_options["model"].current == "sonnet"
        )
        assert await wait_until(pilot, lambda: "Sonnet 5" in orb.render().plain)
        assert await conversation.slash_command("/esforco max")
        assert await wait_until(
            pilot, lambda: conversation.config_options["effort"].current == "max"
        )

        # Without an argument: a list to pick from.
        from toad.widgets.question import Question

        assert await conversation.slash_command("/modelo")
        assert await wait_until(pilot, lambda: app.screen.query(Question))
        question = app.screen.query_one(Question)
        assert await wait_until(
            pilot, lambda: len(question.query_one("#option-container").children) == 4
        )
        question.selection = 2  # opus, sonnet, haiku, Cancelar
        question.action_select()
        assert await wait_until(
            pilot, lambda: conversation.config_options["model"].current == "haiku"
        )


async def test_model_command_with_gemini_models():
    app = ToadApp(agent_data=agent_data("Gemini CLI", "geminicli.com", "--models"), project_dir=".")
    async with app.run_test(size=SIZE) as pilot:
        conversation = await start_conversation(pilot)
        assert conversation.config_options["model"].via_models
        assert await conversation.slash_command("/model 2.5")
        assert await wait_until(
            pilot, lambda: conversation.config_options["model"].current == "gemini-2.5-pro"
        )
        assert await wait_until(pilot, lambda: "session/set_model" in agent_log(conversation))
        # Gemini has no effort option.
        commands = {command.command for command in conversation.prompt.slash_commands}
        assert "/esforco" not in commands


async def test_mode_command():
    app = ToadApp(agent_data=agent_data(), project_dir=".")
    async with app.run_test(size=SIZE) as pilot:
        conversation = await start_conversation(pilot)
        assert await conversation.slash_command("/modo plan")
        assert await wait_until(pilot, lambda: conversation.current_mode.id == "plan")


async def test_sharingan_orb_levels():
    write_settings({"iris": {"orb_theme": "sharingan"}})
    app = ToadApp(agent_data=agent_data(), project_dir=".")
    async with app.run_test(size=SIZE) as pilot:
        conversation = await start_conversation(pilot)
        orb = app.screen.query_one(IrisOrb)
        assert await wait_until(pilot, lambda: "SHARINGAN" in orb.render().plain)
        conversation.current_mode = conversation.modes["plan"]
        assert await wait_until(pilot, lambda: "PLANEJAMENTO" in orb.render().plain)
        conversation.current_mode = conversation.modes["default"]

        # A skill turns it into the Mangekyō until the turn ends.
        conversation.prompt.append("use a skill 2")
        conversation.prompt.prompt_text_area.action_submit()
        assert await wait_until(pilot, lambda: conversation.iris_elevated)
        assert await wait_until(pilot, lambda: "MANGEKYŌ" in orb.render().plain)
        assert await wait_until(pilot, lambda: not conversation.iris_elevated)
        assert await wait_until(pilot, lambda: "SHARINGAN" in orb.render().plain)

        # So does a mode without permissions.
        conversation.current_mode = conversation.modes["bypassPermissions"]
        assert await wait_until(pilot, lambda: "MANGEKYŌ" in orb.render().plain)


def test_every_eye_draws_each_level():
    from textual.color import Color
    from toad.settings_schema import SCHEMA
    from toad.widgets.hefesto_robots import ROBOTS
    from toad.widgets.iris_eyes import EYES, EyeContext

    iris = next(group for group in SCHEMA if group["key"] == "iris")
    theme_field = next(field for field in iris["fields"] if field["key"] == "orb_theme")
    assert {value for _, value in theme_field["choices"]} == {"arco", *EYES, *ROBOTS}
    assert theme_field["default"] == "combinado"
    for eye in EYES.values():
        drawings = []
        for planning, elevation in ((True, 0.0), (False, 0.0), (False, 1.0)):
            context = EyeContext(
                color=Color.parse(eye.plan_color),
                rotation=0.6,
                elapsed=10.0,
                planning=planning,
                elevation=elevation,
                activity=1.0,
            )
            lit = [
                eye.field(context, r / 20, theta / 20 * 6.283) is not None
                for r in range(20)
                for theta in range(20)
            ]
            # Some dots lit, some dark (pupil, rings, veins...).
            assert any(lit) and not all(lit), eye.key
            drawings.append(lit)
        # Each level looks different.
        assert drawings[0] != drawings[1] != drawings[2], eye.key


async def test_project_context_and_mcp(tmp_path):
    folder = tmp_path / "estufa"
    folder.mkdir()
    projects: list[Project] = []
    project = new_project(projects, "Estufa", "Controle da estufa", [folder])
    project.mcp.append(parse_mcp_command("esp-idf", ["eim", "run", "idf.py mcp-server"]))
    save_projects(projects)

    app = ToadApp(agent_data=agent_data(), project_dir=str(folder))
    async with app.run_test(size=SIZE) as pilot:
        conversation = await start_conversation(pilot)
        assert conversation.iris_project.nome == "Estufa"
        assert "[Estufa]" in app.screen.title
        await submit(pilot, conversation, "eco um")
        await submit(pilot, conversation, "eco dois")
        assert await wait_until(pilot, lambda: "eco dois" in agent_log(conversation))
        log = agent_log(conversation)
        # The MCP server went with session/new; the context only with the first prompt.
        new_session = next(line for line in log.splitlines() if "session/new" in line)
        assert "esp-idf" in new_session and "idf.py mcp-server" in new_session
        sent = [line for line in log.splitlines() if line.startswith("[client]")]
        assert sum("Contexto do projeto" in line for line in sent) == 1
        first = next(line for line in log.splitlines() if "eco um" in line and "[client]" in line)
        assert "Controle da estufa" in first


async def test_projeto_slash_creates_project(tmp_path):
    folder = tmp_path / "bancada"
    folder.mkdir()
    (folder / "leia.md").write_text("oi", encoding="utf-8")
    app = ToadApp(agent_data=agent_data(), project_dir=str(folder))
    async with app.run_test(size=SIZE) as pilot:
        conversation = await start_conversation(pilot)
        assert conversation.iris_project is None
        assert await conversation.slash_command("/projeto novo Bancada Testes de bancada")
        assert conversation.iris_project.nome == "Bancada"
        assert await conversation.slash_command("/projeto adicionar leia.md")
    (saved,) = load_projects()
    assert saved.descricao == "Testes de bancada"
    assert saved.pastas == [str(folder.resolve())]
    assert saved.arquivos == [str((folder / "leia.md").resolve())]


async def test_agent_disconnect_is_reported():
    app = ToadApp(agent_data=agent_data("Codex CLI", "openai.com"), project_dir=".")
    async with app.run_test(size=SIZE) as pilot:
        conversation = await start_conversation(pilot)
        conversation.prompt.append("tchau")
        conversation.prompt.prompt_text_area.action_submit()
        assert await wait_until(pilot, lambda: conversation._agent_fail)
        from toad.widgets.markdown_note import MarkdownNote

        assert await wait_until(
            pilot,
            lambda: any(
                "O agente desconectou" in note.source for note in app.screen.query(MarkdownNote)
            ),
        )


def test_terminal_meta_is_collected():
    from toad.acp.agent import merge_terminal_meta
    from toad.widgets.tool_call import elapsed_seconds

    tool_call: dict = {"toolCallId": "b1"}
    merge_terminal_meta(tool_call, {"_meta": {"claudeCode": {}}})
    assert "_iris_terminal" not in tool_call
    for data in ("um\n", "dois\n"):
        merge_terminal_meta(tool_call, {"_meta": {"terminal_output": {"data": data}}})
    merge_terminal_meta(tool_call, {"_meta": {"terminal_exit": {"exit_code": 0}}})
    assert tool_call["_iris_terminal"] == {"output": "um\ndois\n", "exit_code": 0}

    progress = {"_meta": {"claudeCode": {"toolResponse": {"elapsedTimeSeconds": 185.4}}}}
    assert elapsed_seconds(progress) == "3min 05s"
    assert elapsed_seconds({"_meta": {"claudeCode": {"toolResponse": {"elapsedTimeSeconds": 42}}}}) == "42s"
    assert elapsed_seconds({}) == ""


async def test_agent_command_output_is_shown():
    from toad.widgets.tool_call import TerminalOutput, ToolCall, ToolCallHeader

    app = ToadApp(agent_data=agent_data(), project_dir=".")
    async with app.run_test(size=SIZE) as pilot:
        conversation = await start_conversation(pilot)
        assert "'_meta': {'terminal_output': True}" in agent_log(conversation)
        conversation.prompt.append("rode um comando")
        conversation.prompt.prompt_text_area.action_submit()

        def header() -> str:
            tool_call = app.screen.query_one_optional(ToolCall)
            found = tool_call and tool_call.query_one_optional(ToolCallHeader)
            return found.render().plain if found else ""

        assert await wait_until(pilot, lambda: "⏱ 1min 15s" in header())
        assert await wait_until(pilot, lambda: "(código 2)" in header())
        assert "⏱" not in header()
        output = app.screen.query_one(ToolCall).query_one(TerminalOutput)
        text = output.render().plain
        assert "Connecting...." in text and "A fatal error occurred" in text
        assert "\x1b" not in text and "\r" not in text


def test_every_robot_theme_draws_each_phase():
    """Hefesto: each robotic theme draws something different in each AI phase."""
    from toad.widgets.hefesto_robots import ROBOTS, RobotStatus
    from toad.widgets.iris_orb import IrisOrb, OrbState

    states = (
        OrbState(connected=True),
        OrbState(connected=True, busy=True, planning=True),
        OrbState(connected=True, busy=True, planning=False),
        OrbState(connected=True, asking=True),
        OrbState(connected=True, failed=True),
    )

    def draw(theme, state, elapsed=10.0):
        orb = IrisOrb()
        orb._elapsed = elapsed
        orb._angle = elapsed * 0.4
        orb._state = state
        orb._activity = 1.0 if state.busy else 0.0
        return orb._render_robot(36, 9, theme)

    for theme in ROBOTS.values():
        drawings = []
        for state in states:
            text = draw(theme, state)
            assert len(text.plain.splitlines()) == 9
            assert text.plain.strip(), (theme.key, state)
            drawings.append(repr(text.spans) + text.plain)
        # Color and/or shape change with the phase.
        assert len(set(drawings)) > 1, theme.key
        # Any widget size works (the sidebar can be narrow or wide).
        for width, rows in ((12, 4), (60, 14)):
            orb = IrisOrb()
            orb._state = states[0]
            assert len(orb._render_robot(width, rows, theme).plain.splitlines()) == rows

    # The arm themes and the stack light follow a connected controller.
    from toad.widgets.hefesto_robots import RobotContext, andon_light, arm_pose
    from textual.color import Color

    def context(robot, phase="repouso"):
        return RobotContext(Color(255, 255, 255), phase, 10.0, 0.0, 0.0, 0.0, False, False, robot)

    robot = RobotStatus(connected=True, opmode="AUTO", motors="MotorsOn", running=True, joints=(0, 10, -20, 0, 30, 0))
    assert arm_pose(context(robot))[0] == (80, -70, -30)
    assert andon_light(context(robot))[0] == "verde"
    assert andon_light(context(RobotStatus(connected=True, opmode="Manual", motors="MotorsOff")))[0] == "amarelo"
    assert andon_light(context(RobotStatus(connected=True, motors="EmergencyStop")))[0] == "vermelho"
    assert andon_light(context(robot, "confirmacao"))[0] == "azul"
    assert andon_light(context(None, "pensando"))[0] == "azul"
