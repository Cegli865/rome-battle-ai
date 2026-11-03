"""Battlefield terrain for the general: a small ASCII height map with units on it.

The hook samples the game's PHYSICAL_GRID once per battle (80 x 80 points, 25 m
apart, covering -1000..1000 m). Here we crop that to the area around the armies
and draw it so an LLM can see hills and where everyone stands.
"""

from __future__ import annotations

import math

from .geometry import alive
from .protocol import State, TerrainMasks, TerrainMsg as Terrain, Unit

MAX_COLS = 30


def height_at(t: Terrain, x: float, y: float) -> float:
    i = min(t.n - 1, max(0, int((x - t.origin) / t.step)))
    j = min(t.n - 1, max(0, int((y - t.origin) / t.step)))
    return t.heights_dm[j * t.n + i] / 10.0


def _letters(units: list[Unit], upper: bool) -> dict[int, str]:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if upper else "abcdefghijklmnopqrstuvwxyz"
    return {u.id: alphabet[k % 26] for k, u in enumerate(sorted(units, key=lambda u: u.id))}


FOREST_DENSITY = 0.035  # share of 2 m cells with a tree; open ground ~0, woods ~0.04-0.06


def describe_terrain(t: Terrain, ai_alliance: int, state: State, masks: TerrainMasks | None = None, show_units: bool = True) -> str:
    units = alive(state.units)
    if not units:
        return ""
    margin = 250.0
    x0 = max(t.origin, min(u.x for u in units) - margin)
    x1 = min(t.origin + t.n * t.step, max(u.x for u in units) + margin)
    y0 = max(t.origin, min(u.y for u in units) - margin)
    y1 = min(t.origin + t.n * t.step, max(u.y for u in units) + margin)
    cell = max(t.step, math.ceil(max(x1 - x0, y1 - y0) / MAX_COLS / t.step) * t.step)
    cols, rows = max(1, math.ceil((x1 - x0) / cell)), max(1, math.ceil((y1 - y0) / cell))

    hs = [[height_at(t, x0 + (c + 0.5) * cell, y0 + (r + 0.5) * cell) for c in range(cols)] for r in range(rows)]
    lo = min(min(row) for row in hs)
    hi = max(max(row) for row in hs)
    band = max(3.0, (hi - lo) / 9)

    grid = [[str(min(9, int((h - lo) / band))) for h in row] for row in hs]
    if masks is not None:
        obst, zone = decode_masks(masks)
        for r in range(rows):
            for c in range(cols):
                cx0, cy0 = x0 + c * cell, y0 + r * cell
                r0_, c0_ = _cell_rc(masks, cx0, cy0)
                r1_, c1_ = _cell_rc(masks, cx0 + cell, cy0 + cell)
                z = zone[max(0, r0_):r1_, max(0, c0_):c1_]
                if z.size and z.mean() > 0.5:
                    grid[r][c] = "H"
                elif forest_fraction(masks, cx0, cy0, cx0 + cell, cy0 + cell) >= FOREST_DENSITY:
                    grid[r][c] = "T"
    mine = [u for u in units if u.alliance == ai_alliance]
    theirs = [u for u in units if u.alliance != ai_alliance]
    my_l, their_l = _letters(mine, True), _letters(theirs, False)
    for u, ch in ([(u, my_l[u.id]) for u in mine] + [(u, their_l[u.id]) for u in theirs]) if show_units else []:
        c, r = int((u.x - x0) / cell), int((u.y - y0) / cell)
        if 0 <= c < cols and 0 <= r < rows:
            grid[r][c] = ch if (grid[r][c].isdigit() or grid[r][c] in "TH") else "*"

    out = [
        f"TERRAIN MAP: {cols}x{rows} cells of {cell:.0f} m, north at the top, x from {x0:.0f} to {x1:.0f}, "
        f"y from {y1:.0f} (top) to {y0:.0f} (bottom).",
        f"Digits = height band (0 lowest = {lo:.0f} m, each step {band:.0f} m, 9 highest = {hi:.0f} m). "
        + ("UPPERCASE = your units, lowercase = enemy, * = several units in one cell" if show_units else "Units: see the unit list")
        + (", T = woods, H = town (see TOWN MAP)." if masks is not None else "."),
    ]
    out += ["  " + "".join(grid[r]) for r in range(rows - 1, -1, -1)]
    if show_units:
        out.append("  yours: " + ", ".join(f"{my_l[u.id]}=#{u.id}" for u in sorted(mine, key=lambda u: u.id)))
        out.append("  enemy: " + ", ".join(f"{their_l[u.id]}=#{u.id}" for u in sorted(theirs, key=lambda u: u.id)))

    # Notable high ground: local maxima clearly above the surroundings.
    peaks = []
    for r in range(rows):
        for c in range(cols):
            h = hs[r][c]
            nb = [hs[rr][cc] for rr in range(max(0, r - 2), min(rows, r + 3)) for cc in range(max(0, c - 2), min(cols, c + 3))]
            if h >= max(nb) and h - min(nb) >= 6:
                peaks.append((h, x0 + (c + 0.5) * cell, y0 + (r + 0.5) * cell))
    peaks.sort(reverse=True)
    if peaks:
        out.append(
            "  high ground: "
            + "; ".join(f"({x:.0f},{y:.0f}) {h - lo:.0f} m above the lowest point" for h, x, y in peaks[:4])
        )
    for side, us in (("your", mine), ("enemy", theirs)) if show_units else []:
        if us:
            avg = sum(height_at(t, u.x, u.y) for u in us) / len(us)
            out.append(f"  {side} army stands at about {avg - lo:.0f} m above the lowest point")
    return "\n".join(out)


