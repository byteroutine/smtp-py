"""Neon Radar animated banner for the smtp-py menu. Standard library only."""

from __future__ import annotations

import math
import os
import re
import sys
import time

RESET = "\033[0m"
BOLD = "\033[1m"

NEON_PINK = (255, 45, 120)
NEON_CYAN = (45, 200, 255)
VIOLET = (174, 91, 255)
GREEN = (62, 225, 183)
DIM = (83, 101, 125)
WHITE = (232, 239, 249)
AMBER = (237, 185, 86)
MUTED = (126, 148, 174)

STAGES = [
    ("CONNECT", "Resolve SMTP endpoint"),
    ("AUTH", "Secure authentication handshake"),
    ("COMPOSE", "Prepare message payload"),
    ("DELIVER", "Transmit message"),
    ("VERIFY", "Await server response"),
]

_ANSI_RE = re.compile(r"\033\[[0-9;?]*[A-Za-z]")


def rgb(r: int, g: int, b: int, text: str, bold: bool = False) -> str:
    weight = BOLD if bold else ""
    return f"\033[38;2;{r};{g};{b}m{weight}{text}{RESET}"


def mix(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    t = max(0.0, min(1.0, t))
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def gradient(text: str, left=(42, 235, 231), right=(172, 90, 255), bold=True) -> str:
    chars = list(text)
    visible = max(1, len(chars) - 1)
    out = []
    for i, ch in enumerate(chars):
        if ch == " ":
            out.append(ch)
        else:
            out.append(rgb(*mix(left, right, i / visible), ch, bold=bold))
    return "".join(out)


def strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


def center_ansi(line: str, width: int) -> str:
    vis = len(strip_ansi(line))
    pad = max(0, (width - vis) // 2)
    return " " * pad + line


class Canvas:
    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height
        self.cells: dict[tuple[int, int], tuple[str, tuple[int, int, int], bool]] = {}

    def put(self, col: int, row: int, ch: str, color: tuple[int, int, int], bold: bool = False) -> None:
        if ch != " " and 0 <= col < self.width and 0 <= row < self.height:
            self.cells[(col, row)] = (ch, color, bold)

    def render_ansi(self) -> list[str]:
        lines = []
        for r in range(self.height):
            parts = []
            for c in range(self.width):
                cell = self.cells.get((c, r))
                if cell is None:
                    parts.append(" ")
                else:
                    ch, color, bold = cell
                    parts.append(rgb(*color, ch, bold=bold))
            lines.append("".join(parts).rstrip())
        return lines


def line_points(x0: float, y0: float, x1: float, y1: float) -> list[tuple[float, float]]:
    pts = []
    steps = int(max(abs(x1 - x0), abs(y1 - y0)) * 2) + 1
    for i in range(steps + 1):
        t = i / steps
        pts.append((x0 + (x1 - x0) * t, y0 + (y1 - y0) * t))
    return pts


def char_for(dx: float, dy: float) -> str:
    if abs(dy) < 0.25:
        return "─"
    if abs(dx) < 0.25:
        return "│"
    return "\\" if dx * dy > 0 else "/"


def neon_envelope_segments(cx: float, cy: float, w: float, h: float) -> list[list[tuple[float, float]]]:
    x0, x1 = cx - w / 2, cx + w / 2
    y0, y1 = cy - h / 2, cy + h / 2
    r, yr = 3.0, 1.7
    segs = [
        line_points(x0 + r, y0, x1 - r, y0),
        line_points(x0 + r, y1, x1 - r, y1),
        line_points(x0, y0 + r * 0.57, x0, y1 - r * 0.57),
        line_points(x1, y0 + r * 0.57, x1, y1 - r * 0.57),
    ]
    for ccx, ccy, a0 in [(x0 + r, y0 + yr, 180), (x1 - r, y0 + yr, 270),
                         (x0 + r, y1 - yr, 90), (x1 - r, y1 - yr, 0)]:
        arc = []
        for deg in range(0, 95, 8):
            rad = math.radians(a0 + deg)
            arc.append((ccx + r * math.cos(rad), ccy + yr * math.sin(rad)))
        segs.append(arc)
    segs.append(line_points(x0 + 1.6, y0, cx, cy + h * 0.34))
    segs.append(line_points(cx, cy + h * 0.34, x1 - 1.6, y0))
    return segs


def draw_neon_envelope(canvas: Canvas, cx: float, cy: float, w: float, h: float, phase: int) -> None:
    x0 = cx - w / 2
    core: dict[tuple[int, int], tuple[str, tuple[int, int, int]]] = {}
    for seg in neon_envelope_segments(cx, cy, w, h):
        prev = None
        for idx, (x, y) in enumerate(seg):
            col, row = int(round(x)), int(round(y))
            nxt = seg[idx + 1] if idx + 1 < len(seg) else None
            if prev is not None:
                ch = char_for(x - prev[0], (y - prev[1]) * 2.2)
            elif nxt is not None:
                ch = char_for(nxt[0] - x, (nxt[1] - y) * 2.2)
            else:
                ch = "·"
            t = max(0.0, min(1.0, (x - x0) / w))
            neon = mix(NEON_PINK, NEON_CYAN, t)
            shimmer = (x / w + phase * 0.04) % 1
            heat = max(0.0, 1 - abs(shimmer - 0.5) * 5)
            core_col = mix(neon, (255, 255, 255), 0.35 + 0.35 * heat)
            if (col, row) not in core:
                core[(col, row)] = (ch, core_col)
            prev = (x, y)
    for (col, row), (ch, colr) in core.items():
        canvas.put(col, row, ch, colr, True)
    for (col, row), (ch, colr) in list(core.items()):
        halo = (int(colr[0] * 0.30), int(colr[1] * 0.30), int(colr[2] * 0.30))
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            key = (col + dx, row + dy)
            if key not in core:
                canvas.put(key[0], key[1], "·", halo)
                core[key] = ("·", halo)


def draw_radar_rings(canvas: Canvas, f: int, cx: float, cy: float, max_r: float) -> None:
    for k in range(3):
        r = (f * 1.4 + k * (max_r / 3)) % max_r
        if r < 1.5:
            continue
        fade = 1 - r / max_r
        base = (0.25 + 0.75 * fade)
        col = (int(VIOLET[0] * base), int(VIOLET[1] * base), int(VIOLET[2] * base))
        for deg in range(0, 360, 4):
            rad = math.radians(deg)
            x = int(cx + r * math.cos(rad))
            y = int(cy + r * math.sin(rad) * 0.5)
            canvas.put(x, y, "·", col)


def rail_lines(f: int, total: int, width: int) -> list[str]:
    cur = min(len(STAGES) - 1, f // 5)
    parts = []
    for i, (name, _) in enumerate(STAGES):
        if i:
            parts.append(rgb(*DIM, " -- "))
        dot = "●" if i <= cur else "○"
        col = GREEN if i < cur else (NEON_CYAN if i == cur else DIM)
        parts.append(rgb(*col, f"{dot} {name}", bold=i <= cur))
    lines = [center_ansi("".join(parts), width)]
    pct = min(100, int(f / max(1, total - 1) * 100))
    bar = int(20 * pct / 100)
    bar_line = rgb(*MUTED, "PROGRESS [") + rgb(54, 235, 225, "━" * bar) + rgb(75, 95, 122, "━" * (20 - bar)) + rgb(*MUTED, f"] {pct:3d}%")
    lines.append(center_ansi(bar_line, width))
    lines.append("")
    lines.append(center_ansi(rgb(*AMBER, "◆") + rgb(171, 184, 202, " PREVIEW MODE  •  NO EMAILS SENT"), width))
    lines.append(center_ansi(rgb(*DIM, "BYTEROUTINE  /  PYTHON CLI"), width))
    return lines


def frame_lines(lines: list[str], width: int) -> list[str]:
    inner = width - 4
    result = ["╭" + "─" * inner + "╮"]
    for line in lines:
        result.append("│ " + line + " " * max(0, inner - len(strip_ansi(line)) - 1) + "│")
    result.append("╰" + "─" * inner + "╯")
    return result


def fit_width(term_width: int) -> int:
    return max(52, min(76, term_width))


def build_screen(width: int, phase: int, step: int, total: int) -> str:
    w = fit_width(width)
    cx, cy = w // 2, 8
    env_w = min(26, w - 24)
    canvas = Canvas(w, 15)
    draw_radar_rings(canvas, phase, cx, cy, min(26, cx - 8))
    draw_neon_envelope(canvas, cx, cy, env_w, 10, phase)
    lines = canvas.render_ansi()
    lines.append("")
    lines.append(center_ansi(gradient("smtp-py", (42, 235, 231), (172, 90, 255)), w))
    lines.append("")
    lines.extend(rail_lines(step, total, w))
    return "\n".join(frame_lines(lines, w))


def header_ansi(width: int) -> str:
    w = max(40, min(48, width))
    cx = w // 2
    canvas = Canvas(w, 12)
    draw_neon_envelope(canvas, cx, 6, min(26, w - 10), 10, 4)
    lines = canvas.render_ansi()
    lines.append(center_ansi(gradient("smtp-py", (42, 235, 231), (172, 90, 255)), w))
    lines.append(center_ansi(rgb(*DIM, "by ByteRoutine"), w))
    return "\n".join(lines)


def _banner_enabled() -> bool:
    return os.getenv("SMTPY_NO_BANNER") != "1" and sys.stdout.isatty()


def play_banner(speed: float = 1.0, loops: int = 1) -> None:
    if not _banner_enabled():
        return
    try:
        from rich.console import Console
        from rich.live import Live
        from rich.text import Text
    except ImportError:
        return
    console = Console()
    width = fit_width(console.width)
    total = max(1, loops) * 15
    try:
        with Live(console=console, auto_refresh=False, transient=True) as live:
            for step in range(total):
                frame = Text.from_ansi(build_screen(width, step, step % 15, 15))
                live.update(frame, refresh=True)
                time.sleep(max(0.03, 0.09 / max(0.1, speed)))
    except KeyboardInterrupt:
        pass


def animate_header(console, width: int, duration: float = 0.7) -> None:
    if not _banner_enabled():
        return
    try:
        from rich.live import Live
        from rich.text import Text
    except ImportError:
        return
    w = max(40, min(48, width))
    cx = w // 2
    frames = max(1, int(duration / 0.09))
    try:
        with Live(console=console, auto_refresh=False, transient=True) as live:
            for f in range(frames):
                canvas = Canvas(w, 12)
                draw_neon_envelope(canvas, cx, 6, min(26, w - 10), 10, f * 3)
                lines = canvas.render_ansi()
                lines.append(center_ansi(gradient("smtp-py", (42, 235, 231), (172, 90, 255)), w))
                lines.append(center_ansi(rgb(*DIM, "by ByteRoutine"), w))
                live.update(Text.from_ansi("\n".join(lines)), refresh=True)
                time.sleep(0.09)
    except KeyboardInterrupt:
        pass
