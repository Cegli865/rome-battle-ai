"""Town graph for the general: the game's own street graph, the walk along stone walls, and the ways in.

Sources (all read from the game by the hook):
  streets   BATTLE_MAP_STREETS - nodes and links the game's pathfinder uses in towns
  walls     BATTLE_MAP_WALLS - wall-walk platforms (segments on top of stone walls) and their links;
            a link to platform -1 is a way down to the ground (stairs in a tower or a gatehouse)
  buildings walls sections (kind 2), towers (3), gates (4) with hit points left

Wall type: stone walls have wall-walk platforms; wooden walls (palisades) have wall buildings but no
platforms; a town without wall buildings has no walls.
"""

from __future__ import annotations

import heapq
import math
import struct
from collections import defaultdict
from dataclasses import dataclass, field

# march speeds (m/s) measured in battle logs: walking / running
SPEED = {"infantry": (2.2, 4.5), "missile": (2.3, 4.5), "cavalry": (4.0, 9.0), "general": (4.0, 9.0),
         "siege": (1.6, 1.6), "other": (2.2, 4.5)}
TOWER_SNAP_M = 20.0
TOWER_RANGE_M = 150.0  # rough reach of wall towers (an estimate, not read from the game)  # a wall vertex this close to a tower/gate building belongs to it
DESCENT_TO_STREET_M = 150.0  # a way down / entry joins the nearest street node within this distance


@dataclass
class Node:
    id: str
    kind: str  # plaza, street, tower, gate, wall (wall section, an entry point for wooden walls)
    x: float
    y: float
    level: str = "ground"  # ground or wall (on top of the wall)
    info: str = ""
    building: int | None = None  # BUILDING_BATTLE id (gates, towers, wall sections) for attack_building


@dataclass
class Edge:
    a: str
    b: str
    kind: str  # street, wall (walk on top of the wall), stairs (wall <-> ground), entry (outside -> inside)
    length: float
    width: float | None = None
    info: str = ""


@dataclass
class TownGraph:
    walls: str  # none, wooden, stone
    nodes: dict[str, Node] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)
    entries: list[tuple[str, str, bool | None]] = field(default_factory=list)  # (node, how, open now / None)

    def add(self, n: Node) -> None:
        self.nodes[n.id] = n


def decode_walls(platforms: list[dict]):
    """walls_raw -> (segments [(x0, y0, x1, y1)] by platform index, ways down [(x, y)], links [(a, b)],
    doors [(x, y)]). A door is a link between two platforms with a non-zero value at +0x30 - where the wall
    walk passes through a wall tower (they stand at the WALL buildings with 10 hit points)."""
    f = lambda r, o: struct.unpack_from("<f", r, o)[0]  # noqa: E731
    i32 = lambda r, o: struct.unpack_from("<i", r, o)[0]  # noqa: E731
    segs, downs, links, doors = [], [], [], []
    for p in sorted(platforms, key=lambda p: p["i"]):
        r = bytes.fromhex(p["raw"])
        segs.append((f(r, 0xF0), f(r, 0xF4), f(r, 0xF8), f(r, 0xFC)))
        for link in p["links"]:
            lr = bytes.fromhex(link["raw"])
            if len(lr) < 0x28:
                continue
            other = i32(lr, 0x1C)
            if other == -1:
                downs.append((f(lr, 0x20), f(lr, 0x24)))
            else:
                links.append((p["i"], other))
                if abs(f(lr, 0x30)) > 0.5:
                    doors.append((f(lr, 0x20), f(lr, 0x24)))
    return segs, downs, links, doors


def _health(b: dict) -> int:
    return b["damage"] * 100 // b["max_hp"] if b.get("max_hp") else 100