# --- obstacles / settlement (full-resolution masks) -------------------------------------------

_mask_cache: dict[int, tuple] = {}


def decode_masks(m: TerrainMasks):
    """-> (obstacles, settlement) as bool arrays [row = y, col = x] of m.cells x m.cells."""
    import base64

    import numpy as np

    key = id(m)
    if key not in _mask_cache:
        def bits(s: str):
            b = np.frombuffer(base64.b64decode(s), dtype=np.uint8)
            return np.unpackbits(b, bitorder="little")[: m.cells * m.cells].reshape(m.cells, m.cells).astype(bool)

        _mask_cache.clear()
        _mask_cache[key] = (bits(m.obstacles_b64), bits(m.settlement_b64))
    return _mask_cache[key]


def _cell_rc(m: TerrainMasks, x: float, y: float) -> tuple[int, int]:
    return int((y - m.origin) / m.cell_size), int((x - m.origin) / m.cell_size)


def town_bbox(m: TerrainMasks):
    """Bounding box (x0, y0, x1, y1) of the largest connected settlement area, or None."""
    from collections import deque

    _, zone = decode_masks(m)
    k = 5  # work on 10 m blocks
    n = m.cells // k
    blocks = zone[: n * k, : n * k].reshape(n, k, n, k).mean(axis=(1, 3)) > 0.5
    seen = [[False] * n for _ in range(n)]
    best: list[tuple[int, int]] = []
    for r in range(n):
        for c in range(n):
            if blocks[r, c] and not seen[r][c]:
                comp, q = [], deque([(r, c)])
                seen[r][c] = True
                while q:
                    a, b = q.popleft()
                    comp.append((a, b))
                    for da, db in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                        aa, bb = a + da, b + db
                        if 0 <= aa < n and 0 <= bb < n and blocks[aa, bb] and not seen[aa][bb]:
                            seen[aa][bb] = True
                            q.append((aa, bb))
                if len(comp) > len(best):
                    best = comp
    if len(best) < 50:  # < 5000 m2: not a town
        return None
    cs, o = m.cell_size * k, m.origin
    rs, cs_ = [p[0] for p in best], [p[1] for p in best]
    return o + min(cs_) * cs, o + min(rs) * cs, o + (max(cs_) + 1) * cs, o + (max(rs) + 1) * cs


def free_width(obst):
    """Approximate free-space width in cells: 2 * (4-neighbour erosion depth) + 1, capped."""
    import numpy as np

    free = ~obst
    depth = np.zeros(obst.shape, dtype=np.int16)
    cur = free.copy()
    for _ in range(12):
        nxt = cur.copy()
        nxt[1:, :] &= cur[:-1, :]
        nxt[:-1, :] &= cur[1:, :]
        nxt[:, 1:] &= cur[:, :-1]
        nxt[:, :-1] &= cur[:, 1:]
        depth += nxt
        cur = nxt
    return np.where(free, depth * 2 + 1, 0)


NARROW_M = 16.0  # streets at most this wide count as "narrow" (good for a phalanx, hard to outflank)


