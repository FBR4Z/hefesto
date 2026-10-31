"""Íris projects: folders and files that belong together, with a description.

A project is found from the folder a session opens in. When it is, Íris:

- tells the agent, in the first prompt, what the project is and which folders and
  files are part of it (they may live anywhere on disk);
- gives the session the project's MCP servers (e.g. ESP-IDF build/flash, docs).

Stored in `iris-projetos.json`, in the config directory (next to `toad.json`):

    {"projetos": [{"nome": "Estufa", "descricao": "...", "pastas": ["C:/esp/estufa"],
                   "arquivos": ["C:/docs/pinagem.md"], "agente": "claude",
                   "mcp": [{"name": "esp-idf", "command": "eim",
                            "args": ["run", "idf.py mcp-server"]}]}]}
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

FILE_NAME = "iris-projetos.json"


class ProjectError(Exception):
    """Invalid use of a project (unknown name, duplicate...)."""


@dataclass
class Project:
    nome: str
    descricao: str = ""
    pastas: list[str] = field(default_factory=list)
    arquivos: list[str] = field(default_factory=list)
    mcp: list[dict] = field(default_factory=list)
    """MCP servers in ACP format (`name`, `command`, `args`, `env` or `type`, `url`)."""
    agente: str = ""
    """Default agent for `iris projeto abrir` (e.g. claude, codex, gemini)."""
    controladores: list[str] = field(default_factory=list)
    """Hefesto: robot controllers of this cell (names from hefesto-controladores.json)."""

    @property
    def main_folder(self) -> Path | None:
        """Where sessions for this project open: the first folder that exists."""
        for folder in self.pastas:
            if Path(folder).is_dir():
                return Path(folder)
        return None

    def add_path(self, path: Path) -> str:
        """Add a folder or a file. Returns "pasta" or "arquivo"."""
        path = path.expanduser().resolve()
        if not path.exists():
            raise ProjectError(f"Não encontrei {path}")
        kind, items = ("pasta", self.pastas) if path.is_dir() else ("arquivo", self.arquivos)
        if not any(same_path(Path(item), path) for item in items):
            items.append(str(path))
        return kind

    def remove_path(self, path: Path) -> bool:
        path = path.expanduser().resolve()
        removed = False
        for items in (self.pastas, self.arquivos):
            for item in list(items):
                if same_path(Path(item), path):
                    items.remove(item)
                    removed = True
        return removed

    def mcp_servers(self) -> list[dict]:
        """MCP servers ready for ACP `session/new` (missing fields filled in)."""
        servers: list[dict] = []
        for server in self.mcp:
            if not isinstance(server, dict) or not server.get("name"):
                continue
            server = dict(server)
            if server.get("type", "stdio") == "stdio":
                server.pop("type", None)
                if not server.get("command"):
                    continue
                server.setdefault("args", [])
                server["env"] = _name_values(server.get("env"))
            else:
                if not server.get("url"):
                    continue
                server["headers"] = _name_values(server.get("headers"))
            servers.append(server)
        return servers

    def context_prompt(self) -> str:
        """What the agent is told about the project, ahead of the first prompt."""
        lines = [f'[Contexto do projeto Hefesto "{self.nome}"]']
        if self.descricao:
            lines.append(f"Descrição: {self.descricao}")
        if self.pastas:
            lines.append("Pastas do projeto:")
            lines.extend(f"- {folder}" for folder in self.pastas)
        if self.arquivos:
            lines.append("Arquivos do projeto (podem estar fora das pastas):")
            lines.extend(f"- {file}" for file in self.arquivos)
        if names := [server["name"] for server in self.mcp_servers()]:
            lines.append("Servidores MCP ligados a esta sessão: " + ", ".join(names))
        lines.append("[Fim do contexto do projeto]")
        return "\n".join(lines)

    def summary_markdown(self) -> str:
        lines = [f"## Projeto {self.nome}", ""]
        if self.descricao:
            lines += [self.descricao, ""]
        lines.append("**Pastas**")
        lines += [f"- `{folder}`" for folder in self.pastas] or ["- (nenhuma)"]
        lines += ["", "**Arquivos**"]
        lines += [f"- `{file}`" for file in self.arquivos] or ["- (nenhum)"]
        if self.mcp:
            lines += ["", "**Servidores MCP**"]
            for server in self.mcp:
                target = server.get("url") or " ".join(
                    [server.get("command", "")] + list(server.get("args", []))
                )
                lines.append(f"- `{server.get('name', '?')}`: `{target}`")
        return "\n".join(lines)


def _name_values(value) -> list[dict]:
    """`{"A": "1"}` or `[{"name": "A", "value": "1"}]` → ACP's list form."""
    if isinstance(value, dict):
        return [{"name": str(key), "value": str(item)} for key, item in value.items()]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict) and "name" in item]
    return []


