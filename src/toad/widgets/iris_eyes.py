"""Eye themes for the Íris orb.

Each eye is a field: for a dot at normalized radius `r` (0 = center, 1 = rim) and
angle `theta` (radians, y pointing down) it returns `None` for a dark gap, or the
dot's brightness and, optionally, its own color (otherwise the orb's color is used).
Every eye shows three levels: planning, execution and "elevated" (a skill or a
sub-agent running, or a mode that skips permissions).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable

from textual.color import Color

TAU = math.tau
WHITE = Color(255, 255, 255)


@dataclass(frozen=True)
class EyeContext:
    """Animation state for one frame."""

    color: Color
    """The orb's current (eased) color."""
    rotation: float
    """Integrated angle: slow while idle, fast while the agent works."""
    elapsed: float
    planning: bool
    elevation: float
    """0 = normal, 1 = elevated; eased."""
    activity: float
    """0 = idle, 1 = busy; eased."""
    voice: float = 0.0
    """Smoothed loudness of the microphone or of Íris' own voice, 0..1."""
    speaking: bool = False

    @property
    def elevated(self) -> bool:
        return self.elevation > 0.5


Field = Callable[[EyeContext, float, float], "tuple[float, Color | None] | None"]


@dataclass(frozen=True)
class EyeTheme:
    key: str
    title: str
    plan_color: str
    exec_color: str
    elevated_color: str
    labels: tuple[str, str, str]
    """Phase shown under the orb: planning, execution, elevated."""
    field: Field
    help: str = ""


def rim_sheen(ctx: EyeContext, theta: float) -> float:
    """Bright rim, with a sheen that circles faster while working."""
    sheen = (ctx.rotation * 1.5 - theta) % TAU
    return 0.9 + 0.3 * max(0.0, 1 - sheen / (TAU * 0.3))


def angle_gap(a: float, b: float) -> float:
    """Smallest absolute difference between two angles."""
    return abs((a - b + math.pi) % TAU - math.pi)


def sharingan(ctx: EyeContext, r: float, theta: float):
    """Filled red iris; pupil, ring and tomoe are the dark gaps.

    One tomoe while planning, three while executing, and the Mangekyō (a three-bladed
    pinwheel) when elevated.
    """
    rotation = ctx.rotation
    if ctx.elevated:
        if r < 0.22:
            return None
        # Blades curve with the radius and taper towards the rim.
        phi = theta - rotation + 2.4 * r
        sector = TAU / 3
        offset = (phi + sector / 2) % sector - sector / 2
        if r < 0.93 and abs(offset) < 0.6 * (1 - r) ** 0.7 + 0.08:
            return None
    else:
        pupil = 0.17 * (1 + 0.5 * ctx.voice * ctx.speaking)
        ring, head, tail_arc = 0.58, 0.17, 1.1
        if r < pupil or abs(r - ring) < 0.035:
            return None
        tomoe = 1 if ctx.planning else 3
        for index in range(tomoe):
            angle = rotation + index * TAU / tomoe
            distance = math.sqrt(
                max(r * r + ring * ring - 2 * r * ring * math.cos(theta - angle), 0)
            )
            if distance < head:
                return None
            # The comma's tail hugs the ring behind the head, thinning to a point.
            behind = (angle - theta) % TAU
            if behind < tail_arc:
                along = behind / tail_arc
                if abs(r - (ring + 0.1 * along)) < head * 0.75 * (1 - along) ** 1.2:
                    return None
    if r > 0.86:
        return rim_sheen(ctx, theta), None
    return 0.45 + 0.4 * r, None


def rinnegan(ctx: EyeContext, r: float, theta: float):
    """Purple concentric rings drifting outwards (faster while working).

    Elevated, it turns into the Rinne Sharingan: red, with nine tomoe on three orbits.
    """
    if r < 0.075:
        return None
    spacing = 0.24 if ctx.planning and not ctx.elevated else 0.18
    # One full turn of the rotation moves the rings out by one spacing.
    offset = ctx.rotation / TAU * spacing
    position = (r - 0.075 - offset) % spacing
    if r < 0.93 and min(position, spacing - position) < 0.038:
        return None
    if ctx.elevated:
        for index, orbit in enumerate((0.34, 0.56, 0.78)):
            if abs(r - orbit) > 0.08:
                continue
            direction = 1 if index % 2 == 0 else -1
            for tomoe in range(3):
                angle = direction * ctx.rotation + tomoe * TAU / 3 + index * 0.6
                distance = math.sqrt(
                    max(r * r + orbit * orbit - 2 * r * orbit * math.cos(theta - angle), 0)
                )
                if distance < 0.075:
                    return None
    color = ctx.color.blend(WHITE, 0.3 * (1 - r))
    if r > 0.86:
        return rim_sheen(ctx, theta), color
    return 0.5 + 0.35 * r, color