def pick_chokepoints(narrow_pts, mine: list[Unit], theirs: list[Unit], n: int) -> list[tuple[str, float, float, float]]:
    """Narrow street points on the enemy's way to us, at least 40 m apart, labelled S1..Sn."""
    if not narrow_pts or not mine:
        return []
    cx, cy = centroid_xy(mine)
    if theirs:
        ex, ey = centroid_xy(theirs)

        def score(p):
            dm, de = math.hypot(p[0] - cx, p[1] - cy), math.hypot(p[0] - ex, p[1] - ey)
            return dm + de + (0 if dm <= de else 1000)
    else:
        def score(p):
            return math.hypot(p[0] - cx, p[1] - cy)
    picked: list[tuple[float, float, float]] = []
    for x, y, w in sorted(narrow_pts, key=score):
        if all(math.hypot(x - px, y - py) > 40 for px, py, _ in picked):
            picked.append((x, y, w))
        if len(picked) == n:
            break
    return [(f"S{i + 1}", x, y, w) for i, (x, y, w) in enumerate(picked)]


def describe_town(
    m: TerrainMasks, ai_alliance: int, state: State, res: float = 10.0, out_chokepoints: list | None = None, plaza_xy=None,
    show_units: bool = True,
) -> str:
    import numpy as np

    bbox = town_bbox(m)
    if bbox is None:
        return ""
    obst, zone = decode_masks(m)
    x0, y0, x1, y1 = bbox
    x0, y0, x1, y1 = x0 - 30, y0 - 30, x1 + 30, y1 + 30
    k = max(1, int(round(res / m.cell_size)))
    while (x1 - x0) / (k * m.cell_size) > 64:
        k += 1
    res = k * m.cell_size
    r0, c0 = _cell_rc(m, x0, y0)
    r1, c1 = _cell_rc(m, x1, y1)
    r0, c0, r1, c1 = max(0, r0), max(0, c0), min(m.cells, r1), min(m.cells, c1)
    ob, zn = obst[r0:r1, c0:c1], zone[r0:r1, c0:c1]
    width_m = free_width(ob) * m.cell_size
    rows, cols = (r1 - r0) // k, (c1 - c0) // k

    grid, narrow_pts = [], []
    for r in range(rows):
        line = []
        for c in range(cols):
            sl = (slice(r * k, (r + 1) * k), slice(c * k, (c + 1) * k))
            frac = ob[sl].mean()
            if frac > 0.5:
                line.append("#")
            elif frac >= 0.15:
                line.append("=")  # wall line, fence, edge of a building
            elif zn[sl].mean() < 0.5:
                line.append(",")
            else:
                w = width_m[sl][~ob[sl]].max() if (~ob[sl]).any() else 0
                narrow = w <= NARROW_M
                line.append(":" if narrow else ".")
                if narrow:
                    narrow_pts.append((m.origin + (c0 + (c + 0.5) * k) * m.cell_size, m.origin + (r0 + (r + 0.5) * k) * m.cell_size, w))
        grid.append(line)

    units = alive(state.units)
    mine = [u for u in units if u.alliance == ai_alliance]
    theirs = [u for u in units if u.alliance != ai_alliance]
    my_l, their_l = _letters(mine, True), _letters(theirs, False)
    gx0, gy0 = m.origin + c0 * m.cell_size, m.origin + r0 * m.cell_size
    n_spear = sum(1 for u in mine if u.cls == "spearmen")
    chokes = pick_chokepoints(narrow_pts, mine, theirs, min(9, max(6, n_spear + 2)))
    if out_chokepoints is not None:
        out_chokepoints[:] = chokes
    for label, x, y, _ in chokes:
        c, r = int((x - gx0) / res), int((y - gy0) / res)
        if 0 <= c < cols and 0 <= r < rows:
            grid[r][c] = label[1:]
    plaza = plaza_xy
    if plaza is not None:
        c, r = int((plaza[0] - gx0) / res), int((plaza[1] - gy0) / res)
        if 0 <= c < cols and 0 <= r < rows:
            grid[r][c] = "P"
    for u, ch in ([(u, my_l[u.id]) for u in mine] + [(u, their_l[u.id]) for u in theirs]) if show_units else []:
        c, r = int((u.x - gx0) / res), int((u.y - gy0) / res)
        if 0 <= c < cols and 0 <= r < rows:
            grid[r][c] = ch if (grid[r][c] in "#=,.:P" or grid[r][c].isdigit()) else "*"

    out = [
        f"TOWN MAP: {cols}x{rows} cells of {res:.0f} m, north at the top, x from {gx0:.0f} to {gx0 + cols * res:.0f}, "
        f"y from {gy0 + rows * res:.0f} (top) to {gy0:.0f} (bottom).",
        f"# = buildings (impassable), = = walls or other obstacles, . = street or square, "
        f": = NARROW street (<= {NARROW_M:.0f} m wide), , = outside the town, digits 1-9 = chokepoints S1-S9, "
        "P = town square."
        + (" Letters as on the terrain map." if show_units else ""),
    ]
    out += ["  " + "".join(grid[r]) for r in range(rows - 1, -1, -1)]
    if plaza is not None:
        out.append(
            f"  TOWN SQUARE at ({plaza[0]:.0f},{plaza[1]:.0f}): if the attacker holds it, the town falls and the battle is lost. "
            "The defender must always keep a unit there (task=hold, target_point on the square)."
        )
    if chokes:
        out.append(
            "  CHOKEPOINTS (narrow streets on the enemy's way to you): "
            + "; ".join(f"{label} at ({x:.0f},{y:.0f}) ~{w:.0f} m wide" for label, x, y, w in chokes)
        )
    return "\n".join(out)


