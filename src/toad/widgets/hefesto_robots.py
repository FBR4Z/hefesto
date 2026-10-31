"""Hefesto's robotic themes for the orb: an arm, a stack light, a flange, a radar...

Each theme draws on a braille canvas laid out for 72x40 dots (scaled to the widget).
The AI's state (idle, thinking, executing, waiting for you, failed) drives the motion
and the main color; the robot's state, when a controller is connected, drives the
stack light and, for the arm themes, the joint angles.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Literal

from textual.color import Color

TAU = math.tau
BASE_W, BASE_H = 72, 40
"""Dots the themes are drawn for."""

type Phase = Literal["repouso", "pensando", "executando", "confirmacao", "falha"]

ANDON_COLORS = {
    "vermelho": Color.parse("#f03c3c"),
    "amarelo": Color.parse("#ffcd28"),
    "verde": Color.parse("#3cdc6e"),
    "azul": Color.parse("#4696ff"),
}
JOINT_COLOR = Color.parse("#e6ebf5")
PART_COLOR = Color.parse("#c8c8d2")
STEEL = Color.parse("#78788a")


@dataclass
class RobotStatus:
    """What the HMI knows about the robot controller (filled by `hefesto_robot`)."""

    connected: bool = False
    name: str = ""
    real: bool | None = None
    """True for a real controller, False for a virtual one, None if unknown."""
    opmode: str = ""
    """AUTO, MANUAL, MANUAL_FULL_SPEED..."""
    motors: str = ""
    """Controller state: motoron, motoroff, guardstop, emergencystop, sysfail..."""
    running: bool = False
    speed: int | None = None
    """Speed ratio, %."""
    joints: tuple[float, ...] | None = None
    """Joint angles in degrees (axes 1-6)."""
    events: list[tuple[float, str]] = field(default_factory=list)
    """(monotonic time, severity) of recent controller events: info, warning, error."""
    error: str = ""
    """Why the HMI can't reach the controller, if it can't."""

    @property
    def stopped_hard(self) -> bool:
        state = self.motors.lower()
        return any(word in state for word in ("emergency", "guard", "sysfail", "estop"))


@dataclass
class RobotContext:
    color: Color
    """The orb's current (eased) color for the AI state."""
    phase: Phase
    elapsed: float
    rotation: float
    activity: float
    voice: float
    speaking: bool
    listening: bool
    robot: RobotStatus | None


def phase_of(failed: bool, asking: bool, busy: bool, planning: bool) -> Phase:
    if failed:
        return "falha"
    if asking:
        return "confirmacao"
    if busy:
        return "pensando" if planning else "executando"
    return "repouso"


class Canvas:
    """Plots on the orb's braille buffers, scaling the 72x40 design to the widget."""

    def __init__(self, plot: Callable[[float, float, float, Color], None], dots_w: int, dots_h: int):
        self._plot = plot
        self.scale = min(dots_w / BASE_W, dots_h / BASE_H)
        self.ox = (dots_w - BASE_W * self.scale) / 2
        self.oy = (dots_h - BASE_H * self.scale) / 2

    def dot(self, x: float, y: float, color: Color, brightness: float = 1.0) -> None:
        self._plot(self.ox + x * self.scale, self.oy + y * self.scale, brightness, color)

    def line(self, x0, y0, x1, y1, color: Color, brightness: float = 1.0, thick: int = 0) -> None:
        steps = int(max(abs(x1 - x0), abs(y1 - y0)) * 2 * max(self.scale, 1)) + 1
        for index in range(steps + 1):
            t = index / steps
            x, y = x0 + (x1 - x0) * t, y0 + (y1 - y0) * t
            if thick:
                for dx in range(-thick, thick + 1):
                    for dy in range(-thick, thick + 1):
                        if dx * dx + dy * dy <= thick * thick + 0.5:
                            self.dot(x + dx * 0.8, y + dy * 0.8, color, brightness)
            else:
                self.dot(x, y, color, brightness)

    def circle(self, cx, cy, r, color: Color, brightness: float = 1.0, fill: bool = False) -> None:
        if fill:
            step = 0.5 / max(self.scale, 1)
            y = cy - r
            while y <= cy + r:
                half = math.sqrt(max(r * r - (y - cy) ** 2, 0))
                x = cx - half
                while x <= cx + half:
                    self.dot(x, y, color, brightness)
                    x += step
                y += step
            return
        samples = int(TAU * r * 2 * max(self.scale, 1)) + 8
        for index in range(samples):
            angle = TAU * index / samples
            self.dot(cx + r * math.cos(angle), cy - r * math.sin(angle), color, brightness)

    def rect(self, x0, y0, x1, y1, color: Color, brightness: float = 1.0, fill: bool = False) -> None:
        if fill:
            y = y0
            while y <= y1:
                self.line(x0, y, x1, y, color, brightness)
                y += 0.5
        else:
            self.line(x0, y0, x1, y0, color, brightness)
            self.line(x0, y1, x1, y1, color, brightness)
            self.line(x0, y0, x0, y1, color, brightness)
            self.line(x1, y0, x1, y1, color, brightness)