def same_path(first: Path, second: Path) -> bool:
    return os.path.normcase(str(first.expanduser().resolve())) == os.path.normcase(
        str(second.expanduser().resolve())
    )


def is_inside(path: Path, folder: Path) -> bool:
    path = Path(os.path.normcase(str(path.expanduser().resolve())))
    folder = Path(os.path.normcase(str(folder.expanduser().resolve())))
    return path == folder or folder in path.parents


def projects_path() -> Path:
    from toad import paths

    return paths.get_config() / FILE_NAME


def load_projects(path: Path | None = None) -> list[Project]:
    path = path or projects_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    projects: list[Project] = []
    for item in data.get("projetos", []) if isinstance(data, dict) else []:
        if not isinstance(item, dict) or not item.get("nome"):
            continue
        projects.append(
            Project(
                nome=str(item["nome"]),
                descricao=str(item.get("descricao", "")),
                pastas=[str(folder) for folder in item.get("pastas", [])],
                arquivos=[str(file) for file in item.get("arquivos", [])],
                mcp=[server for server in item.get("mcp", []) if isinstance(server, dict)],
                agente=str(item.get("agente", "")),
                controladores=[str(name) for name in item.get("controladores", [])],
            )
        )
    return projects


def save_projects(projects: list[Project], path: Path | None = None) -> None:
    path = path or projects_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"projetos": [asdict(project) for project in projects]}
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def find_project(projects: list[Project], name: str) -> Project | None:
    wanted = name.strip().casefold()
    for project in projects:
        if project.nome.casefold() == wanted:
            return project
    matches = [project for project in projects if project.nome.casefold().startswith(wanted)]
    return matches[0] if len(matches) == 1 else None


def match_project(projects: list[Project], spoken: str) -> Project | None:
    """Find a project from a dictated name ("o mestrado", "OT modbus", "mestrad")."""
    if project := find_project(projects, spoken):
        return project
    from difflib import get_close_matches

    from toad.iris_commands import normalize

    names = {normalize(project.nome): project for project in projects}
    wanted = normalize(spoken)
    for name, project in names.items():
        if name and f" {name} " in f" {wanted} ":
            return project
    close = get_close_matches(wanted, list(names), n=1, cutoff=0.6)
    return names[close[0]] if close else None


# `agente` in the projects file is a short name.
AGENT_IDENTITIES = {"claude": "claude.com", "codex": "openai.com", "gemini": "geminicli.com"}


def project_agent(project: Project, fallback: str = "claude.com") -> str:
    """Identity of the agent a project opens with (its `agente`, else `fallback`)."""
    agent = project.agente.strip().lower()
    return AGENT_IDENTITIES.get(agent, agent) if agent else fallback


def open_project(app, project: Project, agent_identity: str) -> None:
    """Start a session of `agent_identity` in the project's main folder."""
    from toad import messages

    if (folder := project.main_folder) is None:
        raise ProjectError(f'O projeto "{project.nome}" não tem nenhuma pasta que exista.')
    app.iris_project_preferred = project.nome
    app.post_message(messages.SessionNew(str(folder), agent_identity, ""))


def project_for_path(
    projects: list[Project], path: Path, preferred: str | None = None
) -> Project | None:
    """The project a folder belongs to (the most specific folder wins).

    Args:
        preferred: Name of a project to pick if it also contains `path`.
    """
    best: tuple[int, Project] | None = None
    for project in projects:
        for folder in project.pastas:
            if not is_inside(path, Path(folder)):
                continue
            depth = len(Path(folder).expanduser().resolve().parts)
            if preferred and project.nome.casefold() == preferred.casefold():
                return project
            if best is None or depth > best[0]:
                best = (depth, project)
    return best[1] if best else None


def new_project(
    projects: list[Project], name: str, description: str = "", paths: Iterable[Path] = ()
) -> Project:
    name = name.strip()
    if not name:
        raise ProjectError("Dê um nome ao projeto.")
    if any(project.nome.casefold() == name.casefold() for project in projects):
        raise ProjectError(f'Já existe um projeto "{name}".')
    project = Project(nome=name, descricao=description.strip())
    for path in paths:
        project.add_path(path)
    projects.append(project)
    return project


def parse_mcp_command(name: str, command: list[str], env: Iterable[str] = ()) -> dict:
    """Build an MCP server entry from a command line or a URL."""
    if not name or not command:
        raise ProjectError("Informe o nome do servidor e o comando (ou a URL).")
    if len(command) == 1 and command[0].startswith(("http://", "https://")):
        return {"type": "http", "name": name, "url": command[0], "headers": []}
    variables = {}
    for item in env:
        key, equals, value = item.partition("=")
        if not equals:
            raise ProjectError(f"Variável inválida {item!r}; use NOME=valor.")
        variables[key] = value
    server: dict = {"name": name, "command": command[0], "args": command[1:]}
    if variables:
        server["env"] = variables
    return server
