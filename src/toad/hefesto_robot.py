"""Hefesto: ABB controllers — registry, live monitor, MCP servers and the agent's context.

The registry lives in the config directory, never in a repository (addresses and
names are factory data):

    hefesto-controladores.json
    {"abbctl": "C:/ferramentas/abbctl/abbctl.exe",
     "abbctl_mcp": "C:/ferramentas/abbctl/abbctl-mcp.exe",
     "controladores": [
        {"apelido": "celula3-solda", "endereco": "192.168.125.1", "tipo": "pcsdk",
         "real": true, "escrita": false, "descricao": "IRB 6700, solda ponto"}]}

`real` is declared by the user (the guess from the network can be fooled); a real
controller starts read-only (`escrita: false`) until the user allows writes.

For each controller the HMI runs a read-only monitor (`abbctl monitor`, one NDJSON
line per change) that feeds the stack light, the arm and the "Robôs" panel; the
agent gets one MCP server per controller (`abb-<apelido>`), so tools can't be
pointed at the wrong robot, plus a summary of every controller in its first prompt.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from toad.widgets.hefesto_robots import RobotStatus

FILE_NAME = "hefesto-controladores.json"
TYPES = {
    "pcsdk": "PC SDK",
    "rws": "RWS 1.0",
    "rws2": "RWS 2.0",
}
"""How Hefesto talks to the controller. PC SDK works with IRC5 (option 616-1 PC
Interface) and OmniCore (option 3119-1 RobotStudio Connect); virtual controllers
need neither."""


def family(robotware: str) -> str:
    """IRC5 (RobotWare 6) or OmniCore (RobotWare 7+), from the version string."""
    try:
        return "OmniCore" if int(robotware.split(".")[0]) >= 7 else "IRC5"
    except ValueError:
        return ""
RESTART_SECONDS = 5.0
EVENTS_KEPT = 40


class RobotError(Exception):
    """Invalid use of the registry (unknown name, duplicate...)."""


@dataclass
class Controller:
    apelido: str
    endereco: str
    tipo: str = "pcsdk"
    real: bool = True
    """Declared by the user. Unknown controllers are treated as real."""
    escrita: bool = False
    """Writes allowed on a real controller (virtual ones always allow them)."""
    descricao: str = ""
    gemeo: str = ""
    """Virtual twin (apelido of a virtual controller restored from this one's backup):
    edits are loaded and tried there before the real robot."""
    sinais: list[str] = field(default_factory=list)
    """Signals watched live (I/O panel, alarms)."""
    ciclo: str = ""
    """Signal whose rising edge starts a cycle; empty: a cycle is program start → stop."""

    @property
    def writable(self) -> bool:
        return self.escrita or not self.real

    @property
    def kind_label(self) -> str:
        return "REAL" if self.real else "VIRTUAL"


@dataclass
class Registry:
    controladores: list[Controller] = field(default_factory=list)
    abbctl: str = ""
    abbctl_mcp: str = ""

    def find(self, name: str) -> Controller:
        for controller in self.controladores:
            if controller.apelido.lower() == name.lower():
                return controller
        known = ", ".join(c.apelido for c in self.controladores) or "nenhum"
        raise RobotError(f'Controlador "{name}" não cadastrado (cadastrados: {known}).')

    def add(self, controller: Controller) -> None:
        if controller.tipo not in TYPES:
            raise RobotError(f"Tipo inválido: {controller.tipo} (use {', '.join(TYPES)}).")
        if any(c.apelido.lower() == controller.apelido.lower() for c in self.controladores):
            raise RobotError(f'Já existe um controlador "{controller.apelido}".')
        self.controladores.append(controller)

    def select(self, names: Iterable[str] | None) -> list[Controller]:
        """The named controllers (a project's), or all of them."""
        if not names:
            return list(self.controladores)
        wanted = {name.lower() for name in names}
        return [c for c in self.controladores if c.apelido.lower() in wanted]


def registry_path() -> Path:
    from toad import paths

    return paths.get_config() / FILE_NAME


def load_registry(path: Path | None = None) -> Registry:
    path = path or registry_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Registry()
    controllers = []
    for item in data.get("controladores", []):
        if isinstance(item, dict) and item.get("apelido") and item.get("endereco"):
            known = {key: item[key] for key in Controller.__dataclass_fields__ if key in item}
            controllers.append(Controller(**known))
    return Registry(controllers, str(data.get("abbctl", "")), str(data.get("abbctl_mcp", "")))


def save_registry(registry: Registry, path: Path | None = None) -> None:
    path = path or registry_path()
    data = {
        "abbctl": registry.abbctl,
        "abbctl_mcp": registry.abbctl_mcp,
        "controladores": [asdict(controller) for controller in registry.controladores],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


# ------------------------------------------------------------------------- tools

TOOL_FOLDERS = (
    Path("C:/ferramentas/abbctl"),
    Path.home() / "abbctl" / "src" / "abbctl" / "bin" / "Release",
    Path.home() / "abbctl" / "src" / "abbctl-mcp" / "bin" / "Release",
)


def find_tool(configured: str, name: str) -> str | None:
    """`configured` if it exists, else `name` on PATH or in the usual folders."""
    if configured and Path(configured).is_file():
        return configured
    if found := shutil.which(name):
        return found
    for folder in TOOL_FOLDERS:
        if (candidate := folder / f"{name}.exe").is_file():
            return str(candidate)
    return None


def monitor_command(registry: Registry, controller: Controller) -> list[str] | None:
    """Command that streams this controller's state as NDJSON, or None if unsupported."""
    if controller.tipo == "pcsdk":
        if abbctl := find_tool(registry.abbctl, "abbctl"):
            command = [abbctl, "-c", controller.endereco, "monitor", "--interval", "1", "--parent-stdin"]
            if signals := list(dict.fromkeys([*controller.sinais, *([controller.ciclo] if controller.ciclo else [])])):
                command += ["--io", ",".join(signals)]
            return command
        return None
    if abbrwsctl := shutil.which("abbrwsctl"):
        command = [abbrwsctl, "--host", controller.endereco, "monitor"]
        if controller.tipo == "rws2":
            command.append("--rws2")
        command += ["--assume-virtual", "false" if controller.real else "true"]
        return command
    return None


def mcp_servers(registry: Registry, controllers: Iterable[Controller]) -> list[dict]:
    """One MCP server per controller, in ACP format, named `abb-<apelido>`."""
    servers = []
    for controller in controllers:
        env = {"HEFESTO_REAL": "1" if controller.real else "0", "ABBCTL_LOCKED": "1",
               "ABBCTL_READONLY": "0" if controller.writable else "1"}
        if controller.tipo == "pcsdk":
            command = find_tool(registry.abbctl_mcp, "abbctl-mcp")
            if command is None:
                continue
            env["ABBCTL_CONTROLLER"] = controller.endereco
            args: list[str] = []
        else:
            command = shutil.which("abbrwsctl-mcp")
            if command is None:
                continue
            args = ["--host", controller.endereco,
                    "--assume-virtual", "false" if controller.real else "true"]
            if controller.tipo == "rws2":
                args.append("--rws2")
        servers.append({
            "name": f"abb-{controller.apelido}",
            "command": command,
            "args": args,
            "env": [{"name": key, "value": value} for key, value in env.items()],
        })
    return servers


# ------------------------------------------------------------------------ status


CYCLES_KEPT = 50


def _cycle_mark(status: RobotStatus, now: float) -> None:
    """A cycle starts now: close the previous one."""
    if status.cycle_started is not None and now > status.cycle_started:
        status.cycles.append(now - status.cycle_started)
        del status.cycles[:-CYCLES_KEPT]
    status.cycle_started = now


def apply_event(
    status: RobotStatus, event: dict[str, Any], now: float | None = None, cycle_signal: str = ""
) -> None:
    """Fold one monitor line into `status`."""
    now = time.monotonic() if now is None else now
    kind = event.get("type")
    value = event.get("value")
    if kind == "exec" and not cycle_signal:
        # Without a cycle signal, a cycle is one program run (start → stop).
        running = str(value).lower() == "running"
        if running and not status.running:
            status.cycle_started = now
        elif not running and status.running and status.cycle_started is not None:
            status.cycles.append(now - status.cycle_started)
            del status.cycles[:-CYCLES_KEPT]
            status.cycle_started = None
    if kind == "snapshot":
        status.connected = True
        status.error = ""
        status.name = str(event.get("systemName", ""))
        status.opmode = str(event.get("operatingMode", ""))
        status.motors = str(event.get("state", ""))
        status.running = str(event.get("executionStatus", "")).lower() == "running"
        status.speed = _int(event.get("speedRatio"))
        status.joints = _joints(event.get("joints"))
        status.robotware = str(event.get("robotwareVersion", ""))
        status.detected_virtual = event.get("virtual")
        status.task = str(event.get("task") or "")
        status.pp = str(event.get("pp") or "")
    elif kind == "state":
        status.motors = str(value)
    elif kind == "opmode":
        status.opmode = str(value)
    elif kind == "exec":
        status.running = str(value).lower() == "running"
    elif kind == "speed":
        status.speed = _int(value)
    elif kind == "joints":
        status.joints = _joints(value)
    elif kind == "pp":
        status.pp = str(value or "")
    elif kind == "io":
        name, new = str(event.get("name", "")), str(value)
        if name == cycle_signal and new not in ("0", "False") and status.io.get(name) in ("0", "False"):
            _cycle_mark(status, now)
        status.io[name] = new
    elif kind == "log":
        severity = str(event.get("severity", "info"))
        if severity == "error":
            status.last_error = {key: event.get(key, "") for key in ("code", "title", "body")}
            status.last_error["pp"] = status.pp
        status.events.append((now, severity))
        status.log.append(f"{event.get('code', '')} {event.get('title', '')}".strip())
        del status.events[:-EVENTS_KEPT]
        del status.log[:-EVENTS_KEPT]
    elif kind == "disconnected":
        status.connected = False
        status.error = "controlador desconectou"


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _joints(value: Any) -> tuple[float, ...] | None:
    if isinstance(value, list) and len(value) >= 6:
        try:
            return tuple(float(item) for item in value)
        except (TypeError, ValueError):
            return None
    return None


def describe(controller: Controller, status: RobotStatus | None) -> str:
    """One line for the agent and the panel: mode, motors, program, speed."""
    head = f"{controller.apelido} · {controller.kind_label}"
    if status is not None and (name := family(status.robotware)):
        head += f" · {name} RobotWare {status.robotware}"
    head += f" · via {TYPES.get(controller.tipo, controller.tipo)}"
    if status is None or not status.connected:
        reason = status.error if status is not None and status.error else "sem conexão ainda"
        return f"{head} · {reason}"
    parts = [head, status.opmode or "?", status.motors or "?",
             "programa rodando" if status.running else "programa parado"]
    if status.speed is not None:
        parts.append(f"velocidade {status.speed}%")
    if status.pp:
        parts.append(f"ponteiro {status.pp}")
    if status.cycles:
        parts.append(f"ciclo médio {sum(status.cycles) / len(status.cycles):.1f} s")
    if status.joints:
        parts.append("eixos " + " ".join(f"{value:.1f}" for value in status.joints))
    if controller.real and status.detected_virtual:
        parts.append("ATENÇÃO: cadastrado como REAL, mas o controlador diz ser virtual")
    return " · ".join(parts)


def context_prompt(controllers: list[Controller], statuses: dict[str, RobotStatus]) -> str:
    """What the agent is told about the robots, ahead of the first prompt."""
    if not controllers:
        return ""
    lines = ["[Contexto dos robôs ABB (leitura feita pelo Hefesto ao iniciar)]"]
    for controller in controllers:
        lines.append(f"- {describe(controller, statuses.get(controller.apelido))}")
        if controller.descricao:
            lines.append(f"  {controller.descricao}")
        if controller.gemeo:
            lines.append(f"  Gêmeo virtual: {controller.gemeo} (teste lá antes de mudar o real).")
        if status := statuses.get(controller.apelido):
            if status.log:
                lines.append("  Últimos eventos: " + " | ".join(status.log[-3:]))
    lines.append(
        "Cada controlador tem o próprio servidor MCP, `abb-<apelido>`: use o do "
        "controlador certo. Nos REAIS: só leitura, a menos que a escrita esteja "
        "liberada; mesmo liberada, peça confirmação explícita ao usuário antes de "
        "iniciar ou parar programa, carregar módulo, mudar I/O, variável ou velocidade "
        "(um programa iniciado move uma máquina de verdade). Nos VIRTUAIS: pode testar."
    )
    for controller in controllers:
        if controller.real and not controller.writable:
            lines.append(f"- {controller.apelido}: SOMENTE LEITURA (escrita bloqueada).")
    lines.append("[Fim do contexto dos robôs]")
    return "\n".join(lines)


# ----------------------------------------------------------------------- monitor


class RobotMonitor:
    """Keeps one monitor process per controller and the latest status of each."""

    def __init__(self, registry: Registry, on_change: Callable[[], None] | None = None):
        self.registry = registry
        self.statuses: dict[str, RobotStatus] = {
            c.apelido: RobotStatus() for c in registry.controladores
        }
        self.focus: str | None = registry.controladores[0].apelido if registry.controladores else None
        self.on_change = on_change
        self._tasks: list[asyncio.Task] = []
        self._processes: list[asyncio.subprocess.Process] = []

    @property
    def focused(self) -> RobotStatus | None:
        return self.statuses.get(self.focus) if self.focus else None

    def set_focus(self, names: Iterable[str] | None) -> None:
        """Focus the first controller of a project (or keep the current one)."""
        for name in names or ():
            for controller in self.registry.controladores:
                if controller.apelido.lower() == name.lower():
                    self.focus = controller.apelido
                    return

    def start(self) -> None:
        for controller in self.registry.controladores:
            self._tasks.append(asyncio.create_task(self._watch(controller)))

    async def stop(self) -> None:
        self.kill()
        for process in self._processes:
            try:
                await asyncio.wait_for(process.wait(), 5)
            except (asyncio.TimeoutError, ProcessLookupError):
                pass

    def kill(self) -> None:
        """Stop the monitors now (also safe while the app is tearing down)."""
        for task in self._tasks:
            task.cancel()
        for process in self._processes:
            if process.returncode is None:
                try:
                    process.kill()
                except (ProcessLookupError, OSError):
                    pass

    def _changed(self) -> None:
        if self.on_change is not None:
            self.on_change()

    on_alarm: Callable[[Controller, str], None] | None = None
    """Called with a short sentence when a controller stops hard or logs an error."""

    def _alarm(self, controller: Controller, status: RobotStatus, before: tuple, event: dict) -> None:
        if self.on_alarm is None or event.get("type") == "snapshot":
            return
        stopped_before, error_before = before
        if status.stopped_hard and not stopped_before:
            self.on_alarm(controller, f"{controller.apelido}: {status.motors}")
        elif status.last_error is not None and status.last_error is not error_before:
            self.on_alarm(controller, f"{controller.apelido}: erro {status.last_error.get('title', '')}")

    async def _watch(self, controller: Controller) -> None:
        status = self.statuses[controller.apelido]
        while True:
            command = monitor_command(self.registry, controller)
            if command is None:
                status.connected = False
                status.error = (
                    "abbctl não encontrado (veja `hefesto robo ferramentas`)"
                    if controller.tipo == "pcsdk"
                    else "abbrwsctl não instalado"
                )
                self._changed()
                return
            try:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    # Kept open: when Hefesto goes away the monitor sees EOF and exits.
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except OSError as error:
                status.connected = False
                status.error = f"não consegui iniciar o monitor: {error}"
                self._changed()
                await asyncio.sleep(RESTART_SECONDS * 6)
                continue
            self._processes.append(process)
            assert process.stdout is not None
            async for raw in process.stdout:
                try:
                    event = json.loads(raw.decode("utf-8", "replace"))
                except ValueError:
                    continue
                if isinstance(event, dict):
                    before = (status.stopped_hard, status.last_error)
                    apply_event(status, event, cycle_signal=controller.ciclo)
                    self._changed()
                    self._alarm(controller, status, before, event)
            await process.wait()
            stderr = b""
            if process.stderr is not None:
                stderr = await process.stderr.read()
            status.connected = False
            message = stderr.decode("utf-8", "replace").strip().splitlines()
            status.error = (message[-1] if message else "monitor parou")[:160]
            self._changed()
            await asyncio.sleep(RESTART_SECONDS)


# ----------------------------------------------------------------------- the app


def project_controllers(project: Any) -> list[str]:
    return list(getattr(project, "controladores", None) or [])


def install(app: Any) -> RobotMonitor | None:
    """Start monitoring every registered controller (called when the app mounts)."""
    registry = load_registry()
    app.hefesto_registry = registry
    if not registry.controladores:
        app.hefesto_monitor = None
        app.hefesto_robot_status = None
        return None
    monitor = RobotMonitor(registry)
    app.hefesto_monitor = monitor
    app.hefesto_robot_status = monitor.focused
    monitor.start()
    # However the app ends (quit, crash, test harness), don't leave monitors behind.
    import atexit

    atexit.register(monitor.kill)
    return monitor


def focus_project(app: Any, project: Any) -> None:
    """Point the animation at the project's first controller."""
    monitor: RobotMonitor | None = getattr(app, "hefesto_monitor", None)
    if monitor is not None:
        monitor.set_focus(project_controllers(project))
        app.hefesto_robot_status = monitor.focused


def session_controllers(app: Any, project: Any) -> list[Controller]:
    registry: Registry = getattr(app, "hefesto_registry", None) or load_registry()
    return registry.select(project_controllers(project))


def session_mcp_servers(app: Any, project: Any) -> list[dict] | None:
    """The project's MCP servers plus one per controller of the session."""
    registry: Registry = getattr(app, "hefesto_registry", None) or load_registry()
    servers = project.mcp_servers() if project is not None else []
    taken = {server["name"] for server in servers}
    servers += [
        server
        for server in mcp_servers(registry, session_controllers(app, project))
        if server["name"] not in taken
    ]
    return servers or None


def session_context(app: Any, project: Any) -> str:
    """Project and robot context for the first prompt ("" when there is neither)."""
    parts = []
    if project is not None:
        parts.append(project.context_prompt())
    monitor: RobotMonitor | None = getattr(app, "hefesto_monitor", None)
    statuses = monitor.statuses if monitor is not None else {}
    if robots := context_prompt(session_controllers(app, project), statuses):
        parts.append(robots)
    return "\n\n".join(parts)