# ------------------------------------------------------------------ stack light


def andon_light(context: RobotContext) -> tuple[str | None, bool, bool]:
    """(color name or None, bright?, blinking?) for the stack light.

    With a controller: red = emergency/guard stop or a fresh error, yellow = manual
    mode or motors off, green = program running, blue = the AI waits for you.
    Without one, the light follows the AI.
    """
    robot = context.robot
    if robot is not None and robot.connected:
        recent_error = any(
            severity == "error" and context.elapsed - when < 10 for when, severity in robot.events
        )
        if robot.stopped_hard or recent_error:
            return "vermelho", True, True
        if context.phase == "confirmacao":
            return "azul", True, True
        if "manual" in robot.opmode.lower() or "off" in robot.motors.lower():
            return "amarelo", True, False
        if robot.running:
            return "verde", True, False
        return "verde", False, False
    return {
        "repouso": ("verde", False, False),
        "pensando": ("azul", True, False),
        "executando": ("verde", True, False),
        "confirmacao": ("amarelo", True, True),
        "falha": ("vermelho", True, True),
    }[context.phase]


def draw_tower(canvas: Canvas, context: RobotContext, cx: float, top: float, seg_w: float, seg_h: float, rays: bool) -> None:
    lit, strong, blink = andon_light(context)
    on = not blink or math.sin(context.elapsed * 6) > -0.2
    for index, name in enumerate(("vermelho", "amarelo", "verde", "azul")):
        color = ANDON_COLORS[name]
        y0 = top + index * (seg_h + 1)
        x0, x1 = cx - seg_w / 2, cx + seg_w / 2
        if name == lit and on:
            canvas.rect(x0, y0, x1, y0 + seg_h - 1, color, 1.0 if strong else 0.6, fill=True)
            if strong and rays:
                middle = y0 + seg_h / 2
                for dy in (-2, 0, 2):
                    canvas.line(x0 - 3, middle + dy * 1.5, x0 - 6, middle + dy * 2.5, color)
                    canvas.line(x1 + 3, middle + dy * 1.5, x1 + 6, middle + dy * 2.5, color)
        else:
            canvas.rect(x0, y0, x1, y0 + seg_h - 1, color, 0.28)
    bottom = top + 4 * (seg_h + 1)
    canvas.line(cx, bottom, cx, BASE_H - 3, STEEL, 0.8)
    canvas.rect(cx - seg_w / 2, BASE_H - 3, cx + seg_w / 2, BASE_H - 1, STEEL, 0.8, fill=True)


# -------------------------------------------------------------------------- arm

POSES: dict[str, tuple[float, float, float]] = {
    # shoulder (absolute), elbow and wrist (relative), degrees
    "home": (90, -90, -90),
    "hover_pick": (55, -95, -50),
    "pick": (42, -98, -34),
    "hover_place": (40, -70, -60),
    "place": (30, -70, -50),
    "think": (75, -55, -70),
    "wait": (60, -85, -65),
    "slump": (25, -55, -60),
}

PICK_CYCLE = (
    # (pose, seconds to get there, gripper closed after?, part in the gripper?)
    ("hover_pick", 0.7, False, False),
    ("pick", 0.5, True, False),
    ("hover_pick", 0.5, True, True),
    ("hover_place", 0.9, True, True),
    ("place", 0.5, False, True),
    ("hover_place", 0.5, False, False),
)
CYCLE_SECONDS = sum(step[1] for step in PICK_CYCLE)


