import sys

if sys.platform == "win32":
    # Windows consoles default to a legacy codepage, which can't encode emoji / accents.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

import click

import toad
from toad.app import ToadApp
from toad.agent_schema import Agent


def set_process_title(title: str) -> None:
    """Set the process title.

    Args:
        title: Desired title.
    """
    try:
        import setproctitle

        setproctitle.setproctitle(title)
    except Exception:
        pass


def check_directory(path: str) -> None:
    """Check a path is directory, or exit the app.

    Args:
        path: Path to check.
    """
    from pathlib import Path

    if not Path(path).resolve().is_dir():
        print(f"Not a directory: {path}")
        sys.exit(-1)


async def get_agent_data(launch_agent) -> Agent | None:
    launch_agent = launch_agent.lower()

    from toad.agents import read_agents, AgentReadError

    try:
        agents = await read_agents()
    except AgentReadError:
        agents = {}

    for agent_data in agents.values():
        if (
            agent_data["short_name"].lower() == launch_agent
            or agent_data["identity"].lower() == launch_agent
        ):
            launch_agent = agent_data["identity"]
            break

    return agents.get(launch_agent)


class DefaultCommandGroup(click.Group):
    def parse_args(self, ctx, args):
        if "--help" in args or "-h" in args:
            return super().parse_args(ctx, args)
        if "--version" in args or "-v" in args:
            return super().parse_args(ctx, args)
        # Check if first arg is a known subcommand
        if not args or args[0] not in self.commands:
            # If not a subcommand, prepend the default command name
            args.insert(0, "run")
        return super().parse_args(ctx, args)

    def format_usage(self, ctx, formatter):
        formatter.write_usage(ctx.command_path, "[OPTIONS] PATH OR COMMAND [ARGS]...")


@click.group(cls=DefaultCommandGroup, invoke_without_command=True)
@click.option("-v", "--version", is_flag=True, help="Show version and exit.")
@click.pass_context
def main(ctx, version):
    """Hefesto — Claude, Codex e Gemini no chão de fábrica, com os robôs ABB no contexto."""
    from toad.iris_crash import install_fault_handler

    install_fault_handler()
    if version:
        from toad import get_version

        click.echo(get_version())
        ctx.exit()
    # If no command and no version flag, let the default command handling proceed
    if ctx.invoked_subcommand is None and not version:
        pass


# @click.group(invoke_without_command=True)
# @click.pass_context
@main.command("run")
@click.argument("project_dir", metavar="PATH", required=False, default=".")
@click.option("-a", "--agent", metavar="AGENT", default="")
@click.option(
    "-p",
    "--port",
    metavar="PORT",
    default=8000,
    type=int,
    help="Port to use in conjunction with --serve",
)
@click.option(
    "-H",
    "--host",
    metavar="HOST",
    default="localhost",
    type=str,
    help="Host to use in conjunction with --serve",
)
@click.option(
    "--public-url",
    metavar="URL",
    default=None,
    help="Public URL to use in conjunction with --serve",
)
@click.option("-s", "--serve", is_flag=True, help="Serve Toad as a web application")
@click.option(
    "--projeto",
    metavar="NOME",
    default="",
    help="Abre na pasta principal de um projeto do Hefesto (veja `hefesto projeto`).",
)
def run(
    port: int,
    host: str,
    serve: bool,
    project_dir: str = ".",
    agent: str = "1",
    public_url: str | None = None,
    projeto: str = "",
):
    """Run an installed agent (same as `toad PATH`)."""

    if projeto:
        project = _get_project(projeto)
        if (folder := project.main_folder) is None:
            raise click.ClickException(
                f'O projeto "{project.nome}" não tem nenhuma pasta que exista.'
            )
        project_dir = str(folder)
        agent = agent or project.agente
    check_directory(project_dir)

    if agent:
        import asyncio

        agent_data = asyncio.run(get_agent_data(agent))
    else:
        agent_data = None

    app = ToadApp(
        mode=None if agent_data else "store",
        agent_data=agent_data,
        project_dir=project_dir,
    )
    if projeto:
        app.iris_project_preferred = project.nome
    if serve:
        import shlex
        from textual_serve.server import Server

        command_args = sys.argv
        # Remove serve flag from args (could be either --serve or -s)
        for flag in ["--serve", "-s"]:
            try:
                command_args.remove(flag)
                break
            except ValueError:
                pass
        serve_command = shlex.join(command_args)
        server = Server(
            serve_command,
            host=host,
            port=port,
            title=serve_command,
            public_url=public_url,
        )
        set_process_title("toad --serve")
        server.serve()
    else:
        app.run()
    app.run_on_exit()


