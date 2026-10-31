"""Voice commands handled by Íris itself ("Íris, modo planejamento").

Dictation that starts with the wake word is parsed here instead of going to the
agent, so these work the same with Claude, Codex or Gemini.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from toad.app import ToadApp
    from toad.widgets.conversation import Conversation

# Whisper spells the name a few ways.
from toad import WAKE_NAME, WAKE_SPELLINGS

WAKE_WORDS = WAKE_SPELLINGS

AGENTS = {
    "claude": "claude.com",
    "cloud": "claude.com",  # common mishearing
    "clod": "claude.com",
    "codex": "openai.com",
    "codecs": "openai.com",
    "gemini": "geminicli.com",
    "jeminai": "geminicli.com",
}


@dataclass(frozen=True)
class Command:
    name: str
    argument: str = ""


def normalize(text: str) -> str:
    """Lowercase, strip accents and punctuation: 'Íris, Modo de Execução!' -> 'iris modo de execucao'."""
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


FILLERS = ("ok", "ei", "hey", "oi")
# After the wake word was heard, Whisper also puts these before the name ("É, Íris…").
WAKE_FILLERS = FILLERS + ("e", "o", "a", "ai", "ta", "entao", "bom", "olha", "fala")


def is_wake_word(word: str, loose: bool = False) -> bool:
    """`loose`: also near spellings ("isis", "ires"), once the wake word was heard."""
    if word in WAKE_WORDS:
        return True
    from difflib import SequenceMatcher

    name = normalize(WAKE_NAME)
    return (
        loose
        and len(name) - 2 <= len(word) <= len(name) + 2
        and SequenceMatcher(None, word, name).ratio() >= 0.75
    )


def strip_wake_word(text: str, loose: bool = False) -> str | None:
    """Return what follows the wake word, or None if the text isn't addressed to Íris.

    `loose` is for transcripts recorded after the wake word: up to two filler words
    may come first and the name may be misspelled.
    """
    words = normalize(text).split(" ")
    fillers = WAKE_FILLERS if loose else FILLERS
    for _ in range(2 if loose else 1):
        if words and words[0] in fillers:
            words = words[1:]
    if not words or not is_wake_word(words[0], loose):
        return None
    return " ".join(words[1:])


# (pattern, command name). First match wins; patterns run on normalized text.
PATTERNS: list[tuple[str, str]] = [
    # Leaving planning must be checked before "planejamento" alone.
    (r"\bmodo (de )?execucao\b|\bmodo normal\b|\bsai(r)? do (modo (de )?)?planejamento\b", "modo_execucao"),
    (r"\b(modo )?(de )?planeja(mento|r)\b|\bmodo plano\b", "modo_planejamento"),
    (r"\bfecha(r)?( a| essa| esta)? sess(ao|oes)\b", "fechar"),
    (r"\bnova sess(ao)\b|\babre? (uma )?(nova )?sess(ao)\b", "nova_sessao"),
    (r"\b(proxima|seguinte) sess(ao)\b", "proxima_sessao"),
    (r"\bsess(ao)? anterior\b|\bvolta(r)? (a )?sess(ao)\b", "sessao_anterior"),
    # Projects: before "abrir", which would take "abre o projeto" for an agent.
    (r"\b(que|qual) (e o )?projeto\b.*\b(estou|esta|atual|esse|este)\b|\bprojeto atual\b", "projeto_atual"),
    (r"\b(cria(r)?|crie|faz(er)?|novo)( um)?( novo)? projeto( chamado| com o nome( de)?| de nome)? (?P<arg>.+)$", "criar_projeto"),
    (r"\b(abr(e|ir)|entra(r)?|vai|ir)( para| pro| no| na| em)?( o| a)? projeto (?P<arg>.+)$", "abrir_projeto"),
    (r"\bprojetos\b", "listar_projetos"),
    (r"\b(abr(e|ir)|inicia(r)?|chama(r)?|usa(r)?)( o| a)? (\w+)", "abrir"),
    (r"\b(envia(r)?|manda(r)?|pode mandar)\b", "enviar"),
    (r"\b(apaga(r)?|limpa(r)?)( o)? (texto|prompt|campo)\b|\bapaga tudo\b", "limpar"),
    (r"\b(para|parar|cancela(r)?|interromp(e|er))( o agente| tudo)?$", "cancelar"),
    (r"\b(silencio|fica quieta|cala a boca|para de falar|chega)\b", "silencio"),
    (r"\brepet(e|ir)\b", "repetir"),
    (r"\bque horas (sao|e)\b|\bhoras\b", "horas"),
    (r"\b(status|situacao|como (esta|estao)|o que (esta|voce esta) fazendo)\b", "status"),
    (r"\b(ajuda|comandos|o que (voce )?(sabe|pode) fazer)\b", "ajuda"),
    (r"\b(sai(r)?|fecha(r)? (a )?iris|desliga(r)?|encerra(r)?)\b", "sair"),
]

HELP_TEXT = (
    f"Diga {WAKE_NAME} e um comando: modo planejamento, modo execução, abrir o Claude, "
    "o Codex ou o Gemini, nova sessão, próxima sessão, sessão anterior, fechar sessão, "
    "quais são os meus projetos, abrir o projeto NOME, criar um projeto chamado NOME, "
    "em que projeto estou, "
    "enviar, limpar o texto, parar, silêncio, repetir, que horas são, status, ou sair."
)


def parse_command(text: str) -> Command | None:
    """Parse a transcript. None means it wasn't a command (just dictation)."""
    rest = strip_wake_word(text)
    if rest is None:
        return None
    if not rest:
        return Command("vazio")
    for pattern, name in PATTERNS:
        if match := re.search(pattern, rest):
            if name == "abrir_projeto":
                return Command(name, match.group("arg").strip())
            if name == "criar_projeto":
                # The name as spoken (accents, capitals), not the normalized text.
                count = len(match.group("arg").split())
                words = re.findall(r"[^\W_][\w-]*", text)[-count:]
                spoken_name = " ".join(words)
                return Command(name, spoken_name[:1].upper() + spoken_name[1:])
            if name == "abrir":
                target = match.group(match.lastindex or 0)
                identity = next(
                    (agent for key, agent in AGENTS.items() if key in rest.split()), None
                )
                if identity is None:
                    if target in ("sessao",):
                        return Command("nova_sessao")
                    continue
                return Command("abrir", identity)
            return Command(name)
    return Command("desconhecido", rest)