def _blend(a: tuple[float, float, float], b: tuple[float, float, float], t: float) -> tuple[float, float, float]:
    t = 0.5 - 0.5 * math.cos(math.pi * min(max(t, 0.0), 1.0))  # ease in-out
    return tuple(x + (y - x) * t for x, y in zip(a, b))  # type: ignore[return-value]


def arm_pose(context: RobotContext) -> tuple[tuple[float, float, float], bool, bool]:
    """(angles, gripper closed?, carrying the part?) for this frame."""
    robot = context.robot
    if robot is not None and robot.connected and robot.joints and len(robot.joints) >= 5:
        # Mirror the real robot (side view): ABB axes 2, 3 and 5.
        j2, j3, j5 = robot.joints[1], robot.joints[2], robot.joints[4]
        return (90 - j2, -90 - j3, -j5), False, False
    t = context.elapsed
    phase = context.phase
    if phase == "repouso":
        home = POSES["home"]
        return (home[0] + 2 * math.sin(t * 0.9), home[1] - 2 * math.sin(t * 0.9), home[2]), False, False
    if phase == "pensando":
        s, e, w = POSES["think"]
        return (s + 6 * math.sin(t * 0.7), e + 10 * math.sin(t * 0.9), w + 30 * math.sin(t * 1.4)), False, False
    if phase == "confirmacao":
        return POSES["wait"], False, False
    if phase == "falha":
        return POSES["slump"], False, False
    position = (t * 1.0) % CYCLE_SECONDS
    previous = PICK_CYCLE[-1]
    for step in PICK_CYCLE:
        if position <= step[1]:
            angles = _blend(POSES[previous[0]], POSES[step[0]], position / step[1])
            return angles, previous[2], step[3]
        position -= step[1]
        previous = step
    return POSES["home"], False, False


def draw_arm(canvas: Canvas, context: RobotContext, base_x: float = 36, conveyor: bool = True) -> None:
    color = context.color
    (s1, s2, s3), closed, carrying = arm_pose(context)
    base_y = BASE_H - 3
    canvas.rect(base_x - 7, base_y - 1, base_x + 7, base_y + 1, color, 0.55, fill=True)
    sx, sy = base_x, base_y - 9
    canvas.line(base_x, base_y - 1, sx, sy, color, thick=1)
    a1 = math.radians(s1)
    ex, ey = sx + 14 * math.cos(a1), sy - 14 * math.sin(a1)
    a2 = a1 + math.radians(s2)
    wx, wy = ex + 13 * math.cos(a2), ey - 13 * math.sin(a2)
    a3 = a2 + math.radians(s3)
    tx, ty = wx + 4 * math.cos(a3), wy - 4 * math.sin(a3)
    canvas.line(sx, sy, ex, ey, color, thick=1)
    canvas.line(ex, ey, wx, wy, color, thick=1)
    canvas.line(wx, wy, tx, ty, color)
    for jx, jy in ((sx, sy), (ex, ey), (wx, wy)):
        canvas.circle(jx, jy, 2.6, JOINT_COLOR, 1.05)
    px, py = -math.sin(a3), -math.cos(a3)
    spread = 1.2 if closed else 2.5
    for side in (-1, 1):
        fx, fy = tx + px * spread * side, ty + py * spread * side
        canvas.line(tx, ty, fx, fy, color)
        canvas.line(fx, fy, fx + 2.5 * math.cos(a3), fy - 2.5 * math.sin(a3), color)
    if conveyor:
        belt = 0.4
        canvas.line(base_x + 10, BASE_H - 2, base_x + 34, BASE_H - 2, color, belt)
        shift = (context.elapsed * 3 * context.activity) % 5
        x = base_x + 12 + shift
        while x < base_x + 34:
            canvas.dot(x, BASE_H - 1, color, belt)
            x += 5
        if carrying:
            gx, gy = tx + 3 * math.cos(a3), ty - 3 * math.sin(a3)
            canvas.rect(gx - 2, gy, gx + 2, gy + 3, PART_COLOR, 1.05, fill=True)
        elif context.phase != "executando" or not closed:
            part_x = base_x + 17
            canvas.rect(part_x - 2, BASE_H - 6, part_x + 2, BASE_H - 3, PART_COLOR, 0.9, fill=True)
    if context.phase == "falha":
        canvas.line(base_x - 14, 4, base_x - 10, 4, color)
        canvas.line(base_x - 12, 2, base_x - 12, 6, color)


