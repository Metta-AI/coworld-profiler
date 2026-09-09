"""Hand-built SVG charts for the hosted timing report, following the dataviz mark specs.

    uv run python tools/report_charts.py <out_dir>

Specs applied: bars at most 24px thick with a 4px rounded data end and a
square baseline end; 2px surface gaps between touching marks; hairline solid
gridlines; text in ink tokens, never in the series color; a legend for two or
more series; selective direct labels; a single axis per chart. Palette
validated with the dataviz validator: accent #b3542a and blue #2a78d6 on the
#faf8f4 paper surface.
"""

from __future__ import annotations

import sys
from pathlib import Path

INK = "#1f1b16"
MUTED = "#6b6259"
LINE = "#dcd6cc"
PAPER = "#faf8f4"
ACCENT = "#b3542a"
BLUE = "#2a78d6"
GRAY = "#b8b0a4"
FONT = "font-family='Inter, ui-sans-serif, system-ui, sans-serif'"


def text(x: float, y: float, s: str, *, size: int = 12, anchor: str = "start", color: str = INK, weight: str = "normal") -> str:
    return f"<text x='{x:.1f}' y='{y:.1f}' font-size='{size}' text-anchor='{anchor}' fill='{color}' font-weight='{weight}' {FONT}>{s}</text>"


def hbar(x: float, y: float, w: float, h: float, color: str) -> str:
    """Horizontal bar: square at the baseline (left), 4px rounded at the data end (right)."""
    r = min(4, w / 2)
    if w <= 0:
        return ""
    return f"<path d='M{x:.1f},{y:.1f} h{w - r:.1f} a{r},{r} 0 0 1 {r},{r} v{h - 2 * r:.1f} a{r},{r} 0 0 1 -{r},{r} h-{w - r:.1f} z' fill='{color}'/>"


def svg_open(w: int, h: int, title: str) -> str:
    return f"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 {w} {h}' width='{w}' height='{h}' role='img' aria-label='{title}'><rect width='{w}' height='{h}' fill='{PAPER}'/>"


def legend(x: float, y: float, items: list[tuple[str, str]]) -> str:
    out = []
    for i, (color, label) in enumerate(items):
        lx = x + i * 260
        out.append(f"<rect x='{lx}' y='{y - 9}' width='12' height='12' rx='2' fill='{color}'/>")
        out.append(text(lx + 18, y + 1, label, size=12, color=MUTED))
    return "".join(out)


def payload_chart(rows: list[tuple[str, float, float]]) -> str:
    """Horizontal grouped bars on a log axis: round trip and residual per observation size.

    A log axis is the honest form when the values span three orders of
    magnitude; every bar carries its value so the scale never has to be read
    off the grid.
    """
    import math

    W, H = 760, 340
    left, right, top = 150, 90, 56
    band = 52
    xmin, xmax = math.log10(0.3), math.log10(1000)

    def sx(v: float) -> float:
        return left + (math.log10(v) - xmin) / (xmax - xmin) * (W - left - right)

    out = [svg_open(W, H, "Round trip and transport residual by observation size")]
    out.append(text(left, 22, "Round trip and transport residual by observation size", size=14, weight="600"))
    out.append(text(left, 40, "2 slots, 20 ms tick, medians of 8 hosted episodes, first sweep pass, milliseconds on a log scale", size=11, color=MUTED))
    for tick in (1, 10, 100, 1000):
        x = sx(tick)
        out.append(f"<line x1='{x:.1f}' y1='{top}' x2='{x:.1f}' y2='{top + band * len(rows)}' stroke='{LINE}' stroke-width='1'/>")
        out.append(text(x, top + band * len(rows) + 16, f"{tick:,}", size=11, anchor="middle", color=MUTED))
    out.append(text(sx(30), top + band * len(rows) + 32, "milliseconds", size=11, anchor="middle", color=MUTED))
    for i, (label, rtt, resid) in enumerate(rows):
        y = top + i * band + 6
        out.append(text(left - 10, y + 18, label, size=12, anchor="end"))
        out.append(hbar(sx(0.3), y, sx(rtt) - sx(0.3), 18, ACCENT))
        out.append(text(sx(rtt) + 6, y + 13, f"{rtt:,.2f}" if rtt < 10 else f"{rtt:,.0f}", size=11, color=INK))
        out.append(hbar(sx(0.3), y + 20, sx(resid) - sx(0.3), 18, BLUE))
        out.append(text(sx(resid) + 6, y + 33, f"{resid:,.2f}" if resid < 10 else f"{resid:,.0f}", size=11, color=INK))
    out.append(legend(left, H - 8, [(ACCENT, "round trip (game send to reply)"), (BLUE, "transport residual")]))
    out.append("</svg>")
    return "".join(out)


