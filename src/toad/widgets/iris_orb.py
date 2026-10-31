"""The Íris orb: an animated braille ring that reflects the agent's mode and activity."""

from __future__ import annotations

import math
from dataclasses import dataclass
from time import monotonic
from typing import TYPE_CHECKING

from rich.style import Style
from rich.text import Text

from textual.app import RenderResult
from textual.color import Color
from textual.widget import Widget

from toad.widgets.iris_eyes import EYES, EyeContext, EyeTheme
from toad.widgets.hefesto_robots import (
    DEFAULT_ROBOT_THEME,
    ROBOTS,
    Canvas,
    RobotContext,
    RobotTheme,
    phase_of,
)

if TYPE_CHECKING:
    from toad.widgets.conversation import Conversation

TAU = math.tau

# Bit for the dot at (x, y) inside a 2x4 braille cell, indexed as DOT_BITS[y][x].
DOT_BITS = ((0x01, 0x08), (0x02, 0x10), (0x04, 0x20), (0x40, 0x80))

DEFAULT_PLAN_COLOR = "#29c7ff"
DEFAULT_EXEC_COLOR = "#ff8a1f"
DEFAULT_ATTENTION_COLOR = "#ffd23f"
DEFAULT_ERROR_COLOR = "#ff4d5e"
DEFAULT_LISTEN_COLOR = "#bff3ff"
DEFAULT_PLAN_MODES = "plan, read-only"
DEFAULT_AGENT_COLORS = "claude=#d97757, codex=#10a37f, openai=#10a37f, gemini=#7b8cff"
DEFAULT_ELEVATED_MODES = "bypass, yolo"

THEME_ARC = "arco"
THEME_SHARINGAN = "sharingan"

ELEVATED_TOOLS = {"skill", "agent", "task", "activate_skill", "delegate_to_agent"}
"""Tools that mean a skill or a sub-agent is running (Claude Code, Gemini CLI)."""

BOOT_SECONDS = 1.4
RIPPLE_SECONDS = 0.9
DONE_LABEL_SECONDS = 2.5

WHITE = Color(255, 255, 255)


@dataclass(frozen=True)
class OrbState:
    connected: bool = False
    busy: bool = False
    planning: bool = True
    asking: bool = False
    failed: bool = False
    turns: int = 0
    """Completed agent turns, used to spot a finished turn even if it was too quick to see."""
    mode_name: str = ""
    agent_key: str = ""
    """Lowercased identity + name of the agent, used to pick its color."""
    listening: bool = False
    transcribing: bool = False
    speaking: bool = False
    watching: bool = False
    """Waiting for the wake word."""
    level: float = 0.0
    """Microphone or speech loudness, 0..1."""
    elevated: bool = False
    """A skill or sub-agent is running, or the mode skips permissions."""
    model_name: str = ""


def is_elevated_tool(tool_call: dict) -> bool:
    """Is this tool call a skill or a sub-agent?"""
    meta = tool_call.get("_meta") or {}
    names = [
        (meta.get("claudeCode") or {}).get("toolName") if isinstance(meta, dict) else None,
        tool_call.get("name"),
    ]
    return any(
        isinstance(name, str) and name.lower() in ELEVATED_TOOLS for name in names
    )


def ease_out(value: float) -> float:
    value = min(max(value, 0.0), 1.0)
    return 1 - (1 - value) ** 3