# ----------------------------------------------------------------------- themes


def theme_arm(canvas: Canvas, context: RobotContext) -> None:
    draw_arm(canvas, context)


def theme_andon(canvas: Canvas, context: RobotContext) -> None:
    draw_tower(canvas, context, 36, 2, 14, 7, rays=True)


def theme_combined(canvas: Canvas, context: RobotContext) -> None:
    draw_arm(canvas, context, base_x=24)
    draw_tower(canvas, context, 66, 4, 6, 4, rays=False)


def theme_flange(canvas: Canvas, context: RobotContext) -> None:
    color = context.color
    robot = context.robot
    angle = context.rotation * 1.5
    if robot is not None and robot.connected and robot.speed is not None:
        speed = robot.speed / 100 if robot.running else 0.0
    else:
        speed = context.activity
    cx, cy = 36, 20
    canvas.circle(cx, cy, 15, color)
    canvas.circle(cx, cy, 4, color)
    for k in range(6):
        a = angle + k * TAU / 6
        canvas.circle(cx + 10 * math.cos(a), cy - 10 * math.sin(a), 1.8, color, fill=True)
        if speed > 0.5:
            for trail in (1, 2):
                b = a - 0.22 * trail
                canvas.dot(cx + 10 * math.cos(b), cy - 10 * math.sin(b), color, 0.5 / trail)
    pin = angle + TAU / 12
    canvas.circle(cx + 7 * math.cos(pin), cy - 7 * math.sin(pin), 1, JOINT_COLOR, 1.05, fill=True)
    ticks = 36
    for k in range(ticks):
        a = math.pi / 2 - k * TAU / ticks
        lit = k < round(speed * ticks)
        canvas.line(
            cx + 17.5 * math.cos(a), cy - 17.5 * math.sin(a),
            cx + 19.5 * math.cos(a), cy - 19.5 * math.sin(a),
            color, 1.0 if lit else 0.22,
        )
    if context.phase == "falha":
        canvas.line(cx - 3, cy - 3, cx + 3, cy + 3, color)
        canvas.line(cx - 3, cy + 3, cx + 3, cy - 3, color)


def theme_radar(canvas: Canvas, context: RobotContext) -> None:
    color = context.color
    cx, cy = 36, 20
    for r in (6, 12, 18):
        canvas.circle(cx, cy, r, color, 0.25)
    canvas.line(cx - 19, cy, cx + 19, cy, color, 0.18)
    canvas.line(cx, cy - 19, cx, cy + 19, color, 0.18)
    sweep = context.rotation * 2.0
    if context.phase != "falha":
        for k in range(10, -1, -1):
            a = sweep - k * 0.09
            canvas.line(cx, cy, cx + 18 * math.cos(a), cy - 18 * math.sin(a), color, 1 - k * 0.085)
    # Blips: controller events (fading over 30 s), placed by their time.
    robot = context.robot
    events = robot.events if robot is not None else []
    for when, severity in events[-8:]:
        age = context.elapsed - when
        if age > 30:
            continue
        a = (when * 2.399) % TAU
        r = 6 + (when * 7.13) % 12
        blip = ANDON_COLORS["vermelho"] if severity == "error" else ANDON_COLORS["amarelo"] if severity == "warning" else JOINT_COLOR
        canvas.circle(cx + r * math.cos(a), cy - r * math.sin(a), 1.2, blip, 1.05 - age / 40, fill=True)


