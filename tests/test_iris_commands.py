"""Voice commands: parsing (pure) and execution in the UI (fake agent)."""

import pytest

from toad.app import ToadApp
from toad.iris_commands import Command, normalize, parse_command, strip_wake_word
from toad.widgets.conversation import Conversation

from conftest import agent_data, wait_until, write_settings

SIZE = (130, 40)

IRIS_SPELLINGS = ("iris", "ires", "iriz", "eris", "irish", "isis", "iiris")


@pytest.fixture(autouse=True)
def iris_name(monkeypatch):
    """These transcripts say "Íris": keep that name even in forks that use another."""
    import toad
    import toad.iris_commands

    for module in (toad, toad.iris_commands):
        monkeypatch.setattr(module, "WAKE_NAME", "Íris")
    monkeypatch.setattr(toad, "WAKE_SPELLINGS", IRIS_SPELLINGS)
    monkeypatch.setattr(toad.iris_commands, "WAKE_WORDS", IRIS_SPELLINGS)


def test_normalize():
    assert normalize("Íris, Modo de Execução!") == "iris modo de execucao"


@pytest.mark.parametrize(
    "spoken, expected",
    [
        ("Íris, modo planejamento.", Command("modo_planejamento")),
        ("Iris modo de planejamento", Command("modo_planejamento")),
        ("Íris, vamos planejar", Command("modo_planejamento")),
        ("Íris, modo de execução", Command("modo_execucao")),
        ("Íris, sai do planejamento", Command("modo_execucao")),
        ("Íris, modo normal", Command("modo_execucao")),
        ("Íris, abre o Gemini", Command("abrir", "geminicli.com")),
        ("Íris, abrir o Claude.", Command("abrir", "claude.com")),
        ("Íris, abre o cloud", Command("abrir", "claude.com")),  # Whisper mishearing
        ("Íris, usar o Codex", Command("abrir", "openai.com")),
        ("Íris, nova sessão", Command("nova_sessao")),
        ("Íris, abre uma sessão", Command("nova_sessao")),
        ("Íris, próxima sessão", Command("proxima_sessao")),
        ("Íris, sessão anterior", Command("sessao_anterior")),
        ("Íris, fechar sessão", Command("fechar")),
        ("Íris, fecha essa sessão", Command("fechar")),
        ("Íris, pode mandar", Command("enviar")),
        ("Íris, envia", Command("enviar")),
        ("Íris, apaga o texto", Command("limpar")),
        ("Íris, para.", Command("cancelar")),
        ("Íris, cancela tudo", Command("cancelar")),
        ("Íris, para de falar", Command("silencio")),
        ("Íris, silêncio", Command("silencio")),
        ("Íris, repete", Command("repetir")),
        ("Íris, que horas são?", Command("horas")),
        ("Íris, status", Command("status")),
        ("Íris, o que você está fazendo?", Command("status")),
        ("Íris, ajuda", Command("ajuda")),
        ("Íris, sair", Command("sair")),
        ("Ei Íris, modo planejamento", Command("modo_planejamento")),
        ("Íris.", Command("vazio")),
        ("Íris, quais são os meus projetos?", Command("listar_projetos")),
        ("Íris, lista os projetos", Command("listar_projetos")),
        ("Íris, abre o projeto Mestrado.", Command("abrir_projeto", "mestrado")),
        ("Íris, vai pro projeto OT Modbus", Command("abrir_projeto", "ot modbus")),
        ("Íris, cria um projeto chamado Irrigação.", Command("criar_projeto", "Irrigação")),
        ("Íris, criar novo projeto Estufa da horta", Command("criar_projeto", "Estufa da horta")),
        ("Íris, em que projeto eu estou?", Command("projeto_atual")),
        ("Íris, qual é o projeto atual", Command("projeto_atual")),
    ],
)
def test_parse_command(spoken, expected):
    assert parse_command(spoken) == expected


def test_unrecognized_command_keeps_text():
    assert parse_command("Íris, refatora o main.py") == Command(
        "desconhecido", "refatora o main py"
    )


@pytest.mark.parametrize(
    "spoken",
    [
        "Refatora o main.py e roda os testes",
        "Preciso de ajuda com o modo de planejamento",  # no wake word: plain dictation
        "",
    ],
)
def test_plain_dictation_is_not_a_command(spoken):
    assert parse_command(spoken) is None