def build(streets: list[dict], buildings: list[dict], wall_platforms: list[dict] | None,
          plaza: tuple[float, float]) -> TownGraph:
    walls_b = [b for b in buildings if b["kind"] == 2]
    towers = [b for b in buildings if b["kind"] == 3]
    gates = [b for b in buildings if b["kind"] == 4]
    kind = "stone" if wall_platforms else ("wooden" if walls_b or gates else "none")
    g = TownGraph(walls=kind)

    # --- streets ------------------------------------------------------------
    pts, links = [], []
    for inst in streets:
        base = len(pts)
        pts += [tuple(p) for p in inst["nodes"]]
        links += [(a + base, b + base, w, ln) for a, b, w, ln in inst["links"] if a >= 0 and b >= 0]
    plaza_i = min(range(len(pts)), key=lambda i: math.dist(pts[i], plaza)) if pts else None
    order = sorted(range(len(pts)), key=lambda i: (-round(pts[i][1] / 40), pts[i][0]))
    sid: dict[int, str] = {}
    n = 0
    for i in order:
        if i == plaza_i:
            sid[i] = "P"
        else:
            n += 1
            sid[i] = str(n)
        g.add(Node(sid[i], "plaza" if i == plaza_i else "street", pts[i][0], pts[i][1]))
    for a, b, w, ln in links:
        g.edges.append(Edge(sid[a], sid[b], "street", ln, w))

    def nearest_street(x, y, max_m=1e9):
        best = min(sid, key=lambda i: math.dist(pts[i], (x, y)), default=None)
        if best is None or math.dist(pts[best], (x, y)) > max_m:
            return None
        return sid[best]

    def label(prefix, items):
        out = {}
        for k, b in enumerate(sorted(items, key=lambda b: (-round(b["y"] / 40), b["x"]))):
            out[id(b)] = f"{prefix}{k + 1}"
        return out

    tl, gl = label("T", towers), label("G", gates)

    # --- gates: always a way in at ground level (open when broken) --------------
    for b in gates:
        h = _health(b)
        g.add(Node(gl[id(b)], "gate", b["x"], b["y"], info=f"gate {h}%" + (" BROKEN" if h == 0 else ""),
                   building=b["id"]))
        s = nearest_street(b["x"], b["y"], DESCENT_TO_STREET_M)
        if s:
            g.edges.append(Edge(gl[id(b)], s, "street", math.dist((b["x"], b["y"]), (g.nodes[s].x, g.nodes[s].y))))
        g.entries.append((gl[id(b)], "through the gate (ram it)", h == 0))

    if kind == "none":
        g.entries.append(("*", "no walls: enter anywhere, onto any street", True))
        return g

    if kind == "wooden":
        # every wall section can be broken through; each is an entry point
        wl = label("W", walls_b)
        for b in walls_b:
            h = _health(b)
            g.add(Node(wl[id(b)], "wall", b["x"], b["y"], info=f"palisade {h}%" + (" BREACHED" if h == 0 else ""),
                       building=b["id"]))
            s = nearest_street(b["x"], b["y"], DESCENT_TO_STREET_M)
            if s:
                g.edges.append(Edge(wl[id(b)], s, "street", math.dist((b["x"], b["y"]), (g.nodes[s].x, g.nodes[s].y))))
            g.entries.append((wl[id(b)], "break through the palisade", h == 0))
        return g

    # --- stone walls: the walk on top of the walls, towers and gates as nodes ------
    segs, downs, plinks, doors = decode_walls(wall_platforms)
    # WALL buildings the wall walk passes through (doors) are wall corners and buttresses (10 hit points),
    # not towers and not something to break through; the rest are the curtain wall sections
    walls_b = [b for b in walls_b if not any(math.dist(d, (b["x"], b["y"])) < 15 for d in doors)]
    mids = [((x0 + x1) / 2, (y0 + y1) / 2) for x0, y0, x1, y1 in segs]
    adj = defaultdict(dict)  # platform -> platform it is linked to (the game's own wall-walk links)
    for a, b in plinks:
        if 0 <= a < len(segs) and 0 <= b < len(segs) and a != b:
            w = math.dist(mids[a], mids[b])
            adj[a][b] = adj[b][a] = w
    # which platforms belong to a tower / gatehouse
    anchor: dict[int, str] = {}
    for items, labels, kindname in ((towers, tl, "tower"), (gates, gl, "gate")):
        for b in items:
            near = [i for i, m in enumerate(mids) if math.dist(m, (b["x"], b["y"])) < TOWER_SNAP_M
                    or min(math.dist(segs[i][:2], (b["x"], b["y"])), math.dist(segs[i][2:], (b["x"], b["y"]))) < 8]
            if not near:
                continue
            wid = labels[id(b)] + "w"  # the tower / gatehouse on top of the wall
            h = _health(b)
            info = f"gatehouse, on top of {labels[id(b)]}" if kindname == "gate" else f"tower {h}%"
            g.add(Node(wid, kindname, b["x"], b["y"], level="wall", info=info,
                       building=b["id"] if kindname == "tower" else None))
            for i in near:
                anchor.setdefault(i, wid)
            # way down from it into the town (stairs at the tower / gatehouse)
            # ways down that belong to this tower / gatehouse (nearest to it of all towers and gates)
            dn = sorted((p for p in downs if math.dist(p, (b["x"], b["y"])) < 45
                         and math.dist(p, (b["x"], b["y"])) <= min(math.dist(p, (o["x"], o["y"])) for o in towers + gates)),
                        key=lambda p: math.dist(p, plaza))
            if dn:
                s_ = nearest_street(dn[0][0], dn[0][1], DESCENT_TO_STREET_M)
                if s_:
                    g.edges.append(Edge(wid, s_, "stairs", math.dist(dn[0], (g.nodes[s_].x, g.nodes[s_].y)),
                                        info="stairs down to the street"))
            if kindname == "gate":
                g.edges.append(Edge(wid, labels[id(b)], "stairs", 5, info="gatehouse stairs"))
    # wall sections (curtain walls between towers) sit on the platform closest to them
    def seg_dist(i, pt):
        x0, y0, x1, y1 = segs[i]
        dx, dy = x1 - x0, y1 - y0
        t = max(0.0, min(1.0, ((pt[0] - x0) * dx + (pt[1] - y0) * dy) / (dx * dx + dy * dy or 1)))
        return math.dist(pt, (x0 + t * dx, y0 + t * dy))

    wl = label("W", walls_b)
    on_platform: dict[int, list[dict]] = defaultdict(list)
    for b in walls_b:
        if segs:
            on_platform[min(range(len(segs)), key=lambda i: seg_dist(i, (b["x"], b["y"])))].append(b)
    placed = set()
    # contract the wall walk between anchors: Dijkstra from each anchor platform, stop at other anchors;
    # wall sections on the way become nodes of the chain (where ladders / a siege tower put you on the wall)
    seen = set()
    for start_p, a_id in anchor.items():
        dist = {start_p: 0.0}
        prev: dict[int, int] = {}
        h = [(0.0, start_p)]
        while h:
            d, v = heapq.heappop(h)
            if d > dist[v]:
                continue
            if v in anchor and anchor[v] != a_id:
                key = tuple(sorted((a_id, anchor[v])))
                if key not in seen:
                    seen.add(key)
                    path = [v]
                    while path[-1] in prev:
                        path.append(prev[path[-1]])
                    chain = [(0.0, a_id)]
                    for pl in reversed(path):
                        for b in on_platform.get(pl, []):
                            if id(b) not in placed:
                                placed.add(id(b))
                                chain.append((dist[pl], wl[id(b)]))
                                hp = _health(b)
                                g.add(Node(wl[id(b)], "wall", b["x"], b["y"], level="wall",
                                           info=f"wall section {hp}%" + (" BREACHED" if hp == 0 else ""),
                                           building=b["id"]))
                                s_ = nearest_street(b["x"], b["y"], DESCENT_TO_STREET_M)
                                if s_:
                                    g.edges.append(Edge(wl[id(b)], s_, "breach",
                                                        math.dist((b["x"], b["y"]), (g.nodes[s_].x, g.nodes[s_].y)),
                                                        info="OPEN" if hp == 0 else "closed"))
                                g.entries.append((wl[id(b)], "ladders / siege tower onto the wall top: OPEN; breach "
                                                  f"into the street: {'OPEN' if hp == 0 else 'closed'}", None))
                    chain.append((d, anchor[v]))
                    chain.sort(key=lambda c: c[0])
                    for (d0, n0), (d1, n1) in zip(chain, chain[1:]):
                        g.edges.append(Edge(n0, n1, "wall", max(1.0, d1 - d0), info="along the wall top"))
                continue
            for u, w in adj[v].items():
                nd = d + w
                if nd < dist.get(u, 1e18):
                    dist[u] = nd
                    prev[u] = v
                    heapq.heappush(h, (nd, u))
    return g


