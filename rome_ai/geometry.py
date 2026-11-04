from __future__ import annotations

import math
from collections.abc import Iterable

from .protocol import Unit


def dist(a: Unit, b: Unit) -> float:
    return math.hypot(a.x - b.x, a.y - b.y)


def centroid(units: Iterable[Unit]) -> tuple[float, float]:
    us = list(units)
    if not us:
        return (0.0, 0.0)
    return (sum(u.x for u in us) / len(us), sum(u.y for u in us) / len(us))


def nearest(u: Unit, others: Iterable[Unit]) -> Unit | None:
    return min(others, key=lambda o: dist(u, o), default=None)


def alive(units: Iterable[Unit]) -> list[Unit]:
    return [u for u in units if u.action not in ("dead", "routing") and u.soldiers > 0]


def unit_vec(dx: float, dy: float) -> tuple[float, float]:
    n = math.hypot(dx, dy) or 1.0
    return dx / n, dy / n


def bearing(dx: float, dy: float) -> int:
    """Direction in whole degrees: 0 = north (+y), 90 = east, clockwise - the same convention as facing_deg."""
    return round(math.degrees(math.atan2(dx, dy))) % 360


def compass(dx: float, dy: float) -> str:
    """8-way compass word for a direction (y = north)."""
    if math.hypot(dx, dy) < 1e-6:
        return "here"
    ang = (math.degrees(math.atan2(dx, dy)) + 360) % 360
    return ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][int((ang + 22.5) // 45) % 8]


SURROUNDED_M = 60.0  # enemies this close on all sides: the unit has no way out


def surrounded(u: Unit, foes: Iterable[Unit], radius: float = SURROUNDED_M) -> bool:
    """Fighting enemies within `radius` on all sides: no free half-circle to get away through."""
    angs = sorted((math.degrees(math.atan2(f.x - u.x, f.y - u.y)) + 360) % 360
                  for f in foes if f.category != "siege" and f.action not in ("dead", "routing") and f.soldiers > 0
                  and dist(u, f) < radius)
    if len(angs) < 2:
        return False
    gaps = [b - a for a, b in zip(angs, angs[1:])] + [angs[0] + 360 - angs[-1]]
    return max(gaps) < 180