def spoken_list(names: list[str]) -> str:
    """'A', 'A e B', 'A, B e C'."""
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " e " + names[-1]


async def run_project_command(
    app: ToadApp, command: Command, conversation: Conversation | None
) -> str | None:
    """"Íris, quais são os meus projetos / abre o projeto X / cria um projeto chamado X"."""
    from toad import iris_projects

    projects = iris_projects.load_projects()
    names = [project.nome for project in projects]
    try:
        match command.name:
            case "listar_projetos":
                if not projects:
                    return f"Você ainda não tem projetos. Diga: {WAKE_NAME}, cria um projeto chamado, e o nome."
                app.notify(
                    "\n".join(
                        f"• {project.nome} — {project.main_folder or 'sem pasta'}"
                        for project in projects
                    ),
                    title="Projetos",
                    timeout=10,
                )
                plural = "projeto" if len(names) == 1 else "projetos"
                return f"Você tem {len(names)} {plural}: {spoken_list(names)}."
            case "projeto_atual":
                if conversation is None or conversation.iris_project is None:
                    return "Esta sessão não está em nenhum projeto."
                return f"Você está no projeto {conversation.iris_project.nome}."
            case "abrir_projeto":
                project = iris_projects.match_project(projects, command.argument)
                if project is None:
                    known = f" Seus projetos são {spoken_list(names)}." if names else ""
                    return f"Não encontrei o projeto {command.argument}.{known}"
                current = conversation._agent_data if conversation is not None else None
                fallback = current["identity"] if current else "claude.com"
                iris_projects.open_project(
                    app, project, iris_projects.project_agent(project, fallback)
                )
                return f"Abrindo o projeto {project.nome}."
            case "criar_projeto":
                if conversation is None:
                    return "Abra uma conversa na pasta do projeto e peça de novo."
                project = iris_projects.new_project(
                    projects, command.argument, "", [conversation.project_path]
                )
                iris_projects.save_projects(projects)
                conversation.iris_project = project
                conversation.update_title()
                return (
                    f"Projeto {project.nome} criado com a pasta {conversation.project_path.name}. "
                    "A descrição e outras pastas você ajusta com barra projeto."
                )
    except iris_projects.ProjectError as error:
        return str(error)
    return None