def shortest(g: TownGraph, src: str, buildings: dict | None = None) -> dict[str, float]:
    """Walking distance from src to every node. Breach edges count only when the wall section is broken."""
    adj = defaultdict(list)
    for e in g.edges:
        if e.kind == "breach":
            n = g.nodes.get(e.a)
            b = buildings.get(n.building) if buildings and n is not None and n.building is not None else None
            if b is None or b.health > 0:
                continue
        adj[e.a].append((e.b, e.length))
        adj[e.b].append((e.a, e.length))
    dist = {src: 0.0}
    h = [(0.0, src)]
    while h:
        d, a = heapq.heappop(h)
        if d > dist[a]:
            continue
        for b, w in adj[a]:
            if d + w < dist.get(b, 1e18):
                dist[b] = d + w
                heapq.heappush(h, (d + w, b))
    return dist


def alternative_paths(g: TownGraph, src: str, dst: str, k: int = 20, buildings: dict | None = None,
                      streets_only: bool = True):
    """Up to k routes src -> dst that share as few streets as possible: after each route its inner nodes
    get expensive. Returns [(nodes, length m)]."""
    adj = defaultdict(list)
    for e in g.edges:
        if e.kind == "breach":
            n = g.nodes.get(e.a)
            b = buildings.get(n.building) if buildings and n is not None and n.building is not None else None
            if b is None or b.health > 0:
                continue
        na, nb = g.nodes.get(e.a), g.nodes.get(e.b)
        if streets_only and any(n is not None and n.level == "wall" and n.id != src for n in (na, nb)):
            continue  # routes to the square go through the streets, not along the wall top
        adj[e.a].append((e.b, e.length))
        adj[e.b].append((e.a, e.length))
    import random

    rnd = random.Random(1)  # same routes for the same town
    penalty: dict[str, float] = defaultdict(float)
    out = []
    for it in range(k * 40):  # repeated routes are skipped, so try more times
        if len(out) >= k:
            break
        # first rounds: push away from places already used; later rounds: also jitter street lengths
        jitter = {} if it < k else {(a, b): rnd.uniform(1.0, 2.5) for a in adj for b, _ in adj[a]}
        dist, prev = {src: 0.0}, {}
        h = [(0.0, src)]
        while h:
            d, a = heapq.heappop(h)
            if d > dist[a] or a == dst:
                continue
            for b, w in adj[a]:
                nd = d + w * jitter.get((a, b), 1.0) + penalty[b]
                if nd < dist.get(b, 1e18):
                    dist[b], prev[b] = nd, a
                    heapq.heappush(h, (nd, b))
        if dst not in prev and src != dst:
            break
        path = [dst]
        while path[-1] != src:
            path.append(prev[path[-1]])
        path.reverse()
        length = sum(next(w for b, w in adj[a] if b == c) for a, c in zip(path, path[1:]))
        for n in path[1:-1]:
            penalty[n] += 60.0  # discourage reusing these places
        if not any(path == p for p, _ in out):
            out.append((path, length))
    return sorted(out, key=lambda r: r[1])