def byakugan(ctx: EyeContext, r: float, theta: float):
    """Pale iris without a pupil, with veins bulging around it.

    Few veins while planning, more (pulsing) while executing, all around when elevated.
    """
    iris = 0.58
    if r <= iris:
        if abs(r - 0.3) < 0.04:
            return None
        return 0.6 + 0.3 * r / iris, ctx.color.blend(WHITE, 0.35 * (1 - r / iris))
    if r < iris + 0.07:
        return None
    if ctx.elevated:
        veins, reach = 16, 1.0
    elif ctx.planning:
        veins, reach = 6, 0.8
    else:
        veins = 10
        reach = 0.86 + 0.14 * ctx.activity * (0.5 + 0.5 * math.sin(ctx.elapsed * 4))
    if r > reach:
        return None
    step = TAU / veins
    along = (r - iris) / (1 - iris)
    width = 0.075 * (1 - 0.4 * along)
    nearest = round(theta / step)
    for k in (nearest - 1, nearest, nearest + 1):
        seed = k % veins
        wiggle = 0.12 * along * math.sin(r * 9 + seed * 1.7 + ctx.elapsed * (1 + 2 * ctx.activity))
        angle = k * step + 0.3 * step * math.sin(seed * 2.3) + wiggle
        if angle_gap(theta, angle) * r < width:
            return 0.75, None
        if along > 0.35:
            # A branch splitting off towards the tip.
            fork = angle + (1 if seed % 2 else -1) * 0.8 * (along - 0.35)
            if angle_gap(theta, fork) * r < width * 0.8:
                return 0.7, None
    return None


SAGE_ORANGE = Color(255, 104, 24)


def sabio(ctx: EyeContext, r: float, theta: float):
    """Modo Sábio: golden toad eye with a horizontal bar pupil (a nod to Toad).

    The pupil widens while working and sweeps from side to side; elevated, an orange
    pigment band rings the eye.
    """
    x, y = r * math.cos(theta), r * math.sin(theta)
    if ctx.elevated:
        height = 0.17
    elif ctx.planning:
        height = 0.075
    else:
        height = 0.12
    height *= 1 + 0.4 * ctx.voice * ctx.speaking
    shift = 0.12 * math.sin(ctx.rotation) * ctx.activity
    if (abs(x - shift) / 0.52) ** 4 + (abs(y) / height) ** 2 < 1:
        return None
    color = ctx.color.blend(SAGE_ORANGE, 0.5 * r * r)
    if ctx.elevation > 0 and r > 0.74:
        if r < 0.8:
            return None
        color = color.blend(SAGE_ORANGE, 0.8 * ctx.elevation)
    if r > 0.86:
        return rim_sheen(ctx, theta), color
    return 0.68 + 0.2 * r, color


SAURON_HEAT = Color(255, 232, 150)
SAURON_EMBER = Color(150, 16, 0)


def sauron(ctx: EyeContext, r: float, theta: float):
    """A flickering eye of fire with a vertical slit; the slit opens as the level rises."""
    rotation = ctx.rotation
    edge = 0.84 + 0.07 * math.sin(5 * theta + 2 * rotation) + 0.05 * math.sin(
        11 * theta - 3 * rotation
    )
    if r > edge:
        return None
    x, y = r * math.cos(theta), r * math.sin(theta)
    if ctx.elevated:
        slit = 0.13
    elif ctx.planning:
        slit = 0.045
    else:
        slit = 0.075
    slit *= 1 + 0.5 * ctx.voice * ctx.speaking
    if abs(y) < 0.92:
        half = slit * math.sqrt(1 - (y / 0.92) ** 2)
        if abs(x) < half:
            return None
    else:
        half = 0.0
    streak = 0.5 + 0.5 * math.sin(13 * theta - 4 * rotation + 9 * r)
    heat = math.exp(-(((abs(x) - half) / 0.14) ** 2))
    color = ctx.color.blend(SAURON_HEAT, 0.8 * heat).blend(
        SAURON_EMBER, 0.55 * (r / edge) ** 3
    )
    return 0.5 + 0.25 * streak + 0.35 * heat, color


