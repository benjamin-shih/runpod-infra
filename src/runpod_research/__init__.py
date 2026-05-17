"""Reusable RunPod lifecycle controller for research sweeps."""

from __future__ import annotations

from pathlib import Path

__version__ = "0.1.0"


def data_path(name: str) -> Path:
    """Return a packaged data file path."""

    return Path(__file__).with_name("data") / name