def approach_summary(g: TownGraph, plan, units) -> str:
    """From which sides the groups of the current plan that head for the square will reach it."""
    from .geometry import compass

    plaza = next((n for n in g.nodes.values() if n.kind == "plaza"), None)
    if plaza is None or plan is None:
        return ""
    alive = {u.id for u in units}
    sides: dict[str, list[str]] = defaultdict(list)
    for grp in plan.groups:
        if grp.task != "move" or not grp.path or grp.path[-1] != plaza.id or len(grp.path) < 2:
            continue
        n = g.nodes.get(grp.path[-2])
        if n is None:
            continue
        k = sum(1 for i in grp.unit_ids if i in alive)
        if k:
            sides[compass(n.x - plaza.x, n.y - plaza.y)].append(f"{grp.name} ({k} units)")
    if not sides:
        return ""
    return "YOUR APPROACHES TO THE SQUARE (current plan): " + "; ".join(
        f"from the {s}: {', '.join(v)}" for s, v in sorted(sides.items()))


def _street_adj(g: TownGraph, buildings: dict | None, src: str):
    """Walkable links for routes to the square: streets, stairs and open breaches (a breach only once its
    wall section is broken); the wall top is left out except for the starting node itself."""
    adj = defaultdict(list)
    for e in g.edges:
        if e.kind == "breach":
            n = g.nodes.get(e.a)
            b = buildings.get(n.building) if buildings and n is not None and n.building is not None else None
            if b is None or b.health > 0:
                continue
        na, nb = g.nodes.get(e.a), g.nodes.get(e.b)
        if any(n is not None and n.level == "wall" and n.id != src for n in (na, nb)):
            continue
        adj[e.a].append((e.b, e.length))
        adj[e.b].append((e.a, e.length))
    return adj


