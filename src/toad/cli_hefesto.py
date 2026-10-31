"""`hefesto robo ...` and `hefesto projeto controladores`: the robot controller registry."""

from __future__ import annotations

import json
import subprocess

import click


def _registry():
    from toad.hefesto_robot import load_registry

    return load_registry()


def _fail(error: Exception) -> None:
    raise click.ClickException(str(error))


def register(main: click.Group, projeto: click.Group) -> None:
    @main.group("robo")
    def robo() -> None:
        """Controladores ABB: cadastro (fica só nesta máquina), ferramentas e teste.

        \b
        hefesto robo procurar
        hefesto robo adicionar celula3-solda 192.168.125.1 --real
        hefesto robo adicionar bancada 127.0.0.1 --virtual
        hefesto robo adicionar omni-teste 192.168.125.1 --tipo rws2 --real
        hefesto robo escrita celula3-solda liberar
        hefesto projeto controladores Celula3 celula3-solda
        """

    @robo.command("listar")
    def robo_listar() -> None:
        """Lista os controladores cadastrados."""
        from toad.hefesto_robot import TYPES, find_tool, registry_path

        registry = _registry()
        if not registry.controladores:
            click.echo("Nenhum controlador. Use `hefesto robo procurar` e `hefesto robo adicionar`.")
        for c in registry.controladores:
            lock = "" if c.writable else "  [somente leitura]"
            click.echo(f"{c.apelido:<20} {c.kind_label:<8} {TYPES[c.tipo]:<20} {c.endereco}{lock}")
            if c.descricao:
                click.echo(f"{'':<20} {c.descricao}")
        click.echo(f"\nCadastro: {registry_path()}")
        click.echo(f"abbctl:     {find_tool(registry.abbctl, 'abbctl') or '(não encontrado)'}")
        click.echo(f"abbctl-mcp: {find_tool(registry.abbctl_mcp, 'abbctl-mcp') or '(não encontrado)'}")

    @robo.command("procurar")
    def robo_procurar() -> None:
        """Procura controladores IRC5 na rede (reais e virtuais do RobotStudio)."""
        from toad.hefesto_robot import find_tool

        registry = _registry()
        abbctl = find_tool(registry.abbctl, "abbctl")
        if abbctl is None:
            raise click.ClickException("abbctl não encontrado (veja `hefesto robo ferramentas`).")
        result = subprocess.run([abbctl, "scan", "--json"], capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            raise click.ClickException(result.stderr.strip() or "abbctl scan falhou")
        found = json.loads(result.stdout or "[]")
        if not found:
            click.echo("Nenhum controlador encontrado. Virtuais só aparecem com a estação aberta no RobotStudio.")
            return
        known = {c.endereco for c in registry.controladores}
        for item in found:
            kind = "virtual" if item.get("virtual") else "REAL"
            mark = "  (já cadastrado)" if item.get("ip") in known else ""
            click.echo(f"{item.get('systemName')}  {item.get('ip')}  RobotWare {item.get('version')}  {kind}{mark}")
            if not mark:
                flag = "--virtual" if item.get("virtual") else "--real"
                click.echo(f"    hefesto robo adicionar <apelido> {item.get('ip')} {flag}")
        click.echo("\nO cadastro pede --real ou --virtual de propósito: confira antes de escolher.")

    @robo.command("adicionar")
    @click.argument("apelido")
    @click.argument("endereco")
    @click.option("--tipo", type=click.Choice(["pcsdk", "rws", "rws2"]), default="pcsdk",
                  help="pcsdk (padrão): IRC5 com PC Interface 616-1 ou OmniCore com RobotStudio Connect 3119-1; rws: IRC5 por RWS; rws2: OmniCore por RWS 2.0.")
    @click.option("--real/--virtual", "real", default=None,
                  help="Obrigatório: é um robô de verdade ou uma estação do RobotStudio?")
    @click.option("-d", "--descricao", default="", help="Ex.: IRB 6700, solda ponto, célula 3.")
    def robo_adicionar(apelido: str, endereco: str, tipo: str, real: bool | None, descricao: str) -> None:
        """Cadastra um controlador (reais começam somente leitura)."""
        from toad.hefesto_robot import Controller, RobotError, save_registry

        if real is None:
            raise click.UsageError("Diga se é --real ou --virtual.")
        registry = _registry()
        try:
            registry.add(Controller(apelido, endereco, tipo, real, False, descricao))
        except RobotError as error:
            _fail(error)
        save_registry(registry)
        extra = " Somente leitura; libere com `hefesto robo escrita " + apelido + " liberar`." if real else ""
        click.echo(f'Controlador "{apelido}" cadastrado.{extra}')

    @robo.command("remover")
    @click.argument("apelido")
    def robo_remover(apelido: str) -> None:
        """Remove um controlador do cadastro."""
        from toad.hefesto_robot import RobotError, save_registry

        registry = _registry()
        try:
            registry.controladores.remove(registry.find(apelido))
        except RobotError as error:
            _fail(error)
        save_registry(registry)
        click.echo(f'Controlador "{apelido}" removido.')

    @robo.command("escrita")
    @click.argument("apelido")
    @click.argument("acao", type=click.Choice(["liberar", "bloquear"]))
    def robo_escrita(apelido: str, acao: str) -> None:
        """Libera ou bloqueia a escrita da IA num controlador REAL."""
        from toad.hefesto_robot import RobotError, save_registry

        registry = _registry()
        try:
            controller = registry.find(apelido)
        except RobotError as error:
            _fail(error)
        if not controller.real:
            click.echo("Controlador virtual: a escrita já é livre.")
            return
        if acao == "liberar":
            answer = click.prompt(
                f'Liberar a escrita no robô REAL "{apelido}"? A IA continua pedindo '
                "confirmação a cada ação. [s/N]",
                default="n",
                show_default=False,
            )
            if answer.strip().lower() not in ("s", "sim", "y", "yes"):
                raise click.Abort()
        controller.escrita = acao == "liberar"
        save_registry(registry)
        click.echo(f'Escrita {"liberada" if controller.escrita else "bloqueada"} em "{apelido}".')

    def _edit(apelido: str):
        from toad.hefesto_robot import RobotError

        registry = _registry()
        try:
            return registry, registry.find(apelido)
        except RobotError as error:
            _fail(error)

    @robo.command("gemeo")
    @click.argument("apelido")
    @click.argument("gemeo", required=False, default="")
    def robo_gemeo(apelido: str, gemeo: str) -> None:
        """Liga um controlador ao seu gêmeo virtual (sem GEMEO: desliga).

        O gêmeo é um controlador VIRTUAL do RobotStudio restaurado do backup do
        real: o /rapid aplicar carrega lá primeiro."""
        from toad.hefesto_robot import RobotError, save_registry

        registry, controller = _edit(apelido)
        if gemeo:
            try:
                twin = registry.find(gemeo)
            except RobotError as error:
                _fail(error)
            if twin.real:
                raise click.ClickException(f'"{twin.apelido}" está cadastrado como REAL: o gêmeo precisa ser virtual.')
            if twin.apelido == controller.apelido:
                raise click.ClickException("Um controlador não pode ser gêmeo de si mesmo.")
            gemeo = twin.apelido
        controller.gemeo = gemeo
        save_registry(registry)
        click.echo(f'{controller.apelido}: gêmeo {gemeo or "desligado"}.')

    @robo.command("sinais")
    @click.argument("apelido")
    @click.argument("sinais", nargs=-1)
    def robo_sinais(apelido: str, sinais: tuple[str, ...]) -> None:
        """Sinais acompanhados ao vivo (sem SINAIS: nenhum)."""
        from toad.hefesto_robot import save_registry

        registry, controller = _edit(apelido)
        controller.sinais = list(sinais)
        save_registry(registry)
        click.echo(f'{controller.apelido}: sinais {", ".join(sinais) or "nenhum"} (reabra o Hefesto).')

    @robo.command("ciclo")
    @click.argument("apelido")
    @click.argument("sinal", required=False, default="")
    def robo_ciclo(apelido: str, sinal: str) -> None:
        """Sinal cuja subida marca o início de cada ciclo (sem SINAL: início→fim do programa)."""
        from toad.hefesto_robot import save_registry

        registry, controller = _edit(apelido)
        controller.ciclo = sinal
        save_registry(registry)
        click.echo(f'{controller.apelido}: ciclo por {sinal or "início → fim do programa"} (reabra o Hefesto).')

    @robo.command("ferramentas")
    @click.option("--abbctl", default=None, help="Caminho do abbctl.exe.")
    @click.option("--abbctl-mcp", "abbctl_mcp", default=None, help="Caminho do abbctl-mcp.exe.")
    def robo_ferramentas(abbctl: str | None, abbctl_mcp: str | None) -> None:
        """Mostra ou define onde estão o abbctl e o abbctl-mcp."""
        from toad.hefesto_robot import find_tool, save_registry

        registry = _registry()
        if abbctl is not None:
            registry.abbctl = abbctl
        if abbctl_mcp is not None:
            registry.abbctl_mcp = abbctl_mcp
        if abbctl is not None or abbctl_mcp is not None:
            save_registry(registry)
        click.echo(f"abbctl:     {find_tool(registry.abbctl, 'abbctl') or '(não encontrado)'}")
        click.echo(f"abbctl-mcp: {find_tool(registry.abbctl_mcp, 'abbctl-mcp') or '(não encontrado)'}")

    @robo.command("testar")
    @click.argument("apelido")
    @click.option("-s", "--segundos", default=5.0, help="Quanto tempo ouvir o monitor.")
    def robo_testar(apelido: str, segundos: float) -> None:
        """Lê o controlador (só leitura) e mostra o que a IA receberia."""
        from toad.hefesto_robot import RobotError, apply_event, describe, monitor_command
        from toad.widgets.hefesto_robots import RobotStatus

        registry = _registry()
        try:
            controller = registry.find(apelido)
        except RobotError as error:
            _fail(error)
        command = monitor_command(registry, controller)
        if command is None:
            raise click.ClickException("Ferramenta do monitor não encontrada para este tipo.")
        status = RobotStatus()
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   encoding="utf-8", errors="replace")
        try:
            process.wait(timeout=segundos)
        except subprocess.TimeoutExpired:
            process.kill()
        out, err = process.communicate()
        for line in out.splitlines():
            try:
                apply_event(status, json.loads(line))
            except ValueError:
                continue
        if not status.connected and err.strip():
            status.error = err.strip().splitlines()[-1]
        click.echo(describe(controller, status))

    @projeto.command("controladores")
    @click.argument("nome")
    @click.argument("apelidos", nargs=-1)
    def projeto_controladores(nome: str, apelidos: tuple[str, ...]) -> None:
        """Define os controladores da célula do projeto (sem apelidos: todos)."""
        from toad.hefesto_robot import RobotError
        from toad.iris_projects import find_project, load_projects, save_projects

        registry = _registry()
        try:
            names = [registry.find(apelido).apelido for apelido in apelidos]
        except RobotError as error:
            _fail(error)
        projects = load_projects()
        project = find_project(projects, nome)
        if project is None:
            raise click.ClickException(f'Projeto "{nome}" não encontrado (veja `hefesto projeto listar`).')
        project.controladores = names
        save_projects(projects)
        click.echo(f'Projeto "{project.nome}": ' + (", ".join(names) or "todos os controladores"))