class IrisOrb(Widget):
    """Arc-reactor style ring.

    - Outer ring and borders follow the session mode (planning vs execution).
    - Inner ring takes the agent's own color.
    - Breathes slowly while idle, spins fast while the agent works.
    - Pulses amber while waiting for a permission, red when the agent fails.
    - Sends out a ripple when a turn finishes, and "powers up" when mounted.
    """

    DEFAULT_CSS = """
    IrisOrb {
        width: 1fr;
        height: 12;
        background: transparent;
    }
    """

    FPS = 24
    LABEL_LINES = 3
    """Phase and status, mode name, subscription usage."""

    def __init__(
        self,
        show_label: bool = True,
        accent_borders: bool = True,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self.show_label = show_label
        self.accent_borders = accent_borders
        """Tint the prompt and sidebar borders with the mode color."""
        self._start = monotonic()
        self._last_frame = self._start
        self._elapsed = 0.0
        # Integrated rotation angle, so speed changes don't make the ring jump.
        self._angle = 0.0
        self._color = Color.parse(DEFAULT_PLAN_COLOR)
        self._agent_color = Color.parse(DEFAULT_PLAN_COLOR)
        self._activity = 0.0
        """0 = idle, 1 = busy; eased so transitions are smooth."""
        self._intensity = 1.0
        self._state = OrbState()
        self._ripple_start: float | None = None
        self._turn_done_at: float | None = None
        self._voice_level = 0.0
        self._elevation = 0.0
        """0 = normal, 1 = skill / sub-agent level; eased."""
        self._applied_accent: tuple[str, bool] | None = None
        self._booted = False
        """Set on the first tick, so the power-up plays once the app is responsive."""

    def on_mount(self) -> None:
        # Tick even when hidden (e.g. sidebar closed) so the accent borders keep up.
        self.set_interval(1 / self.FPS, self._tick)

    def _setting(self, key: str, default: str) -> str:
        try:
            value = self.app.settings.get(key, str)
        except Exception:
            return default
        return value or default

    def _parse_color(self, key: str, default: str) -> Color:
        try:
            return Color.parse(self._setting(key, default))
        except Exception:
            return Color.parse(default)

    @property
    def theme(self) -> str:
        """Look of the orb: "arco" (arc reactor) or one of the eyes (e.g. "sharingan")."""
        return self._setting("iris.orb_theme", DEFAULT_ROBOT_THEME).strip().lower()

    @property
    def eye(self) -> EyeTheme | None:
        """The eye theme, or None for the arc reactor."""
        return EYES.get(self.theme)

    @property
    def robot_theme(self) -> RobotTheme | None:
        """Hefesto's robotic theme, or None for the ring and the eyes."""
        return ROBOTS.get(self.theme)

    def _conversation(self) -> Conversation | None:
        from toad.widgets.conversation import Conversation

        return self.screen.query_one_optional(Conversation)

    def _read_state(self) -> OrbState:
        voice = getattr(self.app, "iris_voice", None)
        voice_state = {}
        if voice is not None:
            voice_state = {
                "listening": voice.listening,
                "transcribing": voice.transcribing,
                "speaking": voice.speaking,
                "watching": voice.watching,
                "level": voice.level,
            }
        conversation = self._conversation()
        if conversation is None:
            # Outside a conversation (e.g. the launcher): just idle.
            return OrbState(connected=True, **voice_state)
        mode = conversation.current_mode
        plan_words = [
            word.strip().lower()
            for word in self._setting("iris.plan_modes", DEFAULT_PLAN_MODES).split(",")
            if word.strip()
        ]
        mode_id = f"{mode.id} {mode.name}".lower() if mode is not None else ""
        elevated_words = [
            word.strip().lower()
            for word in self._setting(
                "iris.elevated_modes", DEFAULT_ELEVATED_MODES
            ).split(",")
            if word.strip()
        ]
        from toad.iris_config import MODEL, by_category

        model = by_category(list(getattr(conversation, "config_options", {}).values()), MODEL)
        session_state = None
        if self.screen.id is not None:
            session = self.app.session_tracker.get_session(self.screen.id)
            session_state = session.state if session is not None else None
        agent_data = conversation._agent_data or {}
        agent_key = f"{agent_data.get('identity', '')} {agent_data.get('name', '')}"
        return OrbState(
            connected=bool(conversation.agent_ready),
            busy=conversation.busy_count > 0,
            planning=any(word in mode_id for word in plan_words),
            asking=session_state == "asking",
            failed=bool(getattr(conversation, "_agent_fail", False)),
            turns=getattr(conversation, "_turn_count", 0),
            mode_name=mode.name if mode is not None else "",
            agent_key=agent_key.lower(),
            elevated=bool(getattr(conversation, "iris_elevated", False))
            or any(word in mode_id for word in elevated_words),
            model_name=model.current_name if model is not None else "",
            **voice_state,
        )

    def _agent_target(self, state: OrbState, fallback: Color) -> Color:
        agent_colors = self._setting("iris.agent_colors", DEFAULT_AGENT_COLORS)
        for entry in agent_colors.split(","):
            keyword, _, color = entry.partition("=")
            keyword = keyword.strip().lower()
            if keyword and keyword in state.agent_key:
                try:
                    return Color.parse(color.strip())
                except Exception:
                    break
        return fallback

    def _tick(self) -> None:
        """Advance the animation state."""
        now = monotonic()
        if not self._booted:
            self._booted = True
            self._start = self._last_frame = now
        elapsed = self._elapsed = now - self._start
        delta = min(now - self._last_frame, 0.25)
        self._last_frame = now

        previous = self._state
        state = self._state = self._read_state()
        if state.turns > previous.turns and not state.failed:
            self._ripple_start = self._turn_done_at = elapsed
        if state.elevated and not previous.elevated:
            # Going up a level: a pulse out of the core.
            self._ripple_start = elapsed
        eye = self.eye

        if state.listening:
            target = Color.parse(DEFAULT_LISTEN_COLOR)
        elif state.failed:
            target = self._parse_color("iris.error_color", DEFAULT_ERROR_COLOR)
        elif state.asking:
            target = self._parse_color("iris.attention_color", DEFAULT_ATTENTION_COLOR)
        elif eye is not None:
            if state.elevated:
                target = Color.parse(eye.elevated_color)
            elif state.planning:
                target = Color.parse(eye.plan_color)
            else:
                target = Color.parse(eye.exec_color)
        elif state.planning:
            target = self._parse_color("iris.plan_color", DEFAULT_PLAN_COLOR)
        else:
            target = self._parse_color("iris.exec_color", DEFAULT_EXEC_COLOR)
        ease = 1 - math.exp(-delta * 6)
        self._color = self._color.blend(target, ease)
        self._agent_color = self._agent_color.blend(
            self._agent_target(state, target), ease
        )
        self._activity += ((1.0 if state.busy else 0.0) - self._activity) * ease
        self._elevation += ((1.0 if state.elevated else 0.0) - self._elevation) * ease

        speed = 0.35 + self._activity * 3.2
        if not state.connected:
            speed = 1.0
        if state.asking:
            speed = 0.15
        self._angle = (self._angle + speed * delta) % TAU

        breath = 0.5 + 0.5 * math.sin(elapsed * 1.7)
        intensity = 0.62 + 0.38 * breath
        intensity += (1.0 - intensity) * self._activity
        if not state.connected:
            intensity = 0.35 + 0.15 * breath
        if state.asking or state.failed:
            # Faster, deeper pulse to grab attention.
            intensity = 0.45 + 0.55 * (0.5 + 0.5 * math.sin(elapsed * 6))
        if state.listening or state.speaking:
            intensity = max(intensity, 0.55 + 0.45 * state.level)
        # Smooth the voice level so the ring "breathes" with the sound.
        self._voice_level += (state.level - self._voice_level) * min(delta * 18, 1.0)
        self._intensity = intensity

        self._apply_accent()
        if self.display:
            self.refresh()

    def _apply_accent(self) -> None:
        if not self.accent_borders or self._conversation() is None:
            return
        screen = self.screen
        prompt_container = screen.query_one_optional("PromptContainer")
        focused = prompt_container is not None and prompt_container.has_focus_within
        key = (self._color.hex, focused)
        if key == self._applied_accent:
            return
        self._applied_accent = key
        background = screen.styles.background
        if prompt_container is not None:
            prompt_color = (
                self._color if focused else background.blend(self._color, 0.35)
            )
            prompt_container.styles.border = ("tall", prompt_color)
        if (side_bar := screen.query_one_optional("SideBar")) is not None:
            side_bar.styles.border_right = (
                "tall",
                background.blend(self._color, 0.45),
            )

    def render(self) -> RenderResult:
        width = self.size.width
        label_lines = self.LABEL_LINES if self.show_label else 0
        rows = max(self.size.height - label_lines, 1)
        if (robot_theme := self.robot_theme) is not None:
            text = self._render_robot(width, rows, robot_theme)
        elif (eye := self.eye) is not None:
            text = self._render_eye(width, rows, eye)
        else:
            text = self._render_ring(width, rows)
        if self.show_label:
            text.append(self._render_label(width))
        else:
            text.rstrip()
        return text

    def _render_ring(self, width: int, rows: int) -> Text:
        elapsed = self._elapsed
        boot = ease_out(elapsed / BOOT_SECONDS)
        dots_w, dots_h = width * 2, rows * 4
        bits = [[0] * width for _ in range(rows)]
        glow = [[0.0] * width for _ in range(rows)]
        colors: list[list[Color]] = [[self._color] * width for _ in range(rows)]

        def plot(x: float, y: float, brightness: float, color: Color) -> None:
            ix, iy = int(x), int(y)
            if 0 <= ix < dots_w and 0 <= iy < dots_h:
                cx, cy = ix // 2, iy // 4
                bits[cy][cx] |= DOT_BITS[iy % 4][ix % 2]
                if brightness > glow[cy][cx]:
                    glow[cy][cx] = brightness
                    colors[cy][cx] = color

        center_x, center_y = dots_w / 2, dots_h / 2
        radius = min(dots_w, dots_h) / 2 - 1
        head = self._angle
        voice_level = self._voice_level
        if self._state.listening:
            # The ring swells with your voice.
            radius *= 0.9 + 0.1 * voice_level

        # Outer ring with a bright comet; while booting it sweeps in from the top.
        samples = int(TAU * radius * 2)
        sweep = TAU * boot
        for index in range(samples):
            theta = TAU * index / samples
            if (theta + math.pi / 2) % TAU > sweep:
                continue
            tail = (head - theta) % TAU
            if self._elevation > 0.5:
                # Skill / sub-agent: three comets instead of one.
                tail %= TAU / 3
            brightness = 0.25 + 0.75 * max(0.0, 1 - tail / (TAU * 0.45))
            plot(
                center_x + radius * math.cos(theta),
                center_y + radius * math.sin(theta),
                brightness,
                self._color,
            )

        # Dashed inner ring in the agent's color, counter-rotating.
        inner_fade = ease_out((elapsed - BOOT_SECONDS * 0.4) / (BOOT_SECONDS * 0.6))
        if inner_fade > 0:
            inner = radius * 0.72
            samples = int(TAU * inner * 2)
            for index in range(samples):
                theta = TAU * index / samples
                if math.sin(6 * (theta + head * 0.6)) > 0.15:
                    plot(
                        center_x + inner * math.cos(theta),
                        center_y + inner * math.sin(theta),
                        0.6 * inner_fade,
                        self._agent_color,
                    )

        # Pulsing core, growing in as the orb boots.
        core = radius * (0.24 + 0.04 * math.sin(elapsed * 3.4)) * boot
        if self._state.speaking:
            # The core pulses with Íris' own voice.
            core *= 1 + 0.7 * voice_level
        core_int = int(core) + 1
        for dy in range(-core_int, core_int + 1):
            for dx in range(-core_int, core_int + 1):
                if dx * dx + dy * dy <= core * core:
                    plot(center_x + dx, center_y + dy, 1.0, self._color)

        # Ripple expanding out of the core when a turn completes.
        if self._ripple_start is not None:
            progress = (elapsed - self._ripple_start) / RIPPLE_SECONDS
            if progress >= 1:
                self._ripple_start = None
            else:
                ripple = radius * (0.3 + 0.8 * ease_out(progress))
                samples = int(TAU * ripple * 2)
                for index in range(samples):
                    theta = TAU * index / samples
                    plot(
                        center_x + ripple * math.cos(theta),
                        center_y + ripple * math.sin(theta),
                        1.1 * (1 - progress),
                        self._color,
                    )

        return self._to_text(bits, glow, colors)

    def _to_text(
        self, bits: list[list[int]], glow: list[list[float]], colors: list[list[Color]]
    ) -> Text:
        """Braille canvas → colored text."""
        intensity = self._intensity
        background = self.background_colors[1]
        text = Text(no_wrap=True, overflow="crop")
        for bits_row, glow_row, colors_row in zip(bits, glow, colors):
            for cell_bits, cell_glow, cell_color in zip(bits_row, glow_row, colors_row):
                if not cell_bits:
                    text.append(" ")
                    continue
                level = cell_glow * intensity
                color = background.blend(cell_color, min(level, 1.0))
                if level > 0.85:
                    color = color.blend(WHITE, min((level - 0.85) * 2, 0.5))
                text.append(chr(0x2800 + cell_bits), Style(color=color.rich_color))
            text.append("\n")
        return text

    def _render_eye(self, width: int, rows: int, eye: EyeTheme) -> Text:
        """Eye themes: each dot of the disk is lit (or left dark) by the eye's field."""
        elapsed = self._elapsed
        boot = ease_out(elapsed / BOOT_SECONDS)
        dots_w, dots_h = width * 2, rows * 4
        bits = [[0] * width for _ in range(rows)]
        glow = [[0.0] * width for _ in range(rows)]
        colors: list[list[Color]] = [[self._color] * width for _ in range(rows)]
        center_x, center_y = dots_w / 2, dots_h / 2
        radius = min(dots_w, dots_h) / 2 - 1
        if self._state.listening:
            radius *= 0.9 + 0.1 * self._voice_level
        if radius <= 1:
            return self._to_text(bits, glow, colors)

        context = EyeContext(
            color=self._color,
            rotation=self._angle,
            elapsed=elapsed,
            planning=self._state.planning,
            elevation=self._elevation,
            activity=self._activity,
            voice=self._voice_level,
            speaking=self._state.speaking,
        )
        field = eye.field
        limit = radius * boot
        for iy in range(dots_h):
            dy = iy + 0.5 - center_y
            if abs(dy) > limit:
                continue
            for ix in range(dots_w):
                dx = ix + 0.5 - center_x
                distance = math.hypot(dx, dy)
                if distance > limit:
                    continue
                dot = field(context, distance / radius, math.atan2(dy, dx))
                if dot is None:
                    continue
                brightness, color = dot
                cx, cy = ix // 2, iy // 4
                bits[cy][cx] |= DOT_BITS[iy % 4][ix % 2]
                if brightness > glow[cy][cx]:
                    glow[cy][cx] = brightness
                    colors[cy][cx] = self._color if color is None else color

        if self._ripple_start is not None:
            progress = (elapsed - self._ripple_start) / RIPPLE_SECONDS
            if progress >= 1:
                self._ripple_start = None
            else:
                ripple = radius * (1.02 + 0.25 * ease_out(progress))
                samples = int(TAU * ripple * 2)
                for index in range(samples):
                    theta = TAU * index / samples
                    ix = int(center_x + ripple * math.cos(theta))
                    iy = int(center_y + ripple * math.sin(theta))
                    if 0 <= ix < dots_w and 0 <= iy < dots_h:
                        cx, cy = ix // 2, iy // 4
                        bits[cy][cx] |= DOT_BITS[iy % 4][ix % 2]
                        glow[cy][cx] = max(glow[cy][cx], 1.1 * (1 - progress))
        return self._to_text(bits, glow, colors)

    def _render_robot(self, width: int, rows: int, theme: RobotTheme) -> Text:
        """Robotic themes: drawn by `hefesto_robots`, scaled to the widget."""
        dots_w, dots_h = width * 2, rows * 4
        bits = [[0] * width for _ in range(rows)]
        glow = [[0.0] * width for _ in range(rows)]
        colors: list[list[Color]] = [[self._color] * width for _ in range(rows)]
        # Power-up: the drawing fades in from the bottom.
        reveal = dots_h * (1 - ease_out(self._elapsed / BOOT_SECONDS))

        def plot(x: float, y: float, brightness: float, color: Color) -> None:
            ix, iy = int(x), int(y)
            if 0 <= ix < dots_w and 0 <= iy < dots_h and iy >= reveal:
                cx, cy = ix // 2, iy // 4
                bits[cy][cx] |= DOT_BITS[iy % 4][ix % 2]
                if brightness > glow[cy][cx]:
                    glow[cy][cx] = brightness
                    colors[cy][cx] = color

        state = self._state
        try:
            robot = getattr(self.app, "hefesto_robot_status", None)
        except Exception:  # not mounted (previews, tests)
            robot = None
        context = RobotContext(
            color=self._color,
            phase=phase_of(state.failed, state.asking, state.busy, state.planning),
            elapsed=self._elapsed,
            rotation=self._angle,
            activity=self._activity,
            voice=self._voice_level,
            speaking=state.speaking,
            listening=state.listening,
            robot=robot,
        )
        theme.draw(Canvas(plot, dots_w, dots_h), context)
        return self._to_text(bits, glow, colors)

    def _render_label(self, width: int) -> Text:
        state = self._state
        just_done = (
            self._turn_done_at is not None
            and self._elapsed - self._turn_done_at < DONE_LABEL_SECONDS
        )
        if self._elapsed < BOOT_SECONDS:
            phase, status = "iniciando", "…"
        elif state.listening:
            phase, status = "ouvindo", "fale agora"
        elif state.transcribing:
            phase, status = "transcrevendo", "…"
        elif state.failed:
            phase, status = "erro", "veja a conversa"
        elif not state.connected:
            phase, status = "conectando", "…"
        elif state.asking:
            phase, status = "aguardando você", "permissão"
        else:
            phase = "planejamento" if state.planning else "execução"
            if (eye := self.eye) is not None:
                if state.elevated:
                    phase = eye.labels[2]
                elif not state.planning:
                    phase = eye.labels[1]
            if state.speaking:
                status = "falando"
            elif state.busy and state.elevated:
                status = "skill…"
            elif state.busy:
                status = "trabalhando…"
            elif just_done:
                status = "concluído ✓"
            elif state.watching:
                status = "diga “Íris”"
            else:
                status = "pronto"
        label = Text(no_wrap=True, overflow="crop")
        label.append(
            f"{phase.upper()} · {status}".center(width),
            Style(color=self._color.rich_color, bold=True),
        )
        label.append("\n")
        mode_line = " · ".join(name for name in (state.mode_name, state.model_name) if name)
        label.append((mode_line or " ").center(width), Style(dim=True))
        label.append("\n")
        label.append(self._render_usage(width))
        return label

    def _render_usage(self, width: int) -> Text:
        """Subscription usage (e.g. "5h 17% ↻14:00 · semana 9%"), colored by level."""
        from toad.iris_usage import usage_segments

        limits = getattr(self.app, "iris_rate_limits", None)
        show = self._setting("iris.show_usage", "true")
        if limits is None or str(show).lower() in ("false", "0"):
            return Text(" ")
        segments = usage_segments(limits)
        if sum(len(text) for text, _ in segments) > width:
            segments = usage_segments(limits, compact=True)
        styles = {
            "ok": Style(),
            "dim": Style(dim=True),
            "warn": Style(
                color=self._parse_color(
                    "iris.attention_color", DEFAULT_ATTENTION_COLOR
                ).rich_color,
                bold=True,
            ),
            "danger": Style(
                color=self._parse_color("iris.error_color", DEFAULT_ERROR_COLOR).rich_color,
                bold=True,
            ),
        }
        length = sum(len(text) for text, _ in segments)
        usage = Text(" " * max((width - length) // 2, 0), no_wrap=True, overflow="crop")
        for text, level in segments:
            usage.append(text, styles.get(level, Style()))
        return usage
