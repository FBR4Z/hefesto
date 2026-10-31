"""Hefesto's slash commands for robot programming, alarms and read-only voice commands.

    /rapid editar <controlador> <módulo> [ia|eu]   cópia de trabalho + histórico
    /rapid revisar                                  diff, estrutura e riscos (e a IA revisa se você editou)
    /rapid aplicar                                  gêmeo virtual primeiro, depois o controlador, com confirmação
    /rapid voltar [versão]                          carrega uma versão do histórico
    /rapid historico [controlador] [módulo]
    /rapid modelo [nome]                            modelos de RAPID para partir deles
    /rapid cancelar
    /ponto <nome> [controlador]                     grava a posição atual como robtarget
    /io <controlador> [filtro]                      sinais e valores
    /explicar [controlador]                         a IA explica o último erro, com o trecho do programa
    /ciclo [controlador]                            tempos de ciclo

Slash commands are typed by a person (the agent can't run them), so loading a
module from here is a human action: a confirmation dialog stands in for the
"escrita" permission, which is about what the AI may do through MCP.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Static

from toad.slash_command import SlashCommand

if TYPE_CHECKING:
    from toad.hefesto_robot import Controller, Registry
    from toad.widgets.conversation import Conversation

VERDICT = re.compile(r"VEREDITO:\s*(APROVADO|REPROVADO)", re.I)


def slash_commands() -> list[SlashCommand]:
    return [
        SlashCommand("/rapid", "Editar RAPID com segurança: editar, revisar, aplicar, voltar, histórico, modelo",
                     "<editar CONTROLADOR MÓDULO [ia|eu] | revisar | aplicar | voltar [VERSÃO] | historico | modelo [NOME] | cancelar>"),
        SlashCommand("/ponto", "Gravar a posição atual do robô como robtarget", "<nome> [controlador]"),
        SlashCommand("/io", "Sinais de I/O de um controlador", "<controlador> [filtro]"),
        SlashCommand("/explicar", "A IA explica o último erro do controlador", "[controlador]"),
        SlashCommand("/ciclo", "Tempos de ciclo", "[controlador]"),
    ]


# ------------------------------------------------------------------------ helpers


def _registry(app: Any) -> Registry:
    from toad.hefesto_robot import load_registry

    registry = getattr(app, "hefesto_registry", None)
    return registry if registry is not None else load_registry()


def _controller(app: Any, name: str | None) -> Controller:
    """The named controller, or the one in focus."""
    from toad.hefesto_robot import RobotError

    registry = _registry(app)
    if name:
        return registry.find(name)
    monitor = getattr(app, "hefesto_monitor", None)
    if monitor is not None and monitor.focus:
        return registry.find(monitor.focus)
    if len(registry.controladores) == 1:
        return registry.controladores[0]
    raise RobotError("Diga qual controlador (hefesto robo listar).")


async def run_abbctl(registry: Registry, controller: Controller, *args: str, timeout: float = 120) -> tuple[int, str, str]:
    from toad.hefesto_robot import RobotError, find_tool

    abbctl = find_tool(registry.abbctl, "abbctl")
    if abbctl is None:
        raise RobotError("abbctl não encontrado (hefesto robo ferramentas).")
    process = await asyncio.create_subprocess_exec(
        abbctl, "-c", controller.endereco, *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        out, err = await asyncio.wait_for(process.communicate(), timeout)
    except asyncio.TimeoutError:
        process.kill()
        raise RobotError(f"abbctl {' '.join(args)}: sem resposta em {timeout:.0f} s")
    return process.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace").strip()


def _history(controller: Controller):
    from toad import paths
    from toad.hefesto_rapid import History

    return History(paths.get_data() / "rapid" / controller.apelido)


def _work_folder(controller: Controller) -> Path:
    from toad import paths

    folder = paths.get_data() / "rapid-trabalho" / controller.apelido
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def _note(conversation: Conversation, markdown: str) -> None:
    from toad.widgets.markdown_note import MarkdownNote

    await conversation.post(MarkdownNote(markdown))


def _prefill(conversation: Conversation, text: str, submit: bool = False) -> None:
    prompt = conversation.prompt
    prompt.append(text)
    prompt.focus()
    if submit:
        prompt.prompt_text_area.action_submit()


def _last_agent_text(conversation: Conversation) -> str:
    from toad.widgets.agent_response import AgentResponse

    responses = list(conversation.query(AgentResponse))
    if not responses:
        return ""
    last = responses[-1]
    return str(getattr(last, "source", None) or getattr(last, "_markdown", "") or "")


class ConfirmModal(ModalScreen[bool]):
    """Yes/no question with the risks in front of the person."""

    DEFAULT_CSS = """
    ConfirmModal { align: center middle; }
    ConfirmModal > Vertical { width: 80; height: auto; max-height: 90%; border: thick $error; padding: 1 2; background: $panel; }
    ConfirmModal #question { text-style: bold; margin-bottom: 1; }
    ConfirmModal Horizontal { height: auto; align-horizontal: right; margin-top: 1; }
    ConfirmModal Button { margin-left: 2; }
    """
    BINDINGS = [("escape", "dismiss(False)", "Não")]

    def __init__(self, question: str, details: str = "", danger: bool = True) -> None:
        super().__init__()
        self.question = question
        self.details = details
        self.danger = danger

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self.question, id="question")
            if self.details:
                yield Static(self.details, markup=False)
            with Horizontal():
                yield Button("Não", id="nao", variant="primary")
                yield Button("Sim, carregar", id="sim", variant="error" if self.danger else "success")

    def on_mount(self) -> None:
        self.query_one("#nao", Button).focus()  # the safe answer is the default

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "sim")


async def confirm(app: Any, question: str, details: str = "", danger: bool = True) -> bool:
    future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
    app.push_screen(ConfirmModal(question, details, danger), lambda result: future.set_result(bool(result)))
    return await future


# --------------------------------------------------------------------- editing


@dataclass
class EditSession:
    controller: Controller
    module: str
    task: str
    original: str
    path: Path
    mode: str
    """"ia": the AI edits, the person approves. "eu": the person edits, the AI reviews."""
    reviewed_digest: str = ""
    review_turn: int = -1
    log: list[str] = field(default_factory=list)


async def rapid(conversation: Conversation, parameters: str) -> None:
    from toad.hefesto_robot import RobotError

    words = parameters.split()
    action = words[0].lower() if words else "ajuda"
    try:
        match action:
            case "editar":
                await rapid_edit(conversation, words[1:])
            case "revisar":
                await rapid_review(conversation)
            case "aplicar":
                conversation.run_worker(rapid_apply(conversation), exclusive=True, group="hefesto-rapid")
            case "voltar":
                conversation.run_worker(rapid_restore(conversation, words[1:]), exclusive=True, group="hefesto-rapid")
            case "historico" | "histórico":
                await rapid_history(conversation, words[1:])
            case "modelo" | "modelos":
                await rapid_template(conversation, words[1:])
            case "cancelar":
                conversation.hefesto_edit = None
                await _note(conversation, "Edição cancelada. Nada foi carregado no robô.")
            case _:
                await _note(conversation, RAPID_HELP)
    except RobotError as error:
        conversation.notify(str(error), title="/rapid", severity="error")


RAPID_HELP = """## /rapid: editar RAPID com segurança