def centroid_xy(units: list[Unit]) -> tuple[float, float]:
    return sum(u.x for u in units) / len(units), sum(u.y for u in units) / len(units)


def forest_fraction(m: TerrainMasks, x0: float, y0: float, x1: float, y1: float) -> float:
    """Fraction of obstacle cells outside the town in a rectangle (trees)."""
    obst, zone = decode_masks(m)
    r0, c0 = _cell_rc(m, x0, y0)
    r1, c1 = _cell_rc(m, x1, y1)
    r0, c0, r1, c1 = max(0, r0), max(0, c0), min(m.cells, max(r0 + 1, r1)), min(m.cells, max(c0 + 1, c1))
    o, z = obst[r0:r1, c0:c1], zone[r0:r1, c0:c1]
    out = ~z
    return float((o & out).sum()) / max(1, int(out.sum()))


# --- paths through towns -------------------------------------------------------------------------------

PATH_CELL = 2  # BFS on 4 m cells (2x2 grid cells)


def _coarse_free(m: TerrainMasks):
    """Free 4 m cells (no obstacle in the 2x2 block), cached per mask object."""
    key = ("free", id(m))
    if key not in _mask_cache:
        obst, _ = decode_masks(m)
        n = m.cells // PATH_CELL
        blocks = obst[: n * PATH_CELL, : n * PATH_CELL].reshape(n, PATH_CELL, n, PATH_CELL).any(axis=(1, 3))
        _mask_cache[key] = ~blocks
    return _mask_cache[key]


def _to_cell(m: TerrainMasks, x: float, y: float) -> tuple[int, int]:
    s = m.cell_size * PATH_CELL
    return int((y - m.origin) / s), int((x - m.origin) / s)


def _to_xy(m: TerrainMasks, r: int, c: int) -> tuple[float, float]:
    s = m.cell_size * PATH_CELL
    return m.origin + (c + 0.5) * s, m.origin + (r + 0.5) * s


def snap_free(m: TerrainMasks, x: float, y: float, max_m: float = 60.0) -> tuple[float, float]:
    """Nearest point not inside a building/wall (within max_m), else the point itself."""
    free = _coarse_free(m)
    r0, c0 = _to_cell(m, x, y)
    n = free.shape[0]
    rad = int(max_m / (m.cell_size * PATH_CELL))
    best = None
    for dr in range(-rad, rad + 1):
        for dc in range(-rad, rad + 1):
            r, c = r0 + dr, c0 + dc
            if 0 <= r < n and 0 <= c < n and free[r, c]:
                d = dr * dr + dc * dc
                if best is None or d < best[0]:
                    best = (d, r, c)
    return _to_xy(m, best[1], best[2]) if best else (x, y)