HAL_SILVER = Color(186, 190, 200)
HAL_CORE = Color(255, 214, 120)


def hal(ctx: EyeContext, r: float, theta: float):
    """HAL 9000: silver rim, black lens and a red glow that grows with the level."""
    if r > 0.9:
        return 0.35 + 0.35 * max(0.0, 1 - ((ctx.rotation * 1.5 - theta) % TAU) / (TAU * 0.3)), HAL_SILVER
    if r > 0.8:
        return None
    x, y = r * math.cos(theta), r * math.sin(theta)
    # A faint reflection on the glass.
    if math.hypot(x + 0.36, y + 0.36) < 0.07:
        return 0.35, WHITE
    if ctx.elevated:
        sigma = 0.62
    elif ctx.planning:
        sigma = 0.3
    else:
        sigma = 0.42 + 0.05 * ctx.activity * math.sin(ctx.elapsed * 5)
    sigma *= 1 + 0.4 * ctx.voice * ctx.speaking
    glow = math.exp(-((r / sigma) ** 2))
    if glow < 0.12:
        return None
    color = ctx.color.blend(HAL_CORE, max(0.0, 1 - r / (0.4 * sigma)))
    return 0.3 + 0.8 * glow, color


def tenseigan(ctx: EyeContext, r: float, theta: float):
    """Blue eye with a white flower around the pupil, which turns while working.

    Elevated (chakra mode) the flower doubles its petals and the whole eye glows.
    """
    if r < 0.1:
        return None
    if ctx.elevated:
        petals = 0.44 + 0.1 * math.cos(12 * theta - 6 * ctx.rotation)
    else:
        spin = ctx.rotation if not ctx.planning else 0.0
        petals = 0.46 + 0.14 * math.cos(6 * theta - 6 * spin)
    if abs(r - petals) < 0.035 or abs(r - 0.72) < 0.03:
        return None
    if r < petals:
        # The flower: pale, almost white.
        return 0.8 + 0.2 * ctx.elevation, ctx.color.blend(WHITE, 0.55 + 0.3 * ctx.elevation)
    color = ctx.color.blend(WHITE, 0.2 * ctx.elevation)
    if r > 0.86:
        return rim_sheen(ctx, theta), color
    return 0.55 + 0.3 * r + 0.3 * ctx.elevation, color


def jogan(ctx: EyeContext, r: float, theta: float):
    """Pale blue eye with a lit pupil, a dark ring around it and fine radial striae.

    The striae flow while working; elevated, the pupil grows and rays shoot to the rim.
    """
    if ctx.elevated:
        pupil = 0.22
    elif ctx.planning:
        pupil = 0.12
    else:
        pupil = 0.16
    pupil *= 1 + 0.4 * ctx.voice * ctx.speaking
    if r < pupil:
        return 1.0, WHITE
    if r < pupil + 0.07:
        return None
    if ctx.elevated and math.cos(8 * theta - 2 * ctx.rotation) > 0.9:
        return 1.05, ctx.color.blend(WHITE, 0.6)
    flow = 0.0 if ctx.planning else ctx.rotation
    if r < 0.84 and math.cos(20 * theta - 2 * flow + 3 * r) > 0.75:
        return None
    color = ctx.color.blend(WHITE, 0.25 * (1 - r))
    if r > 0.86:
        return rim_sheen(ctx, theta), color
    return 0.5 + 0.35 * r, color


HUMAN_AMBER = Color(205, 150, 60)
HUMAN_LIMBUS = Color(20, 30, 28)