1. `/rapid editar CONTROLADOR MÓDULO ia` — a **IA edita** a cópia de trabalho e você aprova; ou
   `/rapid editar CONTROLADOR MÓDULO eu` — **você edita** e a IA revisa e confirma.
2. `/rapid revisar` — diff, estrutura e o que muda o movimento (velocidade, zona, alvos, ferramenta, I/O).
3. `/rapid aplicar` — carrega no **gêmeo virtual** (se houver) e depois no controlador, com confirmação.
   O módulo original fica no histórico: `/rapid voltar` desfaz.

Também: `/rapid historico`, `/rapid modelo [nome]`, `/rapid cancelar`, `/ponto NOME`.
O programa precisa estar parado para carregar um módulo (o abbctl avisa)."""


async def rapid_edit(conversation: Conversation, words: list[str]) -> None:
    from toad.hefesto_robot import RobotError

    if len(words) < 2:
        raise RobotError("Use: /rapid editar CONTROLADOR MÓDULO [ia|eu] [TAREFA]")
    app = conversation.app
    controller = _controller(app, words[0])
    module = words[1]
    mode = words[2].lower() if len(words) > 2 and words[2].lower() in ("ia", "eu") else "ia"
    task = words[3] if len(words) > 3 else (words[2] if len(words) > 2 and words[2].lower() not in ("ia", "eu") else "")
    args = ["prog", "cat", module] + (["--task", task] if task else [])
    code, source, err = await run_abbctl(_registry(app), controller, *args)
    if code != 0:
        raise RobotError(f"Não consegui ler {module} de {controller.apelido}: {err or source}")
    source = source.replace("\r\n", "\n")
    commit = _history(controller).save(module, source, f"Lido de {controller.apelido} para editar")
    path = _work_folder(controller) / f"{module}.mod"
    path.write_text(source, encoding="utf-8", newline="\n")
    conversation.hefesto_edit = EditSession(controller, module, task, source, path, mode)
    saved = f" (versão `{commit}` guardada no histórico)" if commit else " (igual à última versão do histórico)"
    who = (
        "A **IA edita**; você revisa com `/rapid revisar` e carrega com `/rapid aplicar`."
        if mode == "ia"
        else "**Você edita** (abri a cópia no editor); depois `/rapid revisar` manda a mudança para a IA revisar."
    )
    twin = f"\n\nGêmeo virtual: `{controller.gemeo}` (o módulo vai para ele primeiro)." if controller.gemeo else ""
    await _note(
        conversation,
        f"## Editando {module} de {controller.apelido} ({controller.kind_label})\n\n"
        f"Cópia de trabalho: `{path}`{saved}.\n\n{who}{twin}",
    )
    if mode == "ia":
        _prefill(
            conversation,
            f"Edite a cópia de trabalho do módulo RAPID {module} do controlador {controller.apelido} "
            f"({controller.kind_label}), no arquivo {path}. Mude só esse arquivo; NÃO carregue nada no "
            "robô (nem pelo MCP): eu reviso com /rapid revisar e o Hefesto carrega. Pedido: ",
        )
    else:
        _open_in_editor(path)


def _open_in_editor(path: Path) -> None:
    """HEFESTO_EDITOR (e.g. "code"), else Notepad on Windows."""
    editor = os.environ.get("HEFESTO_EDITOR") or ("notepad" if sys.platform == "win32" else "")
    if not editor:
        return
    command = [editor, str(path)]
    if sys.platform == "win32" and (found := shutil.which(editor)) and found.lower().endswith((".cmd", ".bat")):
        command = ["cmd", "/c", found, str(path)]
    try:
        subprocess.Popen(command)
    except OSError:
        pass


async def rapid_review(conversation: Conversation) -> None:
    from toad.hefesto_rapid import check_syntax, review_markdown
    from toad.hefesto_robot import RobotError

    session: EditSession | None = getattr(conversation, "hefesto_edit", None)
    if session is None:
        raise RobotError("Nenhuma edição aberta: /rapid editar CONTROLADOR MÓDULO")
    new = session.path.read_text(encoding="utf-8")
    if new == session.original:
        await _note(conversation, "A cópia de trabalho ainda é igual ao módulo do controlador.")
        return
    markdown = review_markdown(session.module, session.original, new)
    await _note(conversation, markdown)
    session.reviewed_digest = _digest(new)
    if session.mode == "eu":
        session.review_turn = conversation._turn_count
        _prefill(
            conversation,
            f"Revise esta alteração que eu (humano) fiz no módulo RAPID {session.module} do controlador "
            f"{session.controller.apelido} ({session.controller.kind_label}). Confira lógica, movimentos, "
            "velocidades, zonas, alvos, ferramenta/obra, I/O e o que pode colidir ou travar a célula. Não "
            "altere arquivos nem o robô. Termine com uma linha exatamente assim: `VEREDITO: APROVADO` ou "
            "`VEREDITO: REPROVADO - motivo`.\n\n" + markdown,
            submit=True,
        )
    elif check_syntax(new):
        conversation.notify("Há erros de estrutura: corrija antes de aplicar.", title="/rapid", severity="warning")


async def _load(conversation: Conversation, controller: Controller, session: EditSession, source_path: Path) -> bool:
    args = ["prog", "load", str(source_path), "--replace"] + (["--task", session.task] if session.task else [])
    code, out, err = await run_abbctl(_registry(conversation.app), controller, *args)
    if code != 0:
        await _note(conversation, f"**Falhou em {controller.apelido}:** {err or out}")
        return False
    await _note(conversation, f"Carregado em **{controller.apelido}** ({controller.kind_label}): {out.strip()}")
    return True


async def rapid_apply(conversation: Conversation) -> None:
    from toad.hefesto_rapid import check_syntax, review_change

    session: EditSession | None = getattr(conversation, "hefesto_edit", None)
    if session is None:
        conversation.notify("Nenhuma edição aberta.", title="/rapid", severity="error")
        return
    new = session.path.read_text(encoding="utf-8")
    if _digest(new) != session.reviewed_digest:
        await _note(conversation, "A cópia mudou depois da revisão (ou não foi revisada): rode `/rapid revisar` antes.")
        return
    if problems := check_syntax(new):
        await _note(conversation, "Não carrego com erros de estrutura:\n" + "\n".join(f"- {p}" for p in problems))
        return
    if session.mode == "eu":
        verdict = VERDICT.findall(_last_agent_text(conversation))
        if conversation._turn_count <= session.review_turn or not verdict:
            await _note(conversation, "Esperando a revisão da IA (ela deve terminar com `VEREDITO: ...`).")
            return
        if verdict[-1].upper() != "APROVADO":
            await _note(conversation, "A IA **reprovou** a alteração. Corrija e rode `/rapid revisar` de novo.")
            return
    risks = review_change(session.original, new)
    high = [risk for risk in risks if risk.level == "alto"]
    summary = "\n".join(f"- {'linha ' + str(r.line) if r.line else 'removido'}: {r.text}" for r in risks[:12]) or "- nenhuma mudança de movimento"
    registry = _registry(conversation.app)
    controller = session.controller
    if controller.gemeo:
        twin = registry.find(controller.gemeo)
        if twin.real:
            await _note(conversation, f"O gêmeo `{twin.apelido}` está cadastrado como REAL: corrija o cadastro.")
            return
        if not await _load(conversation, twin, session, session.path):
            await _note(conversation, "O gêmeo virtual recusou o módulo: o controlador real não foi tocado.")
            return
        if not await confirm(
            conversation.app,
            f"O gêmeo {twin.apelido} aceitou {session.module}. Rode e confira no RobotStudio. Continuar para {controller.apelido}?",
            danger=False,
        ):
            await _note(conversation, "Parado depois do gêmeo. O controlador real não foi tocado.")
            return
    question = f"Carregar {session.module} em {controller.apelido} ({controller.kind_label})?"
    details = f"{len(high)} mudança(s) de risco alto.\n{summary}\n\nO original fica no histórico (/rapid voltar)."
    if not await confirm(conversation.app, question, details, danger=controller.real):
        await _note(conversation, "Cancelado. Nada foi carregado.")
        return
    if await _load(conversation, controller, session, session.path):
        who = "IA editou, humano aprovou" if session.mode == "ia" else "humano editou, IA aprovou"
        commit = _history(controller).save(session.module, new, f"Aplicado em {controller.apelido} ({who})")
        session.original = new
        await _note(conversation, f"Versão `{commit}` no histórico. Para desfazer: `/rapid voltar`.")


async def rapid_restore(conversation: Conversation, words: list[str]) -> None:
    session: EditSession | None = getattr(conversation, "hefesto_edit", None)
    if session is None:
        conversation.notify("Abra o módulo com /rapid editar primeiro.", title="/rapid", severity="error")
        return
    history = _history(session.controller)
    entries = history.log(session.module)
    if not words:
        await _note(conversation, _history_markdown(session.controller, session.module, entries)
                    + "\n\nEscolha com `/rapid voltar VERSÃO`.")
        return
    version = words[0]
    try:
        source = history.show(session.module, version)
    except RuntimeError as error:
        conversation.notify(str(error), title="/rapid", severity="error")
        return
    path = _work_folder(session.controller) / f"{session.module}.versao-{version}.mod"
    path.write_text(source, encoding="utf-8", newline="\n")
    if not await confirm(
        conversation.app,
        f"Carregar a versão {version} de {session.module} em {session.controller.apelido} ({session.controller.kind_label})?",
        "Substitui o módulo atual do controlador (que já está no histórico).",
        danger=session.controller.real,
    ):
        return
    if await _load(conversation, session.controller, session, path):
        history.save(session.module, source, f"Voltou para {version} em {session.controller.apelido}")
        session.original = source
        session.path.write_text(source, encoding="utf-8", newline="\n")


def _history_markdown(controller: Controller, module: str, entries: list) -> str:
    if not entries:
        return f"Sem histórico de {module} em {controller.apelido}."
    lines = [f"## Histórico de {module} ({controller.apelido})", "", "| versão | quando | o quê |", "|---|---|---|"]
    lines += [f"| `{commit}` | {date} | {message} |" for commit, date, message in entries]
    return "\n".join(lines)


async def rapid_history(conversation: Conversation, words: list[str]) -> None:
    session: EditSession | None = getattr(conversation, "hefesto_edit", None)
    if len(words) >= 2:
        controller, module = _controller(conversation.app, words[0]), words[1]
    elif session is not None:
        controller, module = session.controller, session.module
    else:
        await _note(conversation, "Use: /rapid historico CONTROLADOR MÓDULO")
        return
    await _note(conversation, _history_markdown(controller, module, _history(controller).log(module)))


async def rapid_template(conversation: Conversation, words: list[str]) -> None:
    from toad.hefesto_rapid import templates

    available = templates()
    if not words or words[0] not in available:
        names = ", ".join(f"`{name}`" for name in available)
        await _note(conversation, f"Modelos: {names}. Use `/rapid modelo NOME`.")
        return
    path = available[words[0]]
    await _note(conversation, f"## Modelo {words[0]}\n\n```rapid\n{path.read_text(encoding='utf-8').rstrip()}\n```")
    _prefill(conversation, f"Use o modelo RAPID em {path} como base (adapte nomes, sinais e alvos da célula) para: ")


# ---------------------------------------------------------------------- points


async def point(conversation: Conversation, parameters: str) -> None:
    from toad.hefesto_rapid import insert_declaration, robtarget_declaration
    from toad.hefesto_robot import RobotError

    words = parameters.split()
    if not words:
        conversation.notify("Use: /ponto NOME [controlador]", title="/ponto", severity="error")
        return
    app = conversation.app
    session: EditSession | None = getattr(conversation, "hefesto_edit", None)
    try:
        controller = _controller(app, words[1] if len(words) > 1 else (session.controller.apelido if session else None))
        code, out, err = await run_abbctl(_registry(app), controller, "pos", "--rapid", "--json")
        if code != 0:
            raise RobotError(err or out)
        data = json.loads(out)
        declaration = robtarget_declaration(words[0], data["robtarget"], data["tool"], data["wobj"])
    except (RobotError, ValueError, KeyError) as error:
        conversation.notify(str(error), title="/ponto", severity="error")
        return
    if session is not None and session.controller.apelido == controller.apelido:
        source = session.path.read_text(encoding="utf-8")
        session.path.write_text(insert_declaration(source, declaration, words[0]), encoding="utf-8", newline="\n")
        where = f"na cópia de trabalho de {session.module} (revise com `/rapid revisar`)"
    else:
        app.copy_to_clipboard(declaration.strip())
        where = "na área de transferência (abra uma edição com `/rapid editar` para inserir direto)"
    await _note(conversation, f"Ponto **{words[0]}** de {controller.apelido}, {where}:\n\n```rapid\n{declaration.strip()}\n```")


# ------------------------------------------------------------------------- I/O


async def io(conversation: Conversation, parameters: str) -> None:
    from toad.hefesto_robot import RobotError

    words = parameters.split()
    try:
        controller = _controller(conversation.app, words[0] if words else None)
        code, out, err = await run_abbctl(_registry(conversation.app), controller, "io", "list", "--json")
        if code != 0:
            raise RobotError(err or out)
        signals = json.loads(out)
    except (RobotError, ValueError) as error:
        conversation.notify(str(error), title="/io", severity="error")
        return
    wanted = words[1].lower() if len(words) > 1 else ""
    signals = [s for s in signals if wanted in str(s.get("name", "")).lower()]
    watched = set(controller.sinais) | ({controller.ciclo} if controller.ciclo else set())
    lines = [f"## I/O de {controller.apelido}" + (f" (filtro: {wanted})" if wanted else ""), "",
             "| sinal | tipo | valor |", "|---|---|---|"]
    for signal in signals[:80]:
        name = signal.get("name", "")
        mark = " 👁" if name in watched else ""
        lines.append(f"| `{name}`{mark} | {signal.get('type', '')} | **{signal.get('value', '')}** |")
    if len(signals) > 80:
        lines.append(f"\n… e mais {len(signals) - 80}. Use um filtro: `/io {controller.apelido} DO_`.")
    lines.append("\n👁 = acompanhado ao vivo (cadastro: `hefesto robo sinais`).")
    await _note(conversation, "\n".join(lines))


# --------------------------------------------------------------------- explain


async def explain(conversation: Conversation, parameters: str) -> None:
    from toad.hefesto_robot import RobotError

    app = conversation.app
    try:
        controller = _controller(app, parameters.strip() or None)
    except RobotError as error:
        conversation.notify(str(error), title="/explicar", severity="error")
        return
    monitor = getattr(app, "hefesto_monitor", None)
    status = monitor.statuses.get(controller.apelido) if monitor is not None else None
    error = status.last_error if status is not None else None
    if not error:
        await _note(conversation, f"Nenhum erro registrado em {controller.apelido} desde que o Hefesto abriu.")
        return
    snippet = ""
    pointer = str(error.get("pp") or "")
    if match := re.match(r"(\w+)/(\w+):(\d+)", pointer):
        code, source, _ = await run_abbctl(_registry(app), controller, "prog", "cat", match.group(1))
        if code == 0:
            lines = source.replace("\r\n", "\n").splitlines()
            row = int(match.group(3))
            start = max(row - 8, 0)
            snippet = "\n".join(
                f"{'>>' if index + 1 == row else '  '} {index + 1:4} {line}"
                for index, line in enumerate(lines[start:row + 6], start)
            )
    prompt = (
        f"Explique este erro do controlador {controller.apelido} ({controller.kind_label}) e proponha a "
        "correção. Não altere nada no robô nem nos arquivos; se a correção for no programa, diga o que "
        "mudar para eu fazer com /rapid editar.\n\n"
        f"Evento {error.get('code', '')}: {error.get('title', '')}\n{error.get('body', '')}\n"
        f"Ponteiro do programa: {pointer or 'desconhecido'}\n"
    )
    if snippet:
        prompt += f"\nTrecho do programa (>> = linha do ponteiro):\n```rapid\n{snippet}\n```"
    _prefill(conversation, prompt, submit=True)


# ----------------------------------------------------------------------- cycle


def cycle_summary(controller: Controller, cycles: list[float]) -> str:
    if not cycles:
        how = f"sinal {controller.ciclo}" if controller.ciclo else "início → fim do programa"
        return f"{controller.apelido}: nenhum ciclo medido ainda ({how})."
    last = cycles[-1]
    mean = sum(cycles) / len(cycles)
    spread = (sum((c - mean) ** 2 for c in cycles) / len(cycles)) ** 0.5
    bars = "▁▂▃▄▅▆▇█"
    low, high = min(cycles[-30:]), max(cycles[-30:])
    spark = "".join(bars[int((c - low) / (high - low) * 7) if high > low else 3] for c in cycles[-30:])
    return (f"{controller.apelido}: último {last:.1f} s · média {mean:.1f} s · desvio {spread:.1f} s · "
            f"{len(cycles)} ciclos  {spark}")


async def cycle(conversation: Conversation, parameters: str) -> None:
    from toad.hefesto_robot import RobotError

    app = conversation.app
    monitor = getattr(app, "hefesto_monitor", None)
    try:
        controllers = [_controller(app, parameters.strip())] if parameters.strip() else _registry(app).controladores
    except RobotError as error:
        conversation.notify(str(error), title="/ciclo", severity="error")
        return
    lines = ["## Tempo de ciclo", ""]
    for controller in controllers:
        status = monitor.statuses.get(controller.apelido) if monitor is not None else None
        lines.append(f"- {cycle_summary(controller, status.cycles if status else [])}")
        if status is not None and status.cycles:
            mean = sum(status.cycles) / len(status.cycles)
            if len(status.cycles) >= 5 and abs(status.cycles[-1] - mean) > 0.2 * mean:
                lines.append(f"  ⚠ o último ciclo está {status.cycles[-1] - mean:+.1f} s fora da média")
    await _note(conversation, "\n".join(lines))


# ---------------------------------------------------------------- the hook


async def run(conversation: Conversation, command: str, parameters: str) -> bool:
    """Called by Conversation.slash_command; True when the command was Hefesto's."""
    match command:
        case "rapid":
            await rapid(conversation, parameters)
        case "ponto":
            await point(conversation, parameters)
        case "io":
            await io(conversation, parameters)
        case "explicar":
            await explain(conversation, parameters)
        case "ciclo":
            await cycle(conversation, parameters)
        case _:
            return False
    return True