def _route(adj, src: str, dst: str, avoid: set[str] = frozenset(), banned_links: set = frozenset()):
    """Shortest path src -> dst not passing through `avoid` nodes nor the `banned_links` (a, b) pairs."""
    dist, prev = {src: 0.0}, {}
    h = [(0.0, src)]
    while h:
        d, a = heapq.heappop(h)
        if d > dist[a]:
            continue
        if a == dst:
            break
        for b, w in adj[a]:
            if (b in avoid and b != dst) or (a, b) in banned_links:
                continue
            if d + w < dist.get(b, 1e18):
                dist[b], prev[b] = d + w, a
                heapq.heappush(h, (d + w, b))
    if dst not in dist:
        return None
    path = [dst]
    while path[-1] != src:
        path.append(prev[path[-1]])
    return path[::-1], dist[dst]


def two_routes(g: TownGraph, buildings: dict | None, src: str, dst: str, avoid=()) -> list[tuple[list[str], float]]:
    """The fastest route src -> dst and an alternative one: into the square by a different street (when dst is
    the square), else the best route sharing as little as possible with the fastest."""
    adj = _street_adj(g, buildings, src)
    avoid = set(avoid) - {src, dst}
    best = _route(adj, src, dst, avoid)
    if best is None:
        return []
    out = [best]
    if len(best[0]) >= 2:
        banned: set = set()
        alt = None
        for _ in range(12):  # keep closing the streets that lead into dst the same way as the fastest
            banned |= {(out[0][0][-2], dst)} | {(a, dst) for a in [r[0][-2] for r in [alt] if r]}
            alt = _route(adj, src, dst, avoid, banned)
            if alt is None or alt[0][-2] != best[0][-2]:
                break
        if alt is None or alt[0] == best[0]:  # no other way into dst: the route sharing the fewest streets
            alts = [r for r in alternative_paths(g, src, dst, 3, buildings) if r[0] != best[0]
                    and not set(r[0]) & avoid]
            alt = alts[0] if alts else None
        if alt is not None:
            out.append(alt)
    return out


def ways_in(g: TownGraph, buildings: dict) -> list[tuple[str, str]]:
    """Every way into the town: the gates, and the wall sections that are broken (open breaches)."""
    out = [(n.id, "gate") for n in sorted(g.nodes.values(), key=lambda n: n.id.zfill(5))
           if n.kind == "gate" and n.level == "ground"]
    # broken wall sections joined to a street: stone walls by a "breach" link, palisades by a plain street link
    linked = {e.a for e in g.edges if e.kind in ("breach", "street")} | {e.b for e in g.edges if e.kind == "street"}
    for n in sorted(g.nodes.values(), key=lambda n: n.id.zfill(5)):
        b = buildings.get(n.building) if n.kind == "wall" and n.building is not None else None
        if b is not None and b.health == 0 and n.id in linked:
            out.append((n.id, "breach"))
    return out


def _route_text(g: TownGraph, path: list[str], length: float) -> str:
    plaza = next((n for n in g.nodes.values() if n.kind == "plaza"), None)
    side = ""
    if plaza is not None and path[-1] == plaza.id and len(path) >= 2:
        from .geometry import compass

        a = g.nodes[path[-2]]
        side = f", into P from the {compass(a.x - plaza.x, a.y - plaza.y)}"
    return f"{'-'.join(path)} {length:.0f}m{side}"


def entrance_routes(g: TownGraph, buildings: dict, ai_alliance: int | None = None) -> str:
    """For every gate and every open breach: the fastest route to the square and an alternative one that
    reaches it by another street."""
    plaza = next((n for n in g.nodes.values() if n.kind == "plaza"), None)
    if plaza is None:
        return ""
    closed = closed_gates(g, buildings, ai_alliance)
    rows = []
    for nid, kind in ways_in(g, buildings):
        rs = two_routes(g, buildings, nid, plaza.id)
        if not rs:
            continue
        note = ("CLOSED gate - usable once broken or opened" if nid in closed else "open gate") if kind == "gate" \
            else "BROKEN wall section - open"
        rows.append(f"  {nid} [{note}]: fastest {_route_text(g, *rs[0])}"
                    + (f"; alternative {_route_text(g, *rs[1])}" if len(rs) > 1 else "; no other way in"))
    if not rows:
        return ""
    return ("ROUTES TO THE SQUARE from every way in (fastest + an alternative entering P by another street; "
            "use them as path; ask for others with route_requests):\n" + "\n".join(rows))