def humana(ctx: EyeContext, r: float, theta: float):
    """A realistic human iris: dark limbal ring, radial fibres, amber collarette.

    The pupil contracts while working (focus) and dilates when elevated.
    """
    if ctx.elevated:
        pupil = 0.4
    elif ctx.planning:
        pupil = 0.28 + 0.02 * math.sin(ctx.elapsed * 0.9)
    else:
        pupil = 0.2
    pupil *= 1 + 0.25 * ctx.voice * ctx.speaking
    x, y = r * math.cos(theta), r * math.sin(theta)
    # Light reflected on the cornea.
    if math.hypot(x + 0.32, y + 0.36) < 0.055:
        return 1.1, WHITE
    if r < pupil:
        return None
    # Radial fibres: thin light strands that wander a little.
    strands = math.cos(28 * theta + 3 * math.sin(9 * r + 5 * theta))
    fibres = math.sin(37 * theta + 4 * r) * math.sin(23 * theta - 7 * r + 1.3)
    if fibres < -0.8 and 0.5 < r < 0.85:
        # Crypts: small dark pits in the stroma.
        return None
    collarette = pupil + 0.14 + 0.03 * math.sin(14 * theta)
    color = ctx.color
    brightness = 0.55 + 0.1 * r
    if strands > 0.8:
        color = color.blend(WHITE, 0.15)
        brightness += 0.15
    if r < collarette:
        color = color.blend(HUMAN_AMBER, 0.8)
        brightness += 0.1
    if r > 0.86:
        return 0.55, color.blend(HUMAN_LIMBUS, 0.6)
    # Stay under the orb's white glow (0.85) so the colors hold.
    return min(brightness, 0.84), color


EYES: dict[str, EyeTheme] = {
    theme.key: theme
    for theme in (
        EyeTheme(
            "sharingan",
            "Sharingan",
            "#a8101f",
            "#e3122d",
            "#ff2a3f",
            ("planejamento", "sharingan", "mangekyō"),
            sharingan,
            "1 tomoe no planejamento, 3 na execução, Mangekyō no nível máximo",
        ),
        EyeTheme(
            "rinnegan",
            "Rinnegan",
            "#8f6bff",
            "#b394ff",
            "#e3122d",
            ("planejamento", "rinnegan", "rinne sharingan"),
            rinnegan,
            "anéis roxos que se expandem; Rinne Sharingan (9 tomoe) no nível máximo",
        ),
        EyeTheme(
            "byakugan",
            "Byakugan",
            "#bdb6e0",
            "#dcd4ff",
            "#ffffff",
            ("planejamento", "byakugan", "byakugan 360°"),
            byakugan,
            "olho pálido; as veias saltam e pulsam na execução, em volta toda no nível máximo",
        ),
        EyeTheme(
            "sabio",
            "Modo Sábio (sapo)",
            "#e8b923",
            "#ffc21a",
            "#ffa31a",
            ("planejamento", "modo sábio", "senjutsu"),
            sabio,
            "olho de sapo dourado com pupila horizontal; faixa laranja no nível máximo",
        ),
        EyeTheme(
            "sauron",
            "Olho de Sauron",
            "#e04a00",
            "#ff6a00",
            "#ff9a2e",
            ("planejamento", "execução", "o olho te vê"),
            sauron,
            "olho de fogo com fenda vertical, que se abre conforme o nível",
        ),
        EyeTheme(
            "hal",
            "HAL 9000",
            "#c8101c",
            "#ff1f1f",
            "#ff3b30",
            ("planejamento", "execução", "desculpe, dave"),
            hal,
            "lente preta com brilho vermelho que cresce conforme o nível",
        ),
        EyeTheme(
            "tenseigan",
            "Tenseigan",
            "#3f8fe8",
            "#5fb0ff",
            "#9fd8ff",
            ("planejamento", "tenseigan", "modo chakra"),
            tenseigan,
            "olho azul com flor branca em volta da pupila, que gira na execução",
        ),
        EyeTheme(
            "jogan",
            "Jōgan",
            "#6fb6d6",
            "#8fd6f5",
            "#c9efff",
            ("planejamento", "jōgan", "jōgan desperto"),
            jogan,
            "olho azul-claro com pupila acesa e estrias que correm na execução",
        ),
        EyeTheme(
            "humana",
            "Íris humana",
            "#4f8a63",
            "#5f9c6c",
            "#6d9a5a",
            ("planejamento", "foco", "pupila dilatada"),
            humana,
            "íris verde-avelã realista; a pupila contrai no foco e dilata no nível máximo",
        ),
    )
}
