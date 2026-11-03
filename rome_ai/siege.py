"""Battle buildings (walls, gates, towers) for siege battles.

The hook sends every BUILDING_BATTLE once per battle with its raw bytes, because the offset of the
building's position inside the object is not known from static analysis. We find it here by
self-calibration: walls, gates and towers stand on blocked cells of the PHYSICAL_GRID, so the right
offset is the one whose (x, y) values land on obstacles for most buildings.
"""

from __future__ import annotations

import logging
import math
import struct

from .protocol import TerrainMasks

log = logging.getLogger(__name__)

KIND = {0: "building", 1: "major", 2: "wall", 3: "tower", 4: "gate"}


class Building:
    __slots__ = ("id", "kind", "x", "y", "damage", "max_hp", "flag", "open", "owner")

    def __init__(self, id: int, kind: int, damage: int, max_hp: int, flag: int) -> None:
        self.id, self.kind, self.damage, self.max_hp, self.flag = id, kind, damage, max_hp, flag
        self.x = self.y = None
        self.open = -1  # gates: 1 open now, 0 shut, -1 unknown
        self.owner = -1  # gates: alliance that controls (can open) it, -1 unknown / nobody

    @property
    def health(self) -> int:
        # The field read at +0x68 ("damage") is the hit points LEFT: every building starts the battle with
        # damage == max_hp. Reading it as damage taken showed every gate and tower as 0% BROKEN.
        return 100 if not self.max_hp else max(0, min(100, self.damage * 100 // self.max_hp))

    @property
    def label(self) -> str:
        return {2: "W", 3: "T", 4: "G"}.get(self.kind, "B") + str(self.id)


def _candidates(raw: bytes):
    """(offset, x, y) for float pairs that look like battle-map coordinates. y may follow x directly
    (VECTOR_2) or after a height value (VECTOR_3)."""
    for off in range(0, len(raw) - 12, 4):
        x = struct.unpack_from("<f", raw, off)[0]
        if not (math.isfinite(x) and 1.0 < abs(x) < 1000):
            continue
        for gap in (4, 8):
            y = struct.unpack_from("<f", raw, off + gap)[0]
            if math.isfinite(y) and 1.0 < abs(y) < 1000:
                yield (off, gap), x, y


def locate(items: list[dict], masks: TerrainMasks | None) -> tuple[dict[int, Building], tuple | None]:
    """Decode buildings; returns ({id: Building}, how positions were obtained or None).
    The hook now reads the position itself (x +0x4c, y +0x54); raw-byte calibration is the fallback."""
    buildings = {it["id"]: Building(it["id"], it["kind"], it["damage"], it["max_hp"], it["flag74"]) for it in items}
    if items and all("x" in it for it in items):
        for it in items:
            buildings[it["id"]].x, buildings[it["id"]].y = it["x"], it["y"]
        return buildings, ("hook", 0x4C)
    if masks is None:
        return buildings, None
    from .terrain import _cell_rc, decode_masks

    obst, _ = decode_masks(masks)

    def on_obstacle(x: float, y: float) -> bool:
        r, c = _cell_rc(masks, x, y)
        return bool(obst[max(0, r - 2) : r + 3, max(0, c - 2) : c + 3].any())

    raws = {it["id"]: bytes.fromhex(it["raw"]) for it in items if it["kind"] in (2, 3, 4)}
    if len(raws) < 3:
        return buildings, None
    scores: dict[tuple, int] = {}
    for raw in raws.values():
        seen = set()
        for key, x, y in _candidates(raw):
            if key not in seen and on_obstacle(x, y):
                scores[key] = scores.get(key, 0) + 1
                seen.add(key)
    if not scores:
        return buildings, None
    best, hits = max(scores.items(), key=lambda kv: kv[1])
    if hits < 0.6 * len(raws):
        log.warning("building positions: no reliable offset (best %s hits %d/%d)", best, hits, len(raws))
        return buildings, None
    off, gap = best
    for it in items:
        raw = bytes.fromhex(it["raw"])
        x, y = struct.unpack_from("<f", raw, off)[0], struct.unpack_from("<f", raw, off + gap)[0]
        if math.isfinite(x) and math.isfinite(y) and abs(x) < 1100 and abs(y) < 1100:
            buildings[it["id"]].x, buildings[it["id"]].y = x, y
    log.info("building positions at offset %#x (+%d): %d/%d walls/gates/towers on obstacles", off, gap, hits, len(raws))
    return buildings, best


def describe_buildings(buildings: dict[int, Building], near: tuple[float, float] | None) -> str:
    """Gates, towers and wall sections the general can attack, closest to `near` first."""
    def key(b: Building):
        return math.hypot(b.x - near[0], b.y - near[1]) if near else 0

    out = []
    for kind, title, n in ((4, "GATES", 8), (3, "TOWERS", 10), (2, "WALL SECTIONS", 10)):
        bs = sorted((b for b in buildings.values() if b.kind == kind and b.x is not None), key=key)[:n]
        if not bs:
            continue
        out.append(f"  {title}: " + "; ".join(
            f"{b.label} ({b.x:.0f},{b.y:.0f}) {b.health}%" + (" BROKEN" if b.health == 0 else "") for b in bs))
    if not out:
        return ""
    return "TOWN DEFENCES (attack them with task=attack_building, target_building=<number>):\n" + "\n".join(out)