# ---------------------------------------------------------------- alarms & voice


def install_alarms(app: Any) -> None:
    """Hard stops and errors on any controller: on screen and, if the voice announces, spoken."""
    monitor = getattr(app, "hefesto_monitor", None)
    if monitor is None:
        return

    def alarm(controller: Controller, text: str) -> None:
        app.notify(text, title="Robô", severity="error", timeout=10)
        voice = getattr(app, "iris_voice", None)
        if voice is not None and getattr(voice, "announces", False):
            voice.say(text)

    monitor.on_alarm = alarm


VOICE_PATTERNS: list[tuple[str, str]] = [
    (r"\b(status|situacao|como esta)\b.*\b(celula|robo|controlador)\b(?P<arg>.*)$", "robo_status"),
    (r"\b(ultimo|qual (foi )?o) erro\b(?P<arg>.*)$", "robo_erro"),
    (r"\btempo de ciclo\b(?P<arg>.*)$", "robo_ciclo"),
]
"""Read-only voice commands, checked before Íris' own ("Vulcano, status da célula 3")."""


def _voice_controller(app: Any, spoken: str) -> Controller | None:
    registry = _registry(app)
    from toad.iris_commands import normalize

    spoken = normalize(spoken)
    for controller in registry.controladores:
        name = normalize(controller.apelido.replace("-", " ").replace("_", " "))
        if name and (name in spoken or any(part in spoken.split() for part in name.split() if part.isdigit())):
            return controller
    try:
        return _controller(app, None)
    except Exception:
        return None


def voice_command(app: Any, name: str, argument: str) -> str | None:
    """Answer a read-only robot question; the text is spoken back."""
    from toad.hefesto_robot import describe

    controller = _voice_controller(app, argument)
    if controller is None:
        return "Não sei de qual controlador você fala."
    monitor = getattr(app, "hefesto_monitor", None)
    status = monitor.statuses.get(controller.apelido) if monitor is not None else None
    if name == "robo_status":
        if status is None or not status.connected:
            return f"{controller.apelido} está sem conexão."
        program = "rodando" if status.running else "parado"
        speed = f", velocidade {status.speed} por cento" if status.speed is not None else ""
        return f"{controller.apelido}: modo {status.opmode}, motores {status.motors}, programa {program}{speed}."
    if name == "robo_erro":
        if status is None or not status.last_error:
            return f"Nenhum erro em {controller.apelido} desde que abri."
        return f"Último erro em {controller.apelido}: {status.last_error.get('title', '')}."
    if name == "robo_ciclo":
        return cycle_summary(controller, status.cycles if status else []).split("  ")[0]
    return None