async def run_command(
    app: ToadApp, command: Command, conversation: Conversation | None
) -> str | None:
    """Carry out a command. Returns text Íris should say back (or None)."""
    from toad import messages

    voice = app.iris_voice

    def need_conversation() -> bool:
        if conversation is None:
            app.notify("Esse comando precisa de uma conversa aberta.", title="Íris")
            return False
        return True

    match command.name:
        case "modo_planejamento" | "modo_execucao":
            if not need_conversation():
                return None
            from toad.iris_voice import is_planning_mode

            want_plan = command.name == "modo_planejamento"
            modes = list(conversation.modes.values())
            candidates = [mode for mode in modes if is_planning_mode(app, mode) == want_plan]
            if not candidates:
                return "Este agente não tem modo de planejamento." if want_plan else None
            # Prefer the agent's default mode when leaving planning.
            target = next((mode for mode in candidates if mode.id == "default"), candidates[0])
            if conversation.current_mode is not None and conversation.current_mode.id == target.id:
                return "Já estou nesse modo."
            await conversation.set_mode(target.id)
            return None  # the mode-change announcement speaks for itself
        case "fechar":
            if need_conversation():
                await conversation.slash_command("/fechar")
            return None
        case "nova_sessao":
            if need_conversation() and conversation._agent_data is not None:
                conversation.post_message(
                    messages.SessionNew(
                        str(conversation.working_directory),
                        conversation._agent_data["identity"],
                        "",
                    )
                )
            return None
        case "abrir":
            if conversation is not None:
                conversation.post_message(
                    messages.SessionNew(str(conversation.working_directory), command.argument, "")
                )
            else:
                app.post_message(messages.LaunchAgent(command.argument))
            return None
        case "proxima_sessao" | "sessao_anterior":
            if app.screen.id is not None:
                direction = 1 if command.name == "proxima_sessao" else -1
                app.post_message(messages.SessionNavigate(app.screen.id, direction))
            return None
        case "enviar":
            if need_conversation():
                if conversation.prompt.prompt_text_area.text.strip():
                    conversation.prompt.prompt_text_area.action_submit()
                else:
                    return "Não há nada para enviar."
            return None
        case "limpar":
            if need_conversation():
                conversation.prompt.prompt_text_area.text = ""
            return "Texto apagado."
        case "cancelar":
            if need_conversation() and conversation.agent is not None:
                if conversation.busy_count > 0:
                    await conversation.agent.cancel()
                    return "Parei."
                return "O agente não está trabalhando."
            return None
        case "silencio":
            voice.send({"cmd": "stop"})
            return None
        case "repetir":
            return voice.last_spoken or "Ainda não falei nada."
        case "listar_projetos" | "abrir_projeto" | "criar_projeto" | "projeto_atual":
            return await run_project_command(app, command, conversation)
        case "horas":
            now = datetime.now()
            return f"São {now.hour} horas e {now.minute} minutos."
        case "status":
            if conversation is None:
                return "Nenhuma conversa aberta."
            agent = conversation.agent_title or "O agente"
            mode = conversation.current_mode.name if conversation.current_mode else ""
            doing = "trabalhando" if conversation.busy_count > 0 else "aguardando você"
            return f"{agent} está {doing}" + (f", no modo {mode}." if mode else ".")
        case "ajuda":
            app.notify(HELP_TEXT, title="Comandos de voz", timeout=15)
            return "Os comandos estão na tela."
        case "sair":
            await app.action_quit()
            return None
        case "vazio":
            return "Sim?"
        case _:
            return None