def requested_routes(g: TownGraph, buildings: dict, requests) -> str:
    """The routes the general asked for in his last plan."""
    plaza = next((n.id for n in g.nodes.values() if n.kind == "plaza"), "P")
    rows = []
    for r in requests:
        dst = r.to_node or plaza
        bad = [x for x in [r.from_node, dst] if x not in g.nodes]
        if bad:
            rows.append(f"  {r.from_node} -> {dst}: unknown node {', '.join(bad)}")
            continue
        rs = two_routes(g, buildings, r.from_node, dst, r.avoid)
        avoid = f" avoiding {', '.join(r.avoid)}" if r.avoid else ""
        if not rs:
            rows.append(f"  {r.from_node} -> {dst}{avoid}: NO ROUTE")
            continue
        rows.append(f"  {r.from_node} -> {dst}{avoid}: fastest {_route_text(g, *rs[0])}"
                    + (f"; alternative {_route_text(g, *rs[1])}" if len(rs) > 1 else ""))
    return "REQUESTED ROUTES (as you asked in your last plan):\n" + "\n".join(rows) if rows else ""


def key_nodes(g: TownGraph) -> list[str]:
    """The town square, the gates and the towers / gatehouses with stairs down."""
    stairs = {e.a for e in g.edges if e.kind == "stairs"} | {e.b for e in g.edges if e.kind == "stairs"}
    keys = [n.id for n in g.nodes.values() if n.kind == "plaza"]
    keys += sorted((n.id for n in g.nodes.values() if n.kind == "gate" and n.level == "ground"), key=lambda i: i.zfill(4))
    keys += sorted((n.id for n in g.nodes.values() if n.kind == "tower" and n.id in stairs), key=lambda i: i.zfill(5))
    return keys


def distance_table(g: TownGraph) -> str:
    keys = key_nodes(g)
    if len(keys) < 2:
        return ""
    rows = ["DISTANCES between key places, walking along streets / walls (m; '-' = no way):",
            "  " + " ".join(f"{k:>5}" for k in [""] + keys)]
    for a in keys:
        d = shortest(g, a)
        rows.append("  " + " ".join([f"{a:>5}"] + [f"{d[b]:5.0f}" if b in d else "    -" for b in keys]))
    return "\n".join(rows)


def march(unit, metres: float) -> str:
    walk, run = SPEED.get(unit.category, SPEED["other"])
    t = lambda v: f"{metres / v / 60:.1f}min"  # noqa: E731
    return f"~{t(walk)} walk" + (f" / {t(run)} run" if run > walk else "")


ON_NODE_SHARE = 0.10  # within this share of a street's length from one end, a unit counts as on that node
OFF_STREET_M = 30.0  # farther than this from every street: outside the town / under the walls
PLAZA_RADIUS_M = 30.0  # the town square is an open area: this close to its node a unit is "at P"


def place_on_graph(g: TownGraph, x: float, y: float):
    """Where a point is on the street network: ("node", a, None, d_a, 0) on a node, ("street", a, b, d_a, d_b)
    between two nodes (metres along the street from each), or None when it is off the streets."""
    best = None
    for e in g.edges:
        na, nb = g.nodes.get(e.a), g.nodes.get(e.b)
        if na is None or nb is None or na.level != "ground" or nb.level != "ground" or e.kind not in ("street",):
            continue
        dx, dy = nb.x - na.x, nb.y - na.y
        ln2 = dx * dx + dy * dy
        t = 0.0 if ln2 == 0 else max(0.0, min(1.0, ((x - na.x) * dx + (y - na.y) * dy) / ln2))
        off = math.dist((x, y), (na.x + t * dx, na.y + t * dy))
        if best is None or off < best[0]:
            best = (off, na, nb, t, math.sqrt(ln2))
    if best is None or best[0] > OFF_STREET_M:
        return None
    plaza = next((n for n in g.nodes.values() if n.kind == "plaza"), None)
    if plaza is not None and math.dist((x, y), (plaza.x, plaza.y)) < PLAZA_RADIUS_M:
        return ("node", plaza.id, None, math.dist((x, y), (plaza.x, plaza.y)), 0.0)
    off, na, nb, t, ln = best
    if t <= ON_NODE_SHARE:
        return ("node", na.id, None, t * ln, 0.0)
    if t >= 1 - ON_NODE_SHARE:
        return ("node", nb.id, None, (1 - t) * ln, 0.0)
    return ("street", na.id, nb.id, t * ln, (1 - t) * ln)