def stacked_timeline(segments: list[tuple[str, float, str]], inside: list[tuple[str, float]], total_label: str) -> str:
    """One horizontal stacked bar of the worker's phases (median seconds) with the inside-view marks beneath it."""
    W, H = 760, 300
    left, right, top = 40, 40, 70
    total = sum(v for _, v, _ in segments)
    scale = (W - left - right) / total
    out = [svg_open(W, H, "A median hosted episode as the worker sees it")]
    out.append(text(left, 22, "A median hosted episode, outside view and inside view", size=14, weight="600"))
    out.append(text(left, 40, f"Worker phases from Datadog spans, seconds, with the game's own stamps as marks. {total_label}", size=11, color=MUTED))
    x = left
    for i, (label, value, color) in enumerate(segments):
        w = value * scale
        gap = 2 if i < len(segments) - 1 else 0
        out.append(f"<rect x='{x:.1f}' y='{top}' width='{max(0, w - gap):.1f}' height='22' fill='{color}'/>")
        if w > 80:
            out.append(text(x + w / 2, top + 15, f"{label} {value:.1f} s", size=11, anchor="middle", color=PAPER if color in (ACCENT, BLUE, INK) else INK))
        x += w
    out.append(f"<line x1='{left}' y1='{top + 30}' x2='{W - right}' y2='{top + 30}' stroke='{LINE}'/>")
    # ticks every 30 s
    for t in range(0, int(total) + 1, 30):
        tx = left + t * scale
        out.append(f"<line x1='{tx:.1f}' y1='{top + 30}' x2='{tx:.1f}' y2='{top + 35}' stroke='{MUTED}'/>")
        out.append(text(tx, top + 48, f"{t} s", size=10, anchor="middle", color=MUTED))
    # legend for small segments
    ly = top + 75
    for i, (label, value, color) in enumerate(segments):
        col, row = i % 3, i // 3
        lx = left + col * 230
        out.append(f"<rect x='{lx}' y='{ly + row * 20 - 9}' width='12' height='12' rx='2' fill='{color}'/>")
        out.append(text(lx + 18, ly + row * 20 + 1, f"{label}: {value:.2f} s" if value < 10 else f"{label}: {value:.0f} s", size=11, color=MUTED))
    # inside view marks
    iy = ly + 60
    out.append(text(left, iy, "Inside view, on the game's clock, relative to the worker phases:", size=11, color=MUTED))
    for j, (label, seconds) in enumerate(inside):
        mx = left + seconds * scale
        out.append(f"<circle cx='{mx:.1f}' cy='{iy + 18 + j * 22}' r='5' fill='{INK}' stroke='{PAPER}' stroke-width='2'/>")
        if mx > W * 0.7:
            out.append(text(mx - 10, iy + 22 + j * 22, label, size=11, anchor="end", color=INK))
        else:
            out.append(text(mx + 10, iy + 22 + j * 22, label, size=11, color=INK))
    out.append("</svg>")
    return "".join(out)


def dot_range(rows: list[tuple[str, float, float, float]], title: str, subtitle: str, unit: str, xmax: float) -> str:
    """Dot for the median with a thin range bar to the p99, one row per category."""
    W, H = 760, 60 + 40 * len(rows) + 72
    left, right, top = 150, 60, 56
    band = 40

    def sx(v: float) -> float:
        return left + v / xmax * (W - left - right)

    out = [svg_open(W, H, title)]
    out.append(text(left, 22, title, size=14, weight="600"))
    out.append(text(left, 40, subtitle, size=11, color=MUTED))
    step = 1 if xmax <= 8 else 5
    for tick in range(0, int(xmax) + 1, step):
        x = sx(tick)
        out.append(f"<line x1='{x:.1f}' y1='{top}' x2='{x:.1f}' y2='{top + band * len(rows)}' stroke='{LINE}'/>")
        out.append(text(x, top + band * len(rows) + 16, str(tick), size=11, anchor="middle", color=MUTED))
    out.append(text(sx(xmax / 2), top + band * len(rows) + 32, unit, size=11, anchor="middle", color=MUTED))
    for i, (label, p50, p99, mx) in enumerate(rows):
        y = top + i * band + band / 2
        out.append(text(left - 10, y + 4, label, size=12, anchor="end"))
        out.append(f"<line x1='{sx(p50):.1f}' y1='{y}' x2='{sx(min(p99, xmax)):.1f}' y2='{y}' stroke='{ACCENT}' stroke-width='2' stroke-linecap='round'/>")
        out.append(f"<circle cx='{sx(p50):.1f}' cy='{y}' r='5' fill='{ACCENT}' stroke='{PAPER}' stroke-width='2'/>")
        out.append(text(sx(min(p99, xmax)) + 8, y + 4, f"p99 {p99:.1f}", size=11, color=MUTED))
    out.append(legend(left, H - 8, [(ACCENT, "median (dot) to p99 (bar end)")]))
    out.append("</svg>")
    return "".join(out)


