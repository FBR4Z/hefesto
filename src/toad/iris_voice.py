"""Íris voice: talks to the local `iris-voz` service and maps agent events to phrases.

The heavy lifting (Whisper, Kokoro/Piper, CUDA) lives in a separate process, so the
UI stays light and each machine can pick the engine it can afford.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
from datetime import datetime
from pathlib import PureWindowsPath
from time import monotonic
from typing import TYPE_CHECKING, Any

from textual import log

if TYPE_CHECKING:
    from toad.app import ToadApp

# mode -> (announce events, dictation, read responses)
MODES: dict[str, tuple[bool, bool, bool]] = {
    "desligada": (False, False, False),
    "avisos": (True, False, False),
    "ditado": (False, True, False),
    "ditado_avisos": (True, True, False),
    "completa": (True, True, True),
}

DEFAULT_PHRASES = """\
saudacao = {saudacao}, {nome}.
conectado = {agente} conectado.
falha = Não consegui falar com {agente}.
modo_planejamento = Modo de planejamento.
modo_execucao = Modo de execução.
trabalhando =
executando = Executando {programa}.
editando = Editando {arquivo}.
apagando = Apagando {arquivo}.
movendo = Movendo {arquivo}.
lendo =
pesquisando =
web = Acessando a web.
permissao = Preciso da sua permissão.
plano =
concluido = Pronto. Terminei em {duracao}.
erro = Algo deu errado.
limite = Você já usou {percentual} por cento do limite {janela}.
limite_atingido = Limite de uso atingido.
tchau = Até logo.
"""
"""One event per line: `event = phrase`. An empty phrase silences that event."""

PHRASE_HELP = (
    "Uma linha por evento, no formato evento = frase. Deixe a frase vazia para silenciar. "
    "Variáveis: {saudacao} {nome} {agente} {modo} {programa} {arquivo} {etapas} {duracao} "
    "{percentual} {janela}. "
    "Eventos: saudacao, conectado, falha, modo_planejamento, modo_execucao, trabalhando, "
    "executando, editando, apagando, movendo, lendo, pesquisando, web, permissao, plano, "
    "concluido, erro, limite, limite_atingido, tchau."
)

PROGRAMS = {
    "powershell": "PowerShell",
    "pwsh": "PowerShell",
    "cmd": "o prompt de comando",
    "git": "git",
    "gh": "GitHub CLI",
    "npm": "npm",
    "npx": "npm",
    "pnpm": "pnpm",
    "yarn": "yarn",
    "node": "Node",
    "python": "Python",
    "python3": "Python",
    "py": "Python",
    "pip": "pip",
    "uv": "uv",
    "pytest": "os testes",
    "dotnet": "dotnet",
    "cargo": "Cargo",
    "go": "Go",
    "docker": "Docker",
    "make": "make",
    "curl": "curl",
}

TOOL_KIND_EVENTS = {
    "execute": "executando",
    "edit": "editando",
    "delete": "apagando",
    "move": "movendo",
    "read": "lendo",
    "search": "pesquisando",
    "fetch": "web",
}

TOOL_ANNOUNCE_GAP = 2.5
"""Minimum seconds between tool announcements, so a burst of tool calls isn't read out."""


def is_planning_mode(app: ToadApp, mode: Any) -> bool:
    """Does this ACP session mode count as planning (per the `iris.plan_modes` setting)?"""
    if mode is None:
        return False
    try:
        words = app.settings.get("iris.plan_modes", str)
    except Exception:
        words = "plan, read-only"
    mode_text = f"{mode.id} {mode.name}".lower()
    return any(word.strip().lower() in mode_text for word in words.split(",") if word.strip())


def parse_phrases(text: str) -> dict[str, str]:
    phrases: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, phrase = line.partition("=")
        if sep and key.strip():
            phrases[key.strip().lower()] = phrase.strip()
    return phrases


