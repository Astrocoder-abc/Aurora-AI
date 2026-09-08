"""Dependency-free launch identity and conservative desktop window sizing."""
from pathlib import Path
import subprocess
import sys

from .paths import PROJECT_ROOT

UI_BUILD = 'amber-windowed-v2'


def window_size(desktop, requested=(960, 640)):
    """Leave room for window chrome/taskbar; never request a screen-mode change."""
    return tuple(max(1, min(want, max(1, available - margin)))
                 for available, want, margin in zip(desktop, requested, (100, 140)))


def launch_report():
    lines = [f'Aurora UI build: {UI_BUILD}', f'Project: {PROJECT_ROOT}',
             f'Python: {sys.executable}', f'UI file: {PROJECT_ROOT / "jarvis_ui" / "hologram.py"}']
    for label, args in [('Branch', ['branch', '--show-current']),
                        ('Commit', ['rev-parse', '--short', 'HEAD'])]:
        try:
            result = subprocess.run(['git', '-C', str(PROJECT_ROOT), *args],
                                    capture_output=True, text=True, timeout=3)
            value = result.stdout.strip() if result.returncode == 0 else 'unavailable (ZIP/non-Git copy)'
        except (OSError, subprocess.TimeoutExpired):
            value = 'unavailable (Git not installed)'
        lines.append(f'{label}: {value}')
    return '\n'.join(lines)