def dumbbell(rows: list[tuple[str, float, float]], title: str, subtitle: str, unit: str, xmax: float, left_label: str, right_label: str) -> str:
    """Two values per item connected by a line: the worker's outside measure and the game's inside measure."""
    W, H = 760, 60 + 40 * len(rows) + 72
    left, right, top = 170, 60, 56
    band = 40

    def sx(v: float) -> float:
        return left + v / xmax * (W - left - right)

    out = [svg_open(W, H, title)]
    out.append(text(left, 22, title, size=14, weight="600"))
    out.append(text(left, 40, subtitle, size=11, color=MUTED))
    step = 2 if xmax <= 12 else 5
    for tick in range(0, int(xmax) + 1, step):
        x = sx(tick)
        out.append(f"<line x1='{x:.1f}' y1='{top}' x2='{x:.1f}' y2='{top + band * len(rows)}' stroke='{LINE}'/>")
        out.append(text(x, top + band * len(rows) + 16, str(tick), size=11, anchor="middle", color=MUTED))
    out.append(text(sx(xmax / 2), top + band * len(rows) + 32, unit, size=11, anchor="middle", color=MUTED))
    for i, (label, a, b) in enumerate(rows):
        y = top + i * band + band / 2
        out.append(text(left - 10, y + 4, label, size=12, anchor="end"))
        out.append(f"<line x1='{sx(a):.1f}' y1='{y}' x2='{sx(b):.1f}' y2='{y}' stroke='{GRAY}' stroke-width='2'/>")
        out.append(f"<circle cx='{sx(a):.1f}' cy='{y}' r='5' fill='{BLUE}' stroke='{PAPER}' stroke-width='2'/>")
        out.append(f"<circle cx='{sx(b):.1f}' cy='{y}' r='5' fill='{ACCENT}' stroke='{PAPER}' stroke-width='2'/>")
        out.append(text(sx(max(a, b)) + 9, y + 4, f"{a:.1f} / {b:.1f}", size=11, color=MUTED))
    out.append(legend(left, H - 8, [(BLUE, left_label), (ACCENT, right_label)]))
    out.append("</svg>")
    return "".join(out)


def strip_plot(groups: list[tuple[str, list[float]]], title: str, subtitle: str, unit: str, xmax: float) -> str:
    """One row per group, one dot per observation, so a two-moded distribution shows as two clusters."""
    W, H = 760, 60 + 44 * len(groups) + 60
    left, right, top = 150, 40, 56
    band = 44

    def sx(v: float) -> float:
        return left + v / xmax * (W - left - right)

    out = [svg_open(W, H, title)]
    out.append(text(left, 22, title, size=14, weight="600"))
    out.append(text(left, 40, subtitle, size=11, color=MUTED))
    for tick in range(0, int(xmax) + 1, 2):
        x = sx(tick)
        out.append(f"<line x1='{x:.1f}' y1='{top}' x2='{x:.1f}' y2='{top + band * len(groups)}' stroke='{LINE}'/>")
        out.append(text(x, top + band * len(groups) + 16, str(tick), size=11, anchor="middle", color=MUTED))
    out.append(text(sx(xmax / 2), top + band * len(groups) + 32, unit, size=11, anchor="middle", color=MUTED))
    for i, (label, values) in enumerate(groups):
        y = top + i * band + band / 2
        out.append(text(left - 10, y + 4, label, size=12, anchor="end"))
        # small vertical jitter so coincident dots stay countable
        for j, v in enumerate(sorted(values)):
            jy = y + ((j % 3) - 1) * 6
            out.append(f"<circle cx='{sx(min(v, xmax)):.1f}' cy='{jy:.1f}' r='4.5' fill='{ACCENT}' fill-opacity='0.85' stroke='{PAPER}' stroke-width='1.5'/>")
    out.append("</svg>")
    return "".join(out)


if __name__ == "__main__":
    out_dir = Path(sys.argv[1])
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "chart-payload.svg").write_text(payload_chart([("1 KiB", 1.24, 0.77), ("16 KiB", 2.29, 0.92), ("256 KiB", 9.98, 0.98), ("2 MiB", 263.1, 221.7)]))
    print("wrote", out_dir)