def unit_positions(g: TownGraph, units, buildings: dict, own: bool, attacker: bool = False) -> list[str]:
    """Where each unit is on the graph; for own units also the walking distance and time to the square."""
    plaza = next((n.id for n in g.nodes.values() if n.kind == "plaza"), None)
    closed = closed_gates(g, buildings, units[0].alliance if units else None) if attacker else set()
    towers_alive = [n for n in g.nodes.values() if (n.kind == "tower" or (n.kind == "gate" and n.level == "wall"))
                    and not (n.building is not None and buildings.get(n.building) is not None
                             and buildings[n.building].health == 0)]
    out = []
    for u in units:
        # in town: on a node, or on the street between two nodes (metres from each end); outside (or under the
        # walls): the nearest node of any kind, usually a gate or a wall section - the way in it would take
        pl = place_on_graph(g, u.x, u.y)
        ends = []  # (node, metres to it) the unit can walk to the square through
        if pl is not None and pl[0] == "street":
            _, a, b, da, db = pl
            n, d = (g.nodes[a], da) if da <= db else (g.nodes[b], db)
            ends = [(a, da), (b, db)]
            where = f"#{u.id} on the street {a}-{b}, {da:.0f}m from {a}, {db:.0f}m from {b}"
        elif pl is not None:
            n, d = g.nodes[pl[1]], pl[3]
            where = f"#{u.id} at {n.id}"
        else:
            n = min(g.nodes.values(), key=lambda n: math.dist((n.x, n.y), (u.x, u.y)))
            d = math.dist((n.x, n.y), (u.x, u.y))
            where = f"#{u.id} " + (f"at {n.id}" if d < 25 else f"{d:.0f}m from {n.id}")
        if own and attacker:
            near = [(math.dist((t.x, t.y), (u.x, u.y)), t.id) for t in towers_alive]
            if near and min(near)[0] < TOWER_RANGE_M:
                where += f" [UNDER FIRE RANGE of tower {min(near)[1]}, {min(near)[0]:.0f}m]"
        if own and plaza is not None:
            via = [(shortest(g, nid, buildings).get(plaza), m) for nid, m in (ends or [(n.id, d)])]
            via = [dp + m for dp, m in via if dp is not None]
            if via:
                total = min(via)
                where += f", to P {total:.0f}m {march(u, total)}"
                if attacker and d > 25 and n.id in closed:
                    where += f" once through the CLOSED gate {n.id}"
        out.append(where)
    return out


def wall_path(g: TownGraph, src: str, dst: str) -> tuple[list[str], float] | None:
    """Shortest way along the wall top (wall sections, towers, gatehouses only)."""
    adj = defaultdict(list)
    for e in g.edges:
        if e.kind == "wall":
            adj[e.a].append((e.b, e.length))
            adj[e.b].append((e.a, e.length))
    dist, prev, h = {src: 0.0}, {}, [(0.0, src)]
    while h:
        d, a = heapq.heappop(h)
        if a == dst:
            break
        if d > dist[a]:
            continue
        for b, w in adj[a]:
            if d + w < dist.get(b, 1e18):
                dist[b], prev[b] = d + w, a
                heapq.heappush(h, (d + w, b))
    if dst not in dist:
        return None
    path = [dst]
    while path[-1] != src:
        path.append(prev[path[-1]])
    return path[::-1], dist[dst]


def to_gatehouse(g: TownGraph, wall_node: str) -> tuple[list[str], float] | None:
    """From a wall section to the nearest gatehouse along the wall top."""
    best = None
    for n in g.nodes.values():
        if n.kind == "gate" and n.level == "wall":
            r = wall_path(g, wall_node, n.id)
            if r and (best is None or r[1] < best[1]):
                best = r
    return best


def for_battle(start) -> TownGraph | None:
    """Build (once) the graph for a BattleStart that has the hook's street graph."""
    if start.town_graph is None and start.streets and start.map.plaza:
        items = [{"id": b.id, "kind": b.kind, "x": b.x, "y": b.y, "damage": b.damage, "max_hp": b.max_hp}
                 for b in start.buildings.values() if b.x is not None]
        start.town_graph = build(start.streets, items, start.wall_platforms,
                                 (start.map.plaza[0], start.map.plaza[1]))
    return start.town_graph