def street_path(m: TerrainMasks, start: tuple[float, float], goal: tuple[float, float], margin_m: float = 200.0):
    """Shortest walkable path (list of (x, y), goal first) between two points, via BFS on free 4 m cells.
    None if no path within the search box."""
    from collections import deque

    free = _coarse_free(m)
    n = free.shape[0]
    sx, sy = snap_free(m, *start)
    gx, gy = snap_free(m, *goal)
    s, g = _to_cell(m, sx, sy), _to_cell(m, gx, gy)
    pad = int(margin_m / (m.cell_size * PATH_CELL))
    r_lo, r_hi = max(0, min(s[0], g[0]) - pad), min(n - 1, max(s[0], g[0]) + pad)
    c_lo, c_hi = max(0, min(s[1], g[1]) - pad), min(n - 1, max(s[1], g[1]) + pad)
    prev = {g: None}
    q = deque([g])
    while q:  # search from the goal so the path comes out goal-first
        cur = q.popleft()
        if cur == s:
            break
        r, c = cur
        for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)):
            nr, nc = r + dr, c + dc
            if r_lo <= nr <= r_hi and c_lo <= nc <= c_hi and free[nr, nc] and (nr, nc) not in prev:
                if dr and dc and not (free[r, nc] and free[nr, c]):
                    continue  # no corner cutting through buildings
                prev[(nr, nc)] = cur
                q.append((nr, nc))
    end = s
    if s not in prev:
        # closed gate / unbroken walls: walk inside as far as possible, then straight to the start
        end = min(prev, key=lambda rc: (rc[0] - s[0]) ** 2 + (rc[1] - s[1]) ** 2)
    out, cur = [], end
    while cur is not None:
        out.append(_to_xy(m, *cur))
        cur = prev[cur]
    out.reverse()  # goal first
    if end != s:
        out.append((sx, sy))
    return out


def points_along(path: list[tuple[float, float]], step: float, count: int) -> list[tuple[float, float]]:
    """`count` points along `path` (goal first), `step` metres apart, starting at the goal."""
    pts, need, walked = [path[0]], step, 0.0
    for (x0, y0), (x1, y1) in zip(path, path[1:]):
        seg = math.hypot(x1 - x0, y1 - y0)
        while seg > 0 and walked + seg >= need and len(pts) < count:
            t = (need - walked) / seg
            pts.append((x0 + (x1 - x0) * t, y0 + (y1 - y0) * t))
            need += step
        walked += seg
    while len(pts) < count:
        pts.append(pts[-1])
    return pts


def alternative_paths(m: TerrainMasks, start: tuple[float, float], goal: tuple[float, float], k: int,
                      max_ratio: float = 2.0, keep_free_m: float = 40.0) -> list[list[tuple[float, float]]]:
    """Up to k street paths (goal first) to the same goal that use different streets: each next path avoids
    the cells of the previous ones (except near the goal, where all routes meet)."""
    first = street_path(m, start, goal)
    if not first:
        return []
    paths = [first]
    key = ("free", id(m))
    base = _coarse_free(m)
    length = lambda p: sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(p, p[1:]))
    try:
        blocked = base.copy()
        for _ in range(k - 1):
            for p in paths:
                for x, y in p:
                    if math.hypot(x - goal[0], y - goal[1]) < keep_free_m or math.hypot(x - start[0], y - start[1]) < 60:
                        continue  # routes may share the first and last streets
                    r, c = _to_cell(m, x, y)
                    blocked[max(0, r - 2) : r + 3, max(0, c - 2) : c + 3] = False  # 10-20 m wide corridor
            _mask_cache[key] = blocked
            alt = street_path(m, start, goal, margin_m=300.0)
            if not alt or length(alt) > max_ratio * length(first) or alt[-1] != first[-1] and len(alt) < 3:
                break
            # reject paths that only reach the goal by the straight-line fallback (blocked in)
            paths.append(alt)
    finally:
        _mask_cache[key] = base
    return paths