def tidy(text: str) -> str:
    """Clean up phrases where a variable came out empty ("Bom dia, ." -> "Bom dia.")."""
    text = re.sub(r"\s+([,.!?])", r"\1", text)
    text = re.sub(r",([.!?])", r"\1", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip(" ,")


def greeting() -> str:
    hour = datetime.now().hour
    if 5 <= hour < 12:
        return "Bom dia"
    if 12 <= hour < 18:
        return "Boa tarde"
    return "Boa noite"


def spoken_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds} segundos"
    minutes, seconds = divmod(seconds, 60)
    text = f"{minutes} minuto" + ("s" if minutes > 1 else "")
    return text + (f" e {seconds} segundos" if seconds else "")


def program_from_tool(tool_call: dict[str, Any]) -> str:
    """Work out which program an `execute` tool call runs, in speakable form."""
    raw = tool_call.get("rawInput") or {}
    command = raw.get("command") if isinstance(raw, dict) else None
    if isinstance(command, list):
        command = " ".join(str(part) for part in command)
    title = str(tool_call.get("title") or "")
    text = str(command or title).strip().strip("`")
    if "powershell" in (text + " " + title).lower():
        return "PowerShell"
    try:
        first = shlex.split(text, posix=False)[0] if text else ""
    except ValueError:
        first = text.split(" ", 1)[0]
    name = PureWindowsPath(first.strip("\"'")).stem.lower()
    return PROGRAMS.get(name, "um comando")


def file_from_tool(tool_call: dict[str, Any]) -> str:
    for location in tool_call.get("locations") or []:
        if path := location.get("path"):
            return PureWindowsPath(path).name
    raw = tool_call.get("rawInput") or {}
    if isinstance(raw, dict):
        for key in ("file_path", "path", "filePath", "notebook_path"):
            if value := raw.get(key):
                return PureWindowsPath(str(value)).name
    return "um arquivo"


def summarize_response(markdown: str, limit: int) -> str:
    """Turn an agent's markdown answer into something pleasant to hear."""
    had_code = "```" in markdown
    text = re.sub(r"```.*?(```|$)", " ", markdown, flags=re.S)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("|") or re.fullmatch(r"[-=*_|: ]+", stripped or "-"):
            continue
        stripped = re.sub(r"^(#+|>|[-*+]|\d+[.)])\s*", "", stripped)
        if stripped and not re.search(r"[.!?:;…]$", stripped):
            # Headings and list items have no final stop; keep them from running on.
            stripped += "."
        lines.append(stripped)
    text = "\n".join(lines)
    text = re.sub(r"(?<=\w)_(?=\w)", " ", text)  # get_user_by_id -> get user by id
    text = re.sub(r"[*_`~]", "", text)
    text = re.sub(r"\n{2,}", "\n\n", text).strip()

    summary = ""
    for sentence in re.split(r"(?<=[.!?:])\s+", text.replace("\n", " ")):
        if summary and len(summary) + len(sentence) > limit:
            break
        summary = f"{summary} {sentence}".strip()
    if len(summary) > limit:
        summary = summary[:limit].rsplit(" ", 1)[0] + "…"
    if had_code:
        summary += " Deixei o código na tela."
    return summary.strip()