@main.command("acp")
@click.argument("command", metavar="COMMAND")
@click.argument("project_dir", metavar="PATH", default=None)
@click.option(
    "-t",
    "--title",
    metavar="TITLE",
    help="Optional title to display in the status bar",
    default=None,
)
@click.option("-d", "--project-dir", metavar="PATH", default=None)
@click.option(
    "-p",
    "--port",
    metavar="PORT",
    default=8000,
    type=int,
    help="Port to use in conjunction with --serve",
)
@click.option(
    "-H",
    "--host",
    metavar="HOST",
    default="localhost",
    help="Host to use in conjunction with --serve",
)
@click.option("-s", "--serve", is_flag=True, help="Serve Toad as a web application")
def acp(
    command: str,
    host: str,
    port: int,
    title: str | None,
    project_dir: str | None,
    serve: bool = False,
) -> None:
    """Run an ACP agent from a command."""

    from rich import print

    from toad.agent_schema import Agent as AgentData

    command_name = command.split(" ", 1)[0].lower()
    identity = f"{command_name}.custom.batrachian.ai"

    agent_data: AgentData = {
        "identity": identity,
        "name": title or command.partition(" ")[0],
        "short_name": "agent",
        "url": "https://github.com/batrachianai/toad",
        "protocol": "acp",
        "type": "coding",
        "author_name": "Will McGugan",
        "author_url": "https://willmcgugan.github.io/",
        "publisher_name": "Will McGugan",
        "publisher_url": "https://willmcgugan.github.io/",
        "description": "Agent launched from CLI",
        "tags": [],
        "help": "",
        "run_command": {"*": command},
        "actions": {},
    }
    if serve:
        import shlex
        from textual_serve.server import Server

        command_components = [sys.argv[0], "acp", command]
        if project_dir:
            command_components.append(f"--project-dir={project_dir}")
        serve_command = shlex.join(command_components)

        server = Server(
            serve_command,
            host=host,
            port=port,
            title=serve_command,
        )
        set_process_title("toad acp --serve")
        server.serve()

    else:
        app = ToadApp(agent_data=agent_data, project_dir=project_dir)
        app.run()
        app.run_on_exit()

    print("")
    print("[bold magenta]Thanks for trying out Toad!")
    print("Please head to Discussions to share your experiences (good or bad).")
    print("https://github.com/batrachianai/toad/discussions")


def _get_project(name: str):
    from toad.iris_projects import find_project, load_projects

    if (project := find_project(load_projects(), name)) is None:
        raise click.ClickException(f'Projeto "{name}" não encontrado (veja `hefesto projeto listar`).')
    return project


def _edit_project(name: str):
    """Load all projects and the named one, for a change followed by `save_projects`."""
    from toad.iris_projects import find_project, load_projects

    projects = load_projects()
    if (project := find_project(projects, name)) is None:
        raise click.ClickException(f'Projeto "{name}" não encontrado (veja `hefesto projeto listar`).')
    return projects, project


@main.group("projeto")
def projeto() -> None:
    """Projetos: pastas e arquivos ligados, com descrição e servidores MCP.

    Uma sessão aberta numa pasta de projeto recebe a descrição e a lista de pastas e
    arquivos no primeiro pedido, e os servidores MCP do projeto.
    """


@projeto.command("novo")
@click.argument("nome")
@click.argument("caminhos", nargs=-1, type=click.Path(exists=True))
@click.option("-d", "--descricao", default="", help="Descrição do projeto.")
@click.option("-a", "--agente", default="", help="Agente padrão (claude, codex, gemini).")
def projeto_novo(nome: str, caminhos: tuple[str, ...], descricao: str, agente: str) -> None:
    """Cria um projeto com pastas e arquivos (sem caminhos: a pasta atual)."""
    from pathlib import Path

    from toad.iris_projects import ProjectError, load_projects, new_project, save_projects

    projects = load_projects()
    try:
        project = new_project(
            projects, nome, descricao, [Path(path) for path in caminhos or (".",)]
        )
    except ProjectError as error:
        raise click.ClickException(str(error))
    project.agente = agente
    save_projects(projects)
    click.echo(f'Projeto "{project.nome}" criado.')


@projeto.command("listar")
def projeto_listar() -> None:
    """Lista os projetos."""
    from toad.iris_projects import load_projects, projects_path

    projects = load_projects()
    if not projects:
        click.echo(f"Nenhum projeto ainda ({projects_path()}).")
    for project in projects:
        click.echo(
            f"{project.nome}: {project.descricao or '(sem descrição)'} "
            f"[{len(project.pastas)} pasta(s), {len(project.arquivos)} arquivo(s), "
            f"{len(project.mcp)} MCP]"
        )


@projeto.command("mostrar")
@click.argument("nome")
def projeto_mostrar(nome: str) -> None:
    """Mostra um projeto."""
    click.echo(_get_project(nome).summary_markdown())


