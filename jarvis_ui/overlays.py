"""Shared responsive geometry for non-interactive HUD overlays."""
from dataclasses import dataclass


@dataclass(frozen=True)
class OverlayRect:
    x: float
    y: float
    width: float
    height: float
    docked: bool


def overlay_rect(width, height, kind='answer'):
    docked = width >= 1000 and kind != 'help'
    panel_width = min(520 if kind != 'help' else 640, width - 48)
    if docked:
        panel_width = min(panel_width, width * .44)
    panel_height = min(520, height - 246)
    x = width - panel_width - 32 if docked else (width - panel_width) / 2
    y = 100 + max(0, (height - 246 - panel_height) / 2)
    return OverlayRect(x, y, panel_width, panel_height, docked)


def visible_page(lines, offset, available_height, line_height=23):
    count = max(1, int(available_height // line_height))
    start = max(0, min(int(offset), (max(0, len(lines) - 1) // count) * count))
    return start, count, lines[start:start + count]