class IrisVoice:
    """Client for the `iris-voz` process, plus the event -> phrase announcer."""

    def __init__(self, app: ToadApp) -> None:
        self.app = app
        self._process: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task | None = None
        self._config: tuple | None = None
        self._greeted = False
        self._last_tool_announce = 0.0
        self._transcript_target: Any = None
        self._pending: list[tuple[str, dict[str, Any]]] = []
        """Announcements made before the service was ready."""
        self.ready = False
        self.speaking = False
        self.listening = False
        self.transcribing = False
        self.watching = False
        """The service is waiting for the wake word."""
        self.level = 0.0
        self.engines = ""
        self.last_spoken = ""

    # ----------------------------------------------------------------- settings

    def _get(self, key: str, default: Any = "") -> Any:
        try:
            value = self.app.settings.get(f"voz.{key}")
        except Exception:
            return default
        return default if value is None else value

    @property
    def mode(self) -> str:
        mode = str(self._get("modo", "desligada"))
        return mode if mode in MODES else "desligada"

    @property
    def announces(self) -> bool:
        return MODES[self.mode][0]

    @property
    def dictation(self) -> bool:
        return MODES[self.mode][1]

    @property
    def reads_responses(self) -> bool:
        return MODES[self.mode][2]

    def _command(self) -> list[str]:
        announce, dictation, read = MODES[self.mode]
        command = shlex.split(str(self._get("comando", "iris-voz")), posix=False)
        tts = str(self._get("motor", "auto")) if (announce or read) else "off"
        stt = str(self._get("ditado_modelo", "auto")) if dictation else "off"
        command += ["serve", "--tts", tts, "--stt", stt]
        command += ["--voice", str(self._get("voz", "pf_dora"))]
        command += ["--speed", str(self._get("velocidade", "1.0") or "1.0")]
        if self._get("somente_cpu", False):
            command.append("--cpu")
        if dictation and self._get("ativacao", False):
            from toad import WAKE_NAME

            command += ["--wake", "--wake-word", WAKE_NAME.lower()]
        return command

    # ------------------------------------------------------------------ process

    async def sync(self) -> None:
        """Start, restart or stop the service to match the current settings."""
        if self.mode == "desligada":
            await self.stop()
            return
        command = self._command()
        if self._process is not None and self._process.returncode is None:
            if tuple(command) == self._config:
                return
            await self.stop()
        self._config = tuple(command)
        from toad import paths

        try:
            # Keep the service's own errors (CUDA, microphone...) for diagnosis.
            stderr_log = (paths.get_state() / "iris-voz.log").open("ab")
        except OSError:
            stderr_log = asyncio.subprocess.DEVNULL
        try:
            self._process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=stderr_log,
            )
        except (OSError, ValueError) as error:
            self._process = None
            self.app.notify(
                f"Não consegui iniciar o serviço de voz ({error}).\n"
                "Verifique o comando em Configurações → Voz.",
                title="Voz",
                severity="error",
            )
            return
        finally:
            if stderr_log is not asyncio.subprocess.DEVNULL:
                stderr_log.close()  # the child has its own handle now
        self._reader = asyncio.create_task(self._read_events(self._process))

    async def stop(self) -> None:
        process, self._process = self._process, None
        self._pending = []
        self.ready = self.speaking = self.listening = self.transcribing = False
        self.watching = False
        self.level = 0.0
        if process is None or process.returncode is not None:
            return
        try:
            self._write(process, {"cmd": "quit"})
            await asyncio.wait_for(process.wait(), 2)
        except Exception:
            process.kill()

    def _write(self, process: asyncio.subprocess.Process | None, message: dict) -> None:
        if process is None or process.stdin is None or process.returncode is not None:
            return
        try:
            process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode())
        except Exception as error:
            log(f"iris-voz write failed: {error}")

    def send(self, message: dict) -> None:
        self._write(self._process, message)

    async def _read_events(self, process: asyncio.subprocess.Process) -> None:
        assert process.stdout is not None
        while line := await process.stdout.readline():
            try:
                event = json.loads(line.decode("utf-8", "replace"))
            except json.JSONDecodeError:
                continue
            self._handle_event(event)
        if process is self._process:
            self.ready = self.speaking = self.listening = self.transcribing = False
            self.watching = False
            from toad import paths

            self.app.notify(
                "O serviço de voz foi encerrado inesperadamente.\n"
                f"Detalhes em {paths.get_state() / 'iris-voz.log'}",
                title="Voz",
                severity="error",
                timeout=10,
            )

    def _handle_event(self, event: dict) -> None:
        match event.get("event"):
            case "ready":
                self.ready = True
                wake = str(event.get("wake") or "off")
                self.watching = wake != "off"
                self.engines = f"fala: {event.get('tts')} · escuta: {event.get('stt')}"
                if self.watching:
                    self.engines += f" · ativação: {wake}"
                if not self._greeted:
                    self._greeted = True
                    self.announce(
                        "saudacao",
                        saudacao=greeting(),
                        nome=str(self._get("nome", "")),
                    )
                pending, self._pending = self._pending, []
                for pending_event, values in pending:
                    self.announce(pending_event, **values)
            case "speaking":
                self.speaking = bool(event.get("on"))
            case "wake":
                self._wake_heard()
            case "listening":
                self.listening = bool(event.get("on"))
                if not self.listening:
                    self.transcribing = True
            case "level":
                self.level = float(event.get("value", 0.0))
            case "transcript":
                self.transcribing = False
                self._deliver_transcript(
                    str(event.get("text", "")).strip(), wake=bool(event.get("wake"))
                )
            case "error":
                self.app.notify(str(event.get("message")), title="Voz", severity="error")

    # ------------------------------------------------------------------ speaking

    def say(self, text: str, cache: bool = False) -> None:
        if text:
            self.last_spoken = text
        if text and self.ready:
            self.send({"cmd": "say", "text": text, "cache": cache})

    def announce(self, event: str, **values: Any) -> None:
        """Speak the phrase mapped to `event`, if announcements are on."""
        if not self.announces:
            return
        if not self.ready:
            # e.g. "agent connected" often happens while the models are still loading.
            if self._process is not None and len(self._pending) < 3:
                self._pending.append((event, values))
            return
        phrases = parse_phrases(DEFAULT_PHRASES)
        phrases.update(parse_phrases(str(self._get("frases", "")) or ""))
        template = phrases.get(event, "")
        if not template:
            return
        try:
            text = tidy(template.format_map(_Blank(values)))
        except (ValueError, IndexError):
            text = tidy(template)
        # Most announcements repeat verbatim ("Executando PowerShell."), so cache their
        # audio; durations and step counts vary too much to be worth it.
        self.say(text, cache=event not in {"concluido", "plano"})

    def announce_tool(self, tool_call: dict[str, Any]) -> None:
        event = TOOL_KIND_EVENTS.get(str(tool_call.get("kind") or ""))
        if event is None or self.speaking:
            return
        now = monotonic()
        if now - self._last_tool_announce < TOOL_ANNOUNCE_GAP:
            return
        self._last_tool_announce = now
        self.announce(
            event,
            programa=program_from_tool(tool_call),
            arquivo=file_from_tool(tool_call),
        )

    def turn_over(self, agent: str, response: str, duration: float) -> None:
        if self.reads_responses and response.strip():
            limit = int(self._get("resposta_max", 350) or 350)
            self.say(summarize_response(response, limit))
            return
        minimum = int(self._get("concluido_segundos", 20) or 0)
        if duration >= minimum:
            self.announce("concluido", agente=agent, duracao=spoken_duration(duration))

    async def farewell(self) -> None:
        if not (self.announces and self.ready):
            return
        self.announce("tchau")
        deadline = monotonic() + 3
        while monotonic() < deadline and not self.speaking:
            await asyncio.sleep(0.05)
        while monotonic() < deadline and self.speaking:
            await asyncio.sleep(0.05)

    # ------------------------------------------------------------------ listening

    @staticmethod
    def dictation_hint(conversation: Any) -> str:
        """Words Whisper should expect: the agents and the project's file names."""
        from toad import WAKE_NAME

        hint = f"{WAKE_NAME}, Claude, Codex, Gemini"
        if conversation is not None:
            try:
                names = sorted(path.name for path in conversation.project_path.iterdir())
                hint += ", " + ", ".join(names[:40])
            except OSError:
                pass
        return hint

    def _current_conversation(self) -> Any:
        from toad.widgets.conversation import Conversation

        try:
            return self.app.screen.query_one_optional(Conversation)
        except Exception:
            return None

    def _wake_heard(self) -> None:
        """The service heard "Íris" and is recording the rest; aim it at this screen."""
        self._transcript_target = self._current_conversation()
        self.send({"cmd": "hint", "hint": self.dictation_hint(self._transcript_target)})
        if self.speaking:
            self.send({"cmd": "stop"})

    def toggle_listen(self, target: Any, hint: str = "") -> None:
        """Start dictation into `target` (a Conversation), or cancel it."""
        if not self.dictation:
            self.app.notify(
                "O ditado está desligado. Ative em Configurações → Voz → Modo.",
                title="Voz",
            )
            return
        if not self.ready:
            self.app.notify("A voz ainda está carregando…", title="Voz")
            return
        if self.listening:
            self.send({"cmd": "cancel"})
            return
        self._transcript_target = target
        self.send({"cmd": "listen", "hint": hint})

    def _deliver_transcript(self, text: str, wake: bool = False) -> None:
        target, self._transcript_target = self._transcript_target, None
        if target is not None and not target.is_attached:
            target = None
        self.handle_transcript(text, target, wake=wake)

    def handle_transcript(self, text: str, target: Any, wake: bool = False) -> None:
        """Run it as a voice command ("Íris, ...") or type it into `target`'s prompt.

        `wake`: recorded after the wake word. Whisper has the final say: if the text
        doesn't start with "Íris" it was a false alarm (e.g. "a íris do olho") and is
        dropped; "Íris, <texto livre>" is dictation.
        """
        if not text:
            return
        from toad import TITLE, WAKE_NAME
        from toad.iris_commands import parse_command, strip_wake_word

        if wake:
            rest = strip_wake_word(text, loose=True)
            if rest is None:
                log(f"iris-voz: alarme falso da palavra de ativação: {text!r}")
                # Say what was heard: a silent drop looks like dictation is broken.
                self.app.notify(
                    f"Ouvi “{text}”, mas não parecia um pedido para mim. "
                    f"Comece com “{WAKE_NAME}, …” ou use o F9.",
                    title="Voz",
                    timeout=5,
                )
                return
            # Written the way the command parser expects ("É, Ísis, …" -> "Íris, …").
            text = f"{WAKE_NAME}, {rest}" if rest else WAKE_NAME
            if not self._get("comandos", True):
                text = rest
                if not text:
                    return
        if self._get("comandos", True):
            command = parse_command(text)
            if command is not None and command.name != "desconhecido":
                self.app.run_worker(self._run_command(command, target))
                return
            if command is not None:
                # Addressed to Íris but not a command: don't lose what was said.
                text = command.argument
                if not wake:  # with the wake word, "Íris, <text>" is how you dictate
                    self.app.notify(
                        "Não reconheci o comando; o texto foi para o campo de digitação.",
                        title=TITLE,
                    )
        if target is None:
            self.app.notify("Abra uma conversa para ditar.", title="Voz")
            return
        prompt = target.prompt
        current = prompt.prompt_text_area.text
        prompt.append((" " if current and not current.endswith(" ") else "") + text)
        prompt.focus()
        if self._get("enviar_ditado", False):
            prompt.prompt_text_area.action_submit()


    async def _run_command(self, command: Any, target: Any) -> None:
        from toad.iris_commands import run_command

        from toad import TITLE

        self.app.notify(f"Comando: {command.name.replace('_', ' ')}", title=TITLE, timeout=2)
        if reply := await run_command(self.app, command, target):
            if self.announces or self.reads_responses:
                self.say(reply)
            else:  # dictation-only mode has no speech: show it instead
                self.app.notify(reply, title=TITLE)


class _Blank(dict):
    """format_map helper: unknown or empty variables render as ''."""

    def __missing__(self, key: str) -> str:
        return ""