@projeto.command("adicionar")
@click.argument("nome")
@click.argument("caminhos", nargs=-1, required=True, type=click.Path(exists=True))
def projeto_adicionar(nome: str, caminhos: tuple[str, ...]) -> None:
    """Liga pastas ou arquivos a um projeto."""
    from pathlib import Path

    from toad.iris_projects import save_projects

    projects, project = _edit_project(nome)
    for path in caminhos:
        kind = project.add_path(Path(path))
        click.echo(f"{kind}: {Path(path).resolve()}")
    save_projects(projects)


@projeto.command("remover")
@click.argument("nome")
@click.argument("caminhos", nargs=-1)
def projeto_remover(nome: str, caminhos: tuple[str, ...]) -> None:
    """Desliga pastas ou arquivos (sem caminhos: apaga o projeto)."""
    from pathlib import Path

    from toad.iris_projects import save_projects

    projects, project = _edit_project(nome)
    if not caminhos:
        click.confirm(f'Apagar o projeto "{project.nome}"?', abort=True)
        projects.remove(project)
    for path in caminhos:
        if not project.remove_path(Path(path)):
            click.echo(f"Não faz parte do projeto: {path}")
    save_projects(projects)


@projeto.command("descricao")
@click.argument("nome")
@click.argument("texto", nargs=-1, required=True)
def projeto_descricao(nome: str, texto: tuple[str, ...]) -> None:
    """Troca a descrição de um projeto."""
    from toad.iris_projects import save_projects

    projects, project = _edit_project(nome)
    project.descricao = " ".join(texto)
    save_projects(projects)


@projeto.command("mcp", context_settings={"ignore_unknown_options": True})
@click.argument("nome")
@click.argument("servidor")
@click.argument("comando", nargs=-1, type=click.UNPROCESSED)
@click.option("-e", "--env", multiple=True, metavar="NOME=valor", help="Variável de ambiente.")
@click.option("--remover", is_flag=True, help="Remove o servidor do projeto.")
def projeto_mcp(
    nome: str, servidor: str, comando: tuple[str, ...], env: tuple[str, ...], remover: bool
) -> None:
    """Liga um servidor MCP ao projeto: um comando (stdio) ou uma URL (http).

    \b
    hefesto projeto mcp Celula abb-robot -- C:\\ferramentas\\abbctl\\abbctl-mcp.exe
    hefesto projeto mcp Celula abb-rws -- abbrwsctl-mcp
    """
    from toad.iris_projects import ProjectError, parse_mcp_command, save_projects

    projects, project = _edit_project(nome)
    project.mcp = [server for server in project.mcp if server.get("name") != servidor]
    if not remover:
        try:
            project.mcp.append(parse_mcp_command(servidor, list(comando), env))
        except ProjectError as error:
            raise click.ClickException(str(error))
    save_projects(projects)
    click.echo(f'Servidor "{servidor}" {"removido" if remover else "ligado"}.')


@projeto.command("abrir")
@click.argument("nome")
@click.option("-a", "--agente", default="", help="Agente (padrão: o do projeto).")
@click.pass_context
def projeto_abrir(ctx, nome: str, agente: str) -> None:
    """Abre o Hefesto na pasta principal do projeto."""
    project = _get_project(nome)
    ctx.invoke(run, projeto=project.nome, agent=agente or project.agente)


@main.command("settings")
def settings() -> None:
    """Settings information."""
    app = ToadApp()
    print(f"{app.settings_path}")


@main.command("replay")
@click.argument("path", metavar="FILE")
def replay(path: str) -> None:
    """Replay interaction from a log file.

    This is a debugging aid. You probably won't need it unless you are building an agent.

    Run it in place of a command line to run an ACP agent:

    toad acp "toad replay toad.log"

    This will replay the agents output, and Toad will update the conversation as it would a real agent.
    """
    import time

    stdout = sys.stdout.buffer
    with open(path, "rb") as replay_file:
        for line in replay_file.readlines():
            sender, space, json_line = line.partition(b" ")
            if sender == b"[agent]":
                stdout.write(json_line.strip() + b"\n")
            time.sleep(0.01)
            stdout.write(line)
            stdout.flush()


@main.command("serve")
@click.option("-p", "--port", metavar="PORT", default=8000, type=int)
@click.option("-H", "--host", metavar="HOST", default="localhost")
@click.option(
    "--public-url",
    metavar="URL",
    default=None,
    help="Public URL for textual_serve Server (e.g. https://example.com)",
)
def serve(port: int, host: str, public_url: str | None = None) -> None:
    """Serve Toad as a web application."""
    from textual_serve.server import Server

    server = Server(
        sys.argv[0], host=host, port=port, title=toad.TITLE, public_url=public_url
    )
    set_process_title("toad serve")
    server.serve()


@main.command("about")
def about() -> None:
    """Show about information."""

    from toad import about

    app = ToadApp()

    print(about.render(app))


if __name__ == "__main__":
    main()
