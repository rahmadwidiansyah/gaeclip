"""Simple metrics endpoint exposing Prometheus-style counters.

Useful for debugging and basic observability. Operators can scrape
`/metrics` with Prometheus or similar tooling.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from typing import Any

logger = logging.getLogger("gaeclip.metrics")

_counters: dict[str, int] = defaultdict(int)
_gauges: dict[str, float] = {}


def inc(name: str, amount: int = 1) -> None:
    _counters[name] += amount


def set_gauge(name: str, value: float) -> None:
    _gauges[name] = value


def render() -> str:
    lines: list[str] = []
    for name, value in sorted(_counters.items()):
        lines.append(f"gaeclip_{name} {value}")
    for name, value in sorted(_gauges.items()):
        lines.append(f"gaeclip_{name} {value}")
    return "\n".join(lines) + "\n"


def reset() -> None:
    _counters.clear()
    _gauges.clear()
