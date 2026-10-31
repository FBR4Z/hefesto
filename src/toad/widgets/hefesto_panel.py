"""Hefesto's "Robôs" panel: every registered controller, its light and its state."""

from __future__ import annotations

from rich.style import Style
from rich.text import Text

from textual.app import RenderResult
from textual.widget import Widget

from toad.widgets.hefesto_robots import ANDON_COLORS, RobotStatus


def light(status: RobotStatus | None) -> str:
    """Stack light color for one controller (the panel has no AI phase)."""
    if status is None or not status.connected:
        return "#6a6a78"
    if status.stopped_hard or any(severity == "error" for _, severity in status.events[-3:]):
        return ANDON_COLORS["vermelho"].hex
    if "manual" in status.opmode.lower() or "off" in status.motors.lower():
        return ANDON_COLORS["amarelo"].hex
    return ANDON_COLORS["verde"].hex


class RobotPanel(Widget):
    """One block per controller: light, name, REAL/VIRTUAL, mode, program, speed."""

    DEFAULT_CSS = """
    RobotPanel {
        height: auto;
        background: transparent;
    }
    """

    def on_mount(self) -> None:
        self.set_interval(1.0, self.refresh_panel)

    def refresh_panel(self) -> None:
        self.refresh(layout=True)

    def render(self) -> RenderResult:
        registry = getattr(self.app, "hefesto_registry", None)
        monitor = getattr(self.app, "hefesto_monitor", None)
        text = Text(no_wrap=True, overflow="ellipsis")
        if registry is None or not registry.controladores:
            text.append("Nenhum controlador cadastrado.\n", Style(dim=True))
            text.append("hefesto robo procurar", Style(bold=True))
            return text
        statuses = monitor.statuses if monitor is not None else {}
        focus = monitor.focus if monitor is not None else None
        for index, controller in enumerate(registry.controladores):
            status = statuses.get(controller.apelido)
            if index:
                text.append("\n")
            text.append("▸ " if controller.apelido == focus else "  ")
            text.append("● ", Style(color=light(status)))
            text.append(controller.apelido, Style(bold=True))
            tag_color = ANDON_COLORS["vermelho"].hex if controller.real else "#8aa0b8"
            text.append(f" {controller.kind_label}", Style(color=tag_color, bold=controller.real))
            if controller.real and not controller.writable:
                text.append(" 🔒")
            text.append("\n    ")
            if status is None or not status.connected:
                text.append(status.error if status and status.error else "conectando…", Style(dim=True))
                continue
            details = [status.opmode, status.motors,
                       "rodando" if status.running else "parado"]
            if status.speed is not None:
                details.append(f"{status.speed}%")
            text.append(" · ".join(item for item in details if item), Style(dim=True))
            if status.pp:
                text.append(f"\n    ↳ {status.pp}", Style(dim=True))
        return text