def approach_routes(m: TerrainMasks, start: tuple[float, float], goal: tuple[float, float], k: int = 3,
                    stage_m: float = 60.0, min_sep_deg: float = 70.0, max_ratio: float = 2.2):
    """Up to k routes that reach `goal` from DIFFERENT DIRECTIONS: staging points around the goal (every 30 deg,
    stage_m out, on a street connected to the goal), a path from the army to each, keeping only staging points at
    least min_sep_deg apart. Returns [(path goal-first, staging (x, y), bearing_deg)] shortest first."""
    import numpy as np

    cands = []
    for deg in range(0, 360, 30):
        rad = math.radians(deg)
        px, py = goal[0] + stage_m * math.sin(rad), goal[1] + stage_m * math.cos(rad)
        sx, sy = snap_free(m, px, py, max_m=25.0)
        if math.hypot(sx - px, sy - py) > 25:
            continue
        inner = street_path(m, (sx, sy), goal, margin_m=60.0)  # staging point really connected to the goal?
        if not inner or len(inner) * m.cell_size * PATH_CELL > stage_m * 2.5:
            continue
        outer = street_path(m, start, (sx, sy))
        if not outer:
            continue
        path = inner[:-1] + outer  # goal ... staging ... start
        length = sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(path, path[1:]))
        bearing = (math.degrees(math.atan2(sx - goal[0], sy - goal[1])) + 360) % 360
        cands.append((length, path, (sx, sy), bearing))
    if not cands:
        return []
    cands.sort(key=lambda c: c[0])
    best = cands[0][0]
    picked = []
    for length, path, stg, bearing in cands:
        if length > max_ratio * best:
            break
        if all(min(abs(bearing - b), 360 - abs(bearing - b)) >= min_sep_deg for _, _, b in picked):
            picked.append((path, stg, bearing))
        if len(picked) == k:
            break
    return picked


def plaza_exits(m: TerrainMasks, goal: tuple[float, float], r_in: float = 25.0, r_out: float = 60.0, min_cells: int = 3):
    """Streets leaving the town square: walk out from the square over free 4 m cells; in the ring r_in..r_out
    around it, every separate patch of street is one exit. Returns [(stage (x, y), bearing_deg)] sorted by bearing."""
    from collections import deque

    free = _coarse_free(m)
    n = free.shape[0]
    s = m.cell_size * PATH_CELL
    g = _to_cell(m, *snap_free(m, *goal))
    reach = {g}
    q = deque([g])
    while q:
        r, c = q.popleft()
        for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nr, nc = r + dr, c + dc
            if 0 <= nr < n and 0 <= nc < n and free[nr, nc] and (nr, nc) not in reach:
                x, y = _to_xy(m, nr, nc)
                if math.hypot(x - goal[0], y - goal[1]) <= r_out:
                    reach.add((nr, nc))
                    q.append((nr, nc))
    ring = {rc for rc in reach if math.hypot(*(a - b for a, b in zip(_to_xy(m, *rc), goal))) >= r_in}
    exits, seen = [], set()
    for start in ring:
        if start in seen:
            continue
        comp, q = [], deque([start])
        seen.add(start)
        while q:
            r, c = q.popleft()
            comp.append((r, c))
            for dr in (-1, 0, 1):
                for dc in (-1, 0, 1):
                    nb = (r + dr, c + dc)
                    if nb in ring and nb not in seen:
                        seen.add(nb)
                        q.append(nb)
        if len(comp) < min_cells:
            continue
        # stage point: the patch cell farthest out (where the street leaves the ring)
        far = max(comp, key=lambda rc: math.hypot(*(a - b for a, b in zip(_to_xy(m, *rc), goal))))
        x, y = _to_xy(m, *far)
        exits.append(((x, y), (math.degrees(math.atan2(x - goal[0], y - goal[1])) + 360) % 360))
    exits.sort(key=lambda e: e[1])
    return exits


def best_plaza_exits(m: TerrainMasks, goal: tuple[float, float]):
    """Open squares merge neighbouring streets in a narrow ring: try a few ring sizes, keep the one that
    separates the most exits (ties: the smaller ring, closer to the square)."""
    best = []
    for r_in, r_out in ((25.0, 60.0), (45.0, 85.0), (65.0, 110.0)):
        ex = plaza_exits(m, goal, r_in, r_out)
        if len(ex) > len(best):
            best = ex
    return best


def exit_routes(m: TerrainMasks, start: tuple[float, float], goal: tuple[float, float], max_ratio: float = 2.5):
    """For every exit of the square: the street path from the army to it and on to the square (goal first).
    Returns [(path, stage, bearing, length)] sorted by bearing; exits much farther than the nearest are dropped."""
    out = []
    for stage, bearing in best_plaza_exits(m, goal):
        inner = street_path(m, stage, goal, margin_m=80.0)
        outer = street_path(m, start, stage)
        if not inner or not outer:
            continue
        path = inner[:-1] + outer
        length = sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(path, path[1:]))
        out.append((path, stage, bearing, length))
    if not out:
        return []
    best = min(o[3] for o in out)
    return [o for o in out if o[3] <= max_ratio * best]