def nearest_node(g: TownGraph, x: float, y: float, max_m: float = 35.0, ground: bool = True) -> str | None:
    best = min((n for n in g.nodes.values() if not ground or n.level == "ground"),
               key=lambda n: math.dist((n.x, n.y), (x, y)), default=None)
    return best.id if best is not None and math.dist((best.x, best.y), (x, y)) <= max_m else None


def status(g: TownGraph, buildings: dict, ai_alliance: int | None = None) -> str:
    """What changed since the briefing: damaged / broken gates, towers and wall sections (the ways in)."""
    out = []
    for n in g.nodes.values():
        b = buildings.get(n.building) if n.building is not None else None
        if b is None or b.health >= 100:
            continue
        what = {"gate": "gate", "tower": "tower", "wall": "wall section"}.get(n.kind, n.kind)
        if b.health == 0:
            out.append(f"{n.id} {what} " + ("DESTROYED" if n.kind == "tower" else "BROKEN - OPEN way in"))
        else:
            out.append(f"{n.id} {what} {b.health}%")
    gates = []
    for n in sorted((n for n in g.nodes.values() if n.kind == "gate" and n.level == "ground"), key=lambda n: n.id):
        b = buildings.get(n.building) if n.building is not None else None
        if b is not None and b.health == 0:
            st = "OPEN (broken)"
        elif b is not None and b.owner >= 0 and ai_alliance is not None:
            who = "YOURS (your units hold it, you can go through)" if b.owner == ai_alliance else "held by the enemy"
            st = ("OPEN, " if b.open == 1 else "CLOSED, ") + who
        else:
            st = "OPEN" if b is not None and b.open == 1 else "CLOSED"
        gates.append(f"{n.id} {st}")
    gate_line = ("GATES: " + ", ".join(gates) + " - a CLOSED gate cannot be passed until it is broken (ram) or "
                 "opened from the gatehouse") if gates else ""
    dmg = ("TOWN DEFENCES DAMAGED: " + "; ".join(out)) if out else "Town defences: all intact."
    return dmg + ("\n" + gate_line if gate_line else "")


def closed_gates(g: TownGraph, buildings: dict, ai_alliance: int | None = None) -> set[str]:
    """Gates the given side cannot pass: not broken, not open, and not controlled by it."""
    out = set()
    for n in g.nodes.values():
        if n.kind == "gate" and n.level == "ground":
            b = buildings.get(n.building) if n.building is not None else None
            if b is None or (b.health > 0 and b.open != 1 and not (ai_alliance is not None and b.owner == ai_alliance)):
                out.add(n.id)
    return out


def describe(g: TownGraph) -> str:
    """Compact text for the general's briefing."""
    deg = defaultdict(int)
    for e in g.edges:
        deg[e.a] += 1
        deg[e.b] += 1
    out = [f"TOWN GRAPH (from the game's own pathfinding). Walls: {g.walls.upper()}. "
           "Ids: P = town square, numbers = street places, G = gate, T..w / G..w = tower / gatehouse on top "
           "of the wall, W = wall section on top of the wall (ladders / siege tower land here; a BREACH edge "
           "leads from it straight to the street once the section is destroyed)."]
    out.append("PLACES: id (x,y) [level] info")
    for n in sorted(g.nodes.values(), key=lambda n: ({"plaza": 0, "gate": 1, "tower": 2, "wall": 3}.get(n.kind, 4),
                                                       n.id.zfill(5))):
        lvl = " [on wall]" if n.level == "wall" else ""
        out.append(f"  {n.id} ({n.x:.0f},{n.y:.0f}){lvl}" + (f" {n.info}" if n.info else ""))
    out.append("CONNECTIONS: a-b length (street width) / kind")
    for e in g.edges:
        tail = f" w{e.width:.0f}" if e.width else ""
        kind = "" if e.kind == "street" else f" {e.kind.upper()}" + (f" ({e.info})" if e.kind == "breach" else "")
        out.append(f"  {e.a}-{e.b} {e.length:.0f}m{tail}{kind}")
    table = distance_table(g)
    if table:
        out.append(table)
    if g.walls == "stone":
        out.append("Ladders and siege towers put you on the wall top: walk along the wall to a tower or gatehouse "
                   "and take its STAIRS down. A destroyed wall section (BREACH) leads straight into the street.")
    out.append("WAYS IN:")
    for node, how, is_open in g.entries:
        out.append(f"  {node}: {how}" + ("" if is_open is None else f" - {'OPEN' if is_open else 'closed'}"))
    return "\n".join(out)