@pytest.mark.parametrize(
    "spoken, loose, strict",
    [
        ("Íris, abre o Claude", "abre o claude", "abre o claude"),
        ("É, Íris, abre o Claude", "abre o claude", None),  # filler only after the wake word
        ("Ó Íres, que horas são?", "que horas sao", None),
        ("Então, Irís.", "", None),
        ("Eu vi a íris do olho dele.", None, None),
        ("Isso não é nada", None, None),
    ],
)
def test_strip_wake_word(spoken, loose, strict):
    assert strip_wake_word(spoken, loose=True) == loose
    assert strip_wake_word(spoken) == strict


# ------------------------------------------------------------------------ UI


async def start(pilot) -> Conversation:
    app = pilot.app
    assert await wait_until(pilot, lambda: app.screen.query_one_optional(Conversation) is not None)
    conversation = app.screen.query_one(Conversation)
    assert await wait_until(pilot, lambda: conversation.agent_ready and conversation.modes)
    return conversation


async def test_voice_commands_in_conversation(spoken):
    write_settings({"voz": {"modo": "ditado_avisos"}})
    app = ToadApp(agent_data=agent_data(), project_dir=".")
    async with app.run_test(size=SIZE) as pilot:
        conversation = await start(pilot)
        voice = app.iris_voice

        voice.handle_transcript("Íris, modo planejamento.", conversation)
        assert await wait_until(pilot, lambda: conversation.current_mode.id == "plan")
        assert await wait_until(pilot, lambda: "Modo de planejamento." in spoken)

        voice.handle_transcript("Íris, modo de execução", conversation)
        assert await wait_until(pilot, lambda: conversation.current_mode.id == "default")

        voice.handle_transcript("Íris, status", conversation)
        assert await wait_until(
            pilot, lambda: "Claude Code está aguardando você, no modo Default." in spoken
        )

        # Plain dictation goes to the prompt; a command can then clear it.
        voice.handle_transcript("explica o main.py", conversation)
        assert conversation.prompt.prompt_text_area.text == "explica o main.py"
        voice.handle_transcript("Íris, apaga o texto", conversation)
        assert await wait_until(pilot, lambda: conversation.prompt.prompt_text_area.text == "")

        # Unrecognized command: the words aren't lost.
        voice.handle_transcript("Íris, refatora o main", conversation)
        assert conversation.prompt.prompt_text_area.text == "refatora o main"


async def test_voice_command_sends_prompt(spoken):
    write_settings({"voz": {"modo": "ditado_avisos"}})
    app = ToadApp(agent_data=agent_data(), project_dir=".")
    async with app.run_test(size=SIZE) as pilot:
        conversation = await start(pilot)
        voice = app.iris_voice
        voice.handle_transcript("olá", conversation)
        voice.handle_transcript("Íris, pode mandar", conversation)
        assert await wait_until(pilot, lambda: conversation._turn_count >= 1)


async def test_voice_commands_can_be_disabled(spoken):
    write_settings({"voz": {"modo": "ditado_avisos", "comandos": False}})
    app = ToadApp(agent_data=agent_data(), project_dir=".")
    async with app.run_test(size=SIZE) as pilot:
        conversation = await start(pilot)
        app.iris_voice.handle_transcript("Íris, modo planejamento", conversation)
        await pilot.pause(0.3)
        assert conversation.current_mode.id == "default"
        assert conversation.prompt.prompt_text_area.text == "Íris, modo planejamento"


