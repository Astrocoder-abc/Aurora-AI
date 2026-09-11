"""Stable project paths, independent of the launching working directory."""
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