def theme_pendant(canvas: Canvas, context: RobotContext) -> None:
    color = context.color
    canvas.rect(8, 1, 64, 38, STEEL, 0.8)
    canvas.rect(12, 4, 50, 30, color, 0.45)
    t = context.elapsed
    if context.listening or context.speaking:
        amplitude = 2 + 9 * context.voice
    else:
        amplitude = {"repouso": 0.6, "pensando": 3.0, "executando": 7.0, "confirmacao": 4.0, "falha": 5.0}[context.phase]
    previous = None
    for x in range(14, 49):
        u = (x - 14) / 34
        if context.phase == "confirmacao":
            y = 17 - amplitude * (1 if math.sin((u * 2 - t * 0.5) * TAU) > 0 else -1)
        elif context.phase == "falha":
            y = 17 - amplitude * ((u * 5) % 1 * 2 - 1) if u < 0.6 else 17
        else:
            y = 17 - amplitude * math.sin((u * 2.5 - t * 0.8) * TAU) * (0.6 + 0.4 * math.sin(u * 9 + t))
        if previous is not None:
            canvas.line(previous[0], previous[1], x, y, color)
        previous = (x, y)
    canvas.circle(57, 10, 4.5, ANDON_COLORS["vermelho"], 0.9, fill=True)
    canvas.circle(57, 10, 5.5, ANDON_COLORS["amarelo"], 0.8)
    canvas.circle(57, 25, 3.5, STEEL, 0.8)
    canvas.circle(57, 25, 1, STEEL, 0.8, fill=True)
    for bx in range(13, 50, 8):
        canvas.rect(bx, 33, bx + 5, 35, color, 0.6)


def theme_wireframe(canvas: Canvas, context: RobotContext) -> None:
    color = context.color
    robot = context.robot
    (s1, s2, _), _, _ = arm_pose(context)
    if robot is not None and robot.connected and robot.joints:
        yaw = math.radians(robot.joints[0]) + 0.6
    else:
        yaw = 0.6 + context.rotation * 0.5
    cx, cy = 36, 30

    def project(x: float, y: float, z: float) -> tuple[float, float]:
        xr = x * math.cos(yaw) - z * math.sin(yaw)
        zr = x * math.sin(yaw) + z * math.cos(yaw)
        return cx + xr, cy - y + zr * 0.35

    for g in range(-16, 17, 8):
        canvas.line(*project(g, 0, -16), *project(g, 0, 16), color, 0.16)
        canvas.line(*project(-16, 0, g), *project(16, 0, g), color, 0.16)
    n = 12
    for index in range(n):
        a0, a1 = index / n * TAU, (index + 1) / n * TAU
        for h, r in ((0, 6), (4, 4)):
            canvas.line(*project(r * math.cos(a0), h, r * math.sin(a0)), *project(r * math.cos(a1), h, r * math.sin(a1)), color, 0.6)
    shoulder = (0.0, 9.0, 0.0)
    a1 = math.radians(s1)
    elbow = (12 * math.cos(a1), 9 + 12 * math.sin(a1), 0.0)
    a2 = a1 + math.radians(s2)
    wrist = (elbow[0] + 12 * math.cos(a2), elbow[1] + 12 * math.sin(a2), 0.0)
    tool = (wrist[0], wrist[1] - 4, 0.0)
    for p, q in (((0.0, 4.0, 0.0), shoulder), (shoulder, elbow), (elbow, wrist), (wrist, tool)):
        for offset in (-1.2, 1.2):
            canvas.line(*project(p[0], p[1], p[2] + offset), *project(q[0], q[1], q[2] + offset), color)
    for joint in (shoulder, elbow, wrist):
        canvas.circle(*project(*joint), 1.8, JOINT_COLOR, 1.05)


@dataclass(frozen=True)
class RobotTheme:
    key: str
    title: str
    draw: Callable[[Canvas, RobotContext], None]


ROBOTS: dict[str, RobotTheme] = {
    theme.key: theme
    for theme in (
        RobotTheme("combinado", "Braço + torre de sinalização", theme_combined),
        RobotTheme("braco", "Braço 6 eixos", theme_arm),
        RobotTheme("andon", "Torre de sinalização (andon)", theme_andon),
        RobotTheme("flange", "Flange da ferramenta", theme_flange),
        RobotTheme("radar", "Radar de eventos", theme_radar),
        RobotTheme("ihm", "IHM / FlexPendant", theme_pendant),
        RobotTheme("wireframe", "Braço em wireframe 3D", theme_wireframe),
    )
}
DEFAULT_ROBOT_THEME = "combinado"