async def test_wake_word_transcripts(spoken):
    """After the wake word, Whisper confirms: only text starting with "Íris" counts."""
    write_settings({"voz": {"modo": "ditado_avisos", "ativacao": True}})
    app = ToadApp(agent_data=agent_data(), project_dir=".")
    async with app.run_test(size=SIZE) as pilot:
        conversation = await start(pilot)
        voice = app.iris_voice
        text_area = conversation.prompt.prompt_text_area

        # The wake event aims the transcript at the open conversation.
        voice._handle_event({"event": "wake"})
        voice._handle_event(
            {"event": "transcript", "text": "Íris, modo planejamento.", "wake": True}
        )
        assert await wait_until(pilot, lambda: conversation.current_mode.id == "plan")

        # False alarm: Vosk heard "íris", but the sentence isn't addressed to her.
        voice._handle_event({"event": "wake"})
        voice._handle_event(
            {"event": "transcript", "text": "Eu vi a íris do olho dele.", "wake": True}
        )
        await pilot.pause(0.2)
        assert text_area.text == ""
        # ...but it says what it heard instead of dropping it silently.
        assert any("Ouvi “Eu vi a íris" in str(n.message) for n in app._notifications)

        # Filler words first and a misspelled name still count after the wake word.
        voice._handle_event({"event": "wake"})
        voice._handle_event(
            {"event": "transcript", "text": "É, Ísis, modo de execução.", "wake": True}
        )
        assert await wait_until(pilot, lambda: conversation.current_mode.id == "default")

        # "Íris, <free text>" is dictation, without the "unknown command" warning.
        voice._handle_event({"event": "wake"})
        voice._handle_event(
            {"event": "transcript", "text": "Íris, refatora o main", "wake": True}
        )
        assert text_area.text == "refatora o main"
        assert not any("Não reconheci" in str(n.message) for n in app._notifications)


@pytest.mark.parametrize("mode, wake", [("ditado", True), ("avisos", False)])
async def test_wake_flag_in_service_command(spoken, mode, wake):
    """The wake word needs dictation: without it the service isn't asked to watch."""
    write_settings({"voz": {"modo": mode, "ativacao": True}})
    app = ToadApp(agent_data=agent_data(), project_dir=".")
    async with app.run_test(size=SIZE):
        assert ("--wake" in app.iris_voice._command()) is wake


def test_match_project_from_dictation(tmp_path):
    from toad.iris_projects import Project, match_project, project_agent

    projects = [Project("Mestrado", agente="codex"), Project("OT Modbus")]
    assert match_project(projects, "mestrado").nome == "Mestrado"
    assert match_project(projects, "o mestrado por favor").nome == "Mestrado"
    assert match_project(projects, "mestrad").nome == "Mestrado"
    assert match_project(projects, "ot mod bus").nome == "OT Modbus"
    assert match_project(projects, "estufa") is None
    assert project_agent(projects[0]) == "openai.com"
    assert project_agent(projects[1], "geminicli.com") == "geminicli.com"


async def test_voice_project_commands(spoken, tmp_path):
    from toad.iris_projects import load_projects

    write_settings({"voz": {"modo": "ditado_avisos"}})
    folder = tmp_path / "estufa"
    folder.mkdir()
    app = ToadApp(agent_data=agent_data(), project_dir=str(folder))
    async with app.run_test(size=SIZE) as pilot:
        conversation = await start(pilot)
        voice = app.iris_voice

        voice.handle_transcript("Íris, quais são os meus projetos?", conversation)
        assert await wait_until(pilot, lambda: any("ainda não tem projetos" in s for s in spoken))

        voice.handle_transcript("Íris, cria um projeto chamado Estufa.", conversation)
        assert await wait_until(pilot, lambda: any("Projeto Estufa criado" in s for s in spoken))
        (project,) = load_projects()
        assert project.nome == "Estufa" and project.main_folder == folder.resolve()
        assert conversation.iris_project.nome == "Estufa"

        voice.handle_transcript("Íris, em que projeto eu estou?", conversation)
        assert await wait_until(pilot, lambda: "Você está no projeto Estufa." in spoken)

        voice.handle_transcript("Íris, lista os projetos", conversation)
        assert await wait_until(pilot, lambda: "Você tem 1 projeto: Estufa." in spoken)

        voice.handle_transcript("Íris, abre o projeto Horta", conversation)
        assert await wait_until(pilot, lambda: any("Não encontrei o projeto horta" in s for s in spoken))

        launched = []
        app.launch_agent = lambda identity, **kwargs: launched.append(
            (identity, kwargs.get("project_path"))
        )  # don't start the real Claude Code
        voice.handle_transcript("Íris, abre o projeto estufa", conversation)
        assert await wait_until(pilot, lambda: "Abrindo o projeto Estufa." in spoken)
        assert await wait_until(pilot, lambda: launched == [("claude.com", folder.resolve())])
        assert app.iris_project_preferred == "Estufa"
