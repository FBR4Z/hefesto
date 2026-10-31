"""Preview the orb's robotic and eye themes in each level, without starting the app.

    uv run python tools/preview_orb.py [largura] [linhas] [--tema sharingan,hal]
    uv run --with pillow python tools/preview_orb.py 40 12 --png olhos.png

Without --png it prints the braille text; with --png it draws the dots, in color, on
a dark background: one row per theme, one column per level (planning, execution,
elevated). Without --tema it shows every eye.
"""

import sys

from rich.console import Console
from textual.color import Color
from toad.widgets.hefesto_robots import ROBOTS, RobotStatus, RobotTheme
from toad.widgets.iris_eyes import EYES, EyeTheme
from toad.widgets.iris_orb import IrisOrb, OrbState

ROBOT_STATES = (
    ("repouso", OrbState(connected=True), "#29c7ff"),
    ("pensando", OrbState(connected=True, busy=True, planning=True), "#29c7ff"),
    ("executando", OrbState(connected=True, busy=True, planning=False), "#ff8a1f"),
    ("aguardando você", OrbState(connected=True, asking=True), "#ffd23f"),
    ("falha", OrbState(connected=True, failed=True), "#ff4d5e"),
)


def render_robot(theme: RobotTheme, level: int, width: int, rows: int, elapsed: float = 10.0):
    _, state, color = ROBOT_STATES[level]
    orb = IrisOrb()
    orb._elapsed = elapsed
    orb._angle = 0.6 + elapsed * 0.4
    orb._state = state
    orb._activity = 1.0 if state.busy else 0.0
    orb._color = Color.parse(color)
    orb._intensity = 1.0
    return orb._render_robot(width, rows, theme)

STATES = (
    (OrbState(connected=True, planning=True), 0.0),
    (OrbState(connected=True, planning=False, busy=True), 0.0),
    (OrbState(connected=True, planning=False, busy=True, elevated=True), 1.0),
)
BACKGROUND = (18, 18, 24)
DOT = 6
"""Pixels per braille dot."""


def render(eye: EyeTheme, level: int, width: int, rows: int):
    state, elevation = STATES[level]
    orb = IrisOrb()
    orb._elapsed = 10.0
    orb._angle = 0.6
    orb._state = state
    orb._elevation = elevation
    orb._activity = 1.0 if state.busy else 0.0
    target = Color.parse((eye.plan_color, eye.exec_color, eye.elevated_color)[level])
    # Settle the color on the state's target, as the running app would.
    for _ in range(60):
        orb._color = orb._color.blend(target, 0.3)
    return orb._render_eye(width, rows, eye)


def save_png(path: str, eyes: list, width: int, rows: int) -> None:
    from PIL import Image, ImageDraw

    console = Console(color_system="truecolor")
    gap = 4 * DOT
    label = 3 * DOT
    panel_w, panel_h = width * 2 * DOT, rows * 4 * DOT
    columns = max(len(ROBOT_STATES) if isinstance(eye, RobotTheme) else len(STATES) for eye in eyes)
    image = Image.new(
        "RGB",
        (columns * panel_w + (columns + 1) * gap, len(eyes) * (panel_h + gap + label) + gap),
        BACKGROUND,
    )
    draw = ImageDraw.Draw(image)
    for line, eye in enumerate(eyes):
        top = gap + line * (panel_h + gap + label)
        robot = isinstance(eye, RobotTheme)
        for level in range(len(ROBOT_STATES) if robot else len(STATES)):
            left = gap + level * (panel_w + gap)
            if robot:
                phase = ROBOT_STATES[level][0]
                text = render_robot(eye, level, width, rows)
            else:
                phase = "planejamento" if level == 0 else eye.labels[level]
                text = render(eye, level, width, rows)
            draw.text((left, top), f"{eye.title} · {phase}", fill=(200, 200, 210))
            row = col = 0
            for offset, char in enumerate(text.plain):
                if char == "\n":
                    row, col = row + 1, 0
                    continue
                code = ord(char) - 0x2800
                if 0 < code < 256:
                    style = text.get_style_at_offset(console, offset)
                    color = style.color.get_truecolor() if style.color else (255, 255, 255)
                    for dy, bits in enumerate(((0x01, 0x08), (0x02, 0x10), (0x04, 0x20), (0x40, 0x80))):
                        for dx, bit in enumerate(bits):
                            if code & bit:
                                x = left + (col * 2 + dx) * DOT
                                y = top + label + (row * 4 + dy) * DOT
                                draw.ellipse((x + 1, y + 1, x + DOT - 1, y + DOT - 1), fill=tuple(color))
                col += 1
    image.save(path)
    print(path)


def main() -> None:
    argv = sys.argv[1:]
    options = {}
    for flag in ("--png", "--tema"):
        if flag in argv:
            index = argv.index(flag)
            options[flag] = argv[index + 1]
            del argv[index : index + 2]
    width = int(argv[0]) if len(argv) > 0 else 30
    rows = int(argv[1]) if len(argv) > 1 else 9
    names = options.get("--tema")
    themes = {**ROBOTS, **EYES}
    eyes = [themes[name.strip()] for name in names.split(",")] if names else list(themes.values())
    if "--png" in options:
        save_png(options["--png"], eyes, width, rows)
        return
    for eye in eyes:
        if isinstance(eye, RobotTheme):
            for level, (phase, _, _) in enumerate(ROBOT_STATES):
                print(f"--- {eye.title} · {phase} ---")
                print(render_robot(eye, level, width, rows).plain)
            continue
        for level in range(len(STATES)):
            print(f"--- {eye.title} · nível {level} ---")
            print(render(eye, level, width, rows).plain)


if __name__ == "__main__":
    main()
