"""Short-term prediction of the battle (2-15 s), for the general: where units will be if every unit keeps its
current order, who runs into whom and from which side, who comes into missile range, which enemy movement ends
beside or behind our line, and who is losing men fast.

Every unit's current order is known from the game for both sides (its attack target, its move destination, at a
walk or a run), and so are the speeds of its type. An enemy unit with no known order (standing, or arrived where
it was sent) keeps the course of its last seconds (state.trend), if it was moving. Fights and morale are not
predicted: only movement and the losses trend."""

from __future__ import annotations

import math

from .geometry import alive, bearing, compass
from .protocol import BattleStart, State, Unit

HORIZON_S = 15.0  # how far ahead events are looked for
STEP_S = 0.5
SNAPSHOTS = (2.0, 5.0, 10.0)
CONTACT_M = 15.0  # centre to centre: the attacker has reached its target
MAX_CONTACTS = 8
MAX_RANGE_EVENTS = 4
MAX_TRACKS = 8
LOSS_WINDOW_S = 15.0  # state.recent_losses covers this many seconds (server.LOSS_WINDOW)
IDLE_M = 120.0  # no enemy this close for the next 10 s: the unit has nothing to do where it stands
WALK_ONLY = (1, 2, 17)  # phalanx / testudo / schiltrom on: the unit cannot run


def _speed(u: Unit) -> float:
    walk = u.walk_speed or 1.5
    if u.ability_on and u.ability in WALK_ONLY:
        return walk
    return (u.run_speed or walk * 2.5) if (u.order_run or u.action == "running") else walk


def _speed_run(u: Unit) -> float:
    """Top speed it could go at if ordered to run (walking if a phalanx-like formation is on)."""
    walk = u.walk_speed or 1.5
    if u.ability_on and u.ability in WALK_ONLY:
        return walk
    return u.run_speed or walk * 2.5


def _engaged(u: Unit) -> bool:
    return u.action in ("melee", "shooting", "routing", "dead")


def _town_route(start: BattleStart | None, u: Unit, dest: tuple[float, float]) -> list[tuple[float, float]] | None:
    """In a town: the way to dest along the streets (node positions, then dest), or None to go straight."""
    g = getattr(start, "town_graph", None) if start is not None else None
    if g is None:
        return None
    from .town_graph import _route, _street_adj, nearest_node

    a, b = nearest_node(g, u.x, u.y), nearest_node(g, dest[0], dest[1])
    if a is None or b is None or a == b:
        return None
    r = _route(_street_adj(g, start.buildings, a), a, b)
    if r is None:
        return None
    return [(g.nodes[n].x, g.nodes[n].y) for n in r[0][1:]] + [dest]


def simulate(state: State, start: BattleStart | None = None, trended: set[int] | None = None
             ) -> tuple[dict[int, list[tuple[float, float]]], dict[int, float]]:
    """Positions of every unit every STEP_S up to HORIZON_S, and the time each attacker reaches its target.
    Our units follow the executor's remaining waypoints (state.own_paths); in a town, units with a move order
    follow the streets; otherwise straight lines. An enemy unit with no known order to follow keeps its recent
    course (state.trend); their ids are added to `trended`."""
    from .units_db import missile_range

    units = [u for u in state.units if u.on_field and u.soldiers > 0 and u.action != "dead"]
    by_id = {u.id: u for u in units}
    # a missile unit sent to attack stops where it can shoot (unless it charges in: melee is not reported)
    shoots_at = {u.id: missile_range(u.name) for u in units
                 if missile_range(u.name) and (u.ammo is None or u.ammo > 0) and (u.category != "siege" or u.engines)}
    ways: dict[int, list[tuple[float, float]]] = {}
    for u in units:
        if u.target is not None or u.action not in ("moving", "running"):
            continue
        if u.id in state.own_paths and state.own_paths[u.id]:
            ways[u.id] = [tuple(p) for p in state.own_paths[u.id]]
        elif u.dest is not None:
            ways[u.id] = _town_route(start, u, (u.dest[0], u.dest[1])) or [(u.dest[0], u.dest[1])]
    pos = {u.id: (u.x, u.y) for u in units}
    tracks = {u.id: [pos[u.id]] for u in units}
    contact: dict[int, float] = {}
    steps = int(HORIZON_S / STEP_S)
    for k in range(1, steps + 1):
        new = {}
        for u in units:
            x, y = pos[u.id]
            goal, budget = None, _speed(u) * STEP_S
            if u.id in ways and u.id in state.trend and math.dist((x, y), ways[u.id][-1]) < 3:
                del ways[u.id]  # an enemy that got where it was sent: from here on it keeps its recent course
            if u.id in contact or _engaged(u):
                pass
            elif u.target is not None and u.target in by_id and by_id[u.target].alliance != u.alliance:
                goal = pos[u.target]  # chases where its target is now (the target moves too)
                if u.id in shoots_at and math.dist((x, y), goal) <= shoots_at[u.id]:
                    goal = None  # in range: it stands and shoots
            elif u.id in ways:
                w = ways[u.id]
                while len(w) > 1 and math.dist((x, y), w[0]) < 3:
                    w.pop(0)
                goal = w[0]
            elif u.id in state.trend:
                vx, vy = state.trend[u.id]  # no known order: it keeps the course of its last seconds
                goal, budget = (x + vx * STEP_S, y + vy * STEP_S), min(math.hypot(vx, vy), _speed_run(u)) * STEP_S
                if trended is not None:
                    trended.add(u.id)
            elif u.action in ("moving", "running") and u.id in state.velocity and k * STEP_S <= 5.0:
                vx, vy = state.velocity[u.id]  # moving with no known goal: keep going a little while
                goal, budget = (x + vx * STEP_S, y + vy * STEP_S), math.hypot(vx, vy) * STEP_S
            if goal is not None:
                d = math.dist((x, y), goal)
                if d > 1e-6:
                    f = min(1.0, budget / d)
                    x, y = x + (goal[0] - x) * f, y + (goal[1] - y) * f
            new[u.id] = (x, y)
        pos = new
        for u in units:
            tracks[u.id].append(pos[u.id])
            if (u.id not in contact and u.id not in shoots_at and u.target is not None and u.target in pos
                    and not _engaged(u)
                    and by_id[u.target].alliance != u.alliance
                    and math.dist(pos[u.id], pos[u.target]) <= CONTACT_M):
                contact[u.id] = k * STEP_S
    return tracks, contact


def _at(track: list[tuple[float, float]], t: float) -> tuple[float, float]:
    return track[min(len(track) - 1, int(round(t / STEP_S)))]


def _side(target: Unit, frm: tuple[float, float], at: tuple[float, float]) -> str:
    """Front / left flank / right flank / rear of the target, for an attacker coming from frm to at."""
    ang = bearing(frm[0] - at[0], frm[1] - at[1])  # where the attacker comes from, seen from the target
    diff = (ang - target.heading + 540) % 360 - 180  # 0 = straight ahead of the target, + = its right
    if abs(diff) <= 60:
        return "front"
    if abs(diff) >= 120:
        return "REAR"
    return "RIGHT FLANK" if diff > 0 else "LEFT FLANK"


def _when(t: float) -> str:
    return "now" if t < 1.0 else f"in ~{t:.0f}s"


def _kind(u: Unit) -> str:
    return {"cavalry": "cav", "missile": "msl", "siege": "art", "general": "general"}.get(u.category, "inf")


def _line_frame(own: list[Unit]):
    """Centre, front and right-hand vectors and the lateral extent of our fighting line, or None."""
    line = [u for u in own if u.category in ("infantry", "cavalry") and u.action != "routing"]
    if len(line) < 3:
        return None
    fx = sum(math.sin(math.radians(u.heading)) for u in line)
    fy = sum(math.cos(math.radians(u.heading)) for u in line)
    n = math.hypot(fx, fy)
    if n < 0.5 * len(line):  # facing all ways (a square, a melee): no single front
        return None
    fx, fy = fx / n, fy / n
    cx, cy = sum(u.x for u in line) / len(line), sum(u.y for u in line) / len(line)
    rx, ry = fy, -fx
    lats = [((u.x - cx) * rx + (u.y - cy) * ry, u) for u in line]
    return (cx, cy), (fx, fy), (rx, ry), min(lats, key=lambda p: p[0]), max(lats, key=lambda p: p[0])


def predictions(start: BattleStart, state: State) -> list[str]:
    from .units_db import missile_range

    ai = start.ai_alliance
    mine = [u for u in alive([u for u in state.units if u.alliance == ai]) if u.on_field]
    foes = [u for u in alive([u for u in state.units if u.alliance != ai]) if u.on_field]
    if not mine or not foes:
        return []
    trended: set[int] = set()
    tracks, contact = simulate(state, start, trended)
    by_id = {u.id: u for u in state.units}
    events: list[tuple[float, str]] = []  # contacts
    ranges: list[tuple[float, str]] = []  # coming into missile range
    dangers: list[tuple[str, tuple[float, float], float, set[int]]] = []  # (what, where, in s, units not to send)

    # 1. who reaches whom, from which side, running or not
    done = set()
    for uid, t in contact.items():
        a, b = by_id[uid], by_id[by_id[uid].target]
        if b.id in contact and b.target == a.id:  # both charge each other: one event
            if (b.id, a.id) in done:
                continue
            done.add((a.id, b.id))
            mine_u, foe_u = (a, b) if a.alliance == ai else (b, a)
            events.append((min(t, contact[b.id]), f"{_when(min(t, contact[b.id]))}: your #{mine_u.id} ({_kind(mine_u)}) "
                                                     f"and enemy #{foe_u.id} ({_kind(foe_u)}) charge into each other"))
            continue
        at_b = _at(tracks[b.id], t)
        frm = _at(tracks[a.id], max(0.0, t - 2.0))
        side = _side(b, frm, at_b)
        who = "enemy" if a.alliance != ai else "your"
        tgt = "your" if b.alliance == ai else "enemy"
        how = "at a RUN" if (a.order_run or a.action == "running") else "at a walk"
        events.append((t, f"{_when(t)}: {who} #{a.id} ({_kind(a)}) reaches {tgt} #{b.id} ({_kind(b)}) - "
                          f"hits its {side}, {how}"))
        if a.alliance != ai and side != "front":
            dangers.append((f"enemy #{a.id} into the {side} of your #{b.id}", at_b, t, {b.id}))

    # 2. coming into missile range (first moment the distance drops below the range)
    def entering(shooters: list[Unit], others: list[Unit], text):
        for s in shooters:
            r = missile_range(s.name)
            if not r or (s.ammo is not None and s.ammo <= 0) or (s.category == "siege" and s.engines == 0):
                continue
            for o in others:
                if math.dist((s.x, s.y), (o.x, o.y)) <= r:
                    continue
                for k, p in enumerate(tracks.get(o.id, [])):
                    if math.dist(_at(tracks[s.id], k * STEP_S), p) <= r:
                        ranges.append((k * STEP_S, text(s, o, k * STEP_S, r)))
                        break

    entering([u for u in mine if missile_range(u.name)], foes,
             lambda s, o, t, r: f"{_when(t)}: enemy #{o.id} ({_kind(o)}) comes into range of your #{s.id} ({r:.0f} m)")
    entering([u for u in foes if missile_range(u.name)], [u for u in mine if u.action in ("moving", "running")],
             lambda s, o, t, r: f"{_when(t)}: your #{o.id} comes into range of enemy #{s.id} ({_kind(s)}, {r:.0f} m)")

    # 3. enemy movement that ends beside or behind our line
    frame = _line_frame(mine)
    threats = []
    if frame is not None:
        (cx, cy), (fx, fy), (rx, ry), (lmin, umin), (lmax, umax) = frame
        for e in foes:
            if e.dest is None or e.action not in ("moving", "running") or e.target is not None:
                continue
            gx, gy = e.dest
            along = (gx - cx) * fx + (gy - cy) * fy
            lat = (gx - cx) * rx + (gy - cy) * ry
            now_along = (e.x - cx) * fx + (e.y - cy) * fy
            if along < -15 and now_along > along:
                where, ref, past = "BEHIND YOUR LINE", None, None
            elif along < 40 and lat > lmax + 15:  # level with the line (not far out in front) and past its end
                where, ref, past = "BEYOND YOUR RIGHT FLANK", umax, lat - lmax
            elif along < 40 and lat < lmin - 15:
                where, ref, past = "BEYOND YOUR LEFT FLANK", umin, lmin - lat
            else:
                continue
            left = math.dist((e.x, e.y), (gx, gy))
            eta = left / max(_speed(e), 0.5)
            if eta > 60:
                continue
            note = f", {past:.0f} m past your #{ref.id}" if ref is not None else ""
            if left >= 15:
                dangers.append((f"enemy #{e.id} {where.lower()}", (gx, gy), eta, set()))
            if left < 15:
                threats.append(f"enemy #{e.id} ({_kind(e)}) stands {where}{note} at ({gx:.0f},{gy:.0f})")
            else:
                threats.append(f"enemy #{e.id} ({_kind(e)}, {'running' if e.order_run else 'walking'}) heading for "
                               f"({gx:.0f},{gy:.0f}): {where}{note}, arrives in ~{eta:.0f}s")

    # 4. who can get there first: the fastest free unit of ours to each danger point
    free = [u for u in mine if u.action in ("idle", "moving", "running") and u.category in ("infantry", "cavalry")]
    intercepts, seen = [], set()
    for what, (px, py), t, skip in sorted(dangers, key=lambda d: d[2]):
        key = what.split(" ", 2)[-1] if what.startswith("enemy #") and " beyond " in what or " behind " in what \
            else (round(px / 30), round(py / 30))  # several enemies heading the same way: one line
        if key in seen or len(intercepts) >= 3:
            continue
        seen.add(key)
        cands = sorted(((math.dist((u.x, u.y), (px, py)) / max(_speed_run(u), 0.5), u) for u in free
                        if u.id not in skip), key=lambda c: c[0])[:2]
        if not cands:
            continue
        best = cands[0][0]
        lead = "fastest of yours" if best <= t + 15 else "NOBODY of yours gets there in time - fastest"
        intercepts.append(f"{what} (in ~{t:.0f}s): {lead} to ({px:.0f},{py:.0f}): "
                          + ", ".join(f"#{u.id} ({_kind(u)}) ~{eta:.0f}s" for eta, u in cands))

    # 5. ammunition and fatigue trends (last ~10 s)
    supplies = []
    for u in mine:
        rate = state.ammo_rate.get(u.id, 0.0)
        if rate > 0 and u.ammo is not None and u.ammo > 0:
            left_s = u.ammo / rate
            if left_s <= 60:
                supplies.append(f"your #{u.id} runs out of ammunition in ~{left_s:.0f}s at this rate")
        fr = state.fatigue_rate.get(u.id, 0.0)
        for limit, word in ((0.5, "tired"), (0.8, "exhausted")):
            if fr > 0 and u.fatigue < limit:
                left_s = (limit - u.fatigue) / fr
                if left_s <= 45:
                    supplies.append(f"your #{u.id} will be {word} in ~{left_s:.0f}s if it keeps this pace")
                break

    # 6. risk of breaking: a warning sign from the numbers we have, not a prediction of the game's morale
    risks = []
    for u in mine + foes:
        if u.action != "melee":
            continue
        reasons = []
        hit_from = {_side(u, (f.x, f.y), (u.x, u.y)) for f in (foes if u.alliance == ai else mine)
                    if f.target == u.id and f.action == "melee"}
        if hit_from - {"front"}:
            reasons.append("attacked in the " + " and ".join(sorted(h.lower() for h in hit_from - {"front"})))
        lost = state.recent_losses.get(u.id, 0)
        if lost >= 0.2 * u.max_soldiers:
            reasons.append(f"lost {lost} men in 15 s")
        if u.morale < 0.6:
            reasons.append(f"morale {u.morale:.0%}")
        high = u.morale < 0.35 or (len(reasons) >= 2 and u.morale < 0.7)
        if high or len(reasons) >= 2:
            who = "your" if u.alliance == ai else "enemy"
            risks.append((u.morale, f"{who} #{u.id} {'HIGH RISK' if high else 'at risk'} of breaking ("
                                    + ", ".join(reasons) + ")"))

    # 8. where the enemy is massing - which part of our army his units are heading for, whatever our formation
    cx = sum(u.x for u in mine) / len(mine)
    cy = sum(u.y for u in mine) / len(mine)
    aims: dict[str, list[tuple[Unit, Unit, float | None]]] = {}
    for e in foes:
        if e.id not in tracks or e.action == "routing":
            continue
        tr = tracks[e.id]
        aimed, first = None, None
        if e.target is not None and by_id.get(e.target) is not None and by_id[e.target].alliance == ai:
            aimed = by_id[e.target]
            first = contact.get(e.id)
        else:  # the unit of ours its predicted path gets closest to, if it comes within 60 m in the next 15 s
            best = None
            for m in mine:
                if m.id not in tracks:
                    continue
                for k, pnt in enumerate(tr):
                    d = math.dist(pnt, tracks[m.id][k])
                    if d <= 60 and (best is None or k * STEP_S < best[1]):
                        best = (m, k * STEP_S)
                        break
            if best:
                aimed, first = best
            elif e.dest is not None and e.action in ("moving", "running"):  # slower: where his move order ends
                gx, gy = e.dest
                m = min(mine, key=lambda m: math.dist((m.x, m.y), (gx, gy)))
                if math.dist((m.x, m.y), (gx, gy)) <= 120:
                    aimed, first = m, None
        if aimed is None or math.dist((aimed.x, aimed.y), (cx, cy)) < 1:
            side = "centre"
        else:
            side = compass(aimed.x - cx, aimed.y - cy)
        if aimed is not None:
            aims.setdefault(side, []).append((e, aimed, first))
    massing = []
    total_men = sum(f.soldiers for f in foes) or 1
    for side, rows in aims.items():
        men = sum(e.soldiers for e, _, _ in rows)
        if len(rows) < 2 and men < 0.2 * total_men:
            continue
        kinds = {}
        for e, _, _ in rows:
            kinds[_kind(e)] = kinds.get(_kind(e), 0) + 1
        ours = sorted({m.id for _, m, _ in rows})
        times = [t for _, _, t in rows if t is not None]
        when = (", already in contact" if min(times) < 1 else f", first contact in ~{min(times):.0f}s") if times else ""
        massing.append((men, f"ENEMY MASSING against your {side} part (your #"
                             + ", #".join(str(i) for i in ours) + "): "
                             + " + ".join(f"{n} {k}" for k, n in sorted(kinds.items()))
                             + f" ({men} men, {men * 100 // total_men}% of his army){when}"))

    # 9. our units that will have nothing to do while others fight
    fight_pts = [(u.x, u.y) for u in mine if u.action == "melee"]
    fight_pts += [_at(tracks[by_id[uid].target], t) for uid, t in contact.items()
                  if by_id[uid].alliance != ai and t <= HORIZON_S]
    idle = []
    if fight_pts:
        for u in mine:
            if u.category not in ("infantry", "cavalry") or u.action in ("melee", "routing") or u.id not in tracks:
                continue
            far = all(math.dist(tracks[u.id][k], tracks[f.id][k]) > IDLE_M
                      for f in foes if f.id in tracks for k in range(0, int(10.0 / STEP_S) + 1, 2))
            if far and not any(f.target == u.id for f in foes):
                d = min(math.dist((u.x, u.y), p) for p in fight_pts)
                if d > 100:
                    idle.append((d, u))
    idle_line = ("IDLE while others fight: " + ", ".join(f"#{u.id} ({_kind(u)}, {d:.0f} m from the fight)"
                                                         for d, u in sorted(idle, key=lambda p: p[0]))
                 + f" - no enemy within {IDLE_M:.0f} m of them for the next 10 s") if idle else None

    # 7. who is losing men fast (trend of the last 15 s)
    trends = []
    for u in mine + foes:
        lost = state.recent_losses.get(u.id, 0)
        rate = lost / LOSS_WINDOW_S
        if rate >= 0.5 and u.soldiers > 0:
            left = max(0, round(u.soldiers - rate * 10))
            who = "your" if u.alliance == ai else "enemy"
            trends.append((rate, f"{who} #{u.id} loses ~{rate:.1f} men/s ({u.soldiers} left) - ~{left} in 10s at this rate"))

    # 5. where the moving enemy units will be
    moving = sorted((e for e in foes if (e.action in ("moving", "running") or e.id in trended) and e.id in tracks
                     and math.dist(tracks[e.id][0], _at(tracks[e.id], SNAPSHOTS[-1])) > 10),
                    key=lambda e: min(math.dist((e.x, e.y), (m.x, m.y)) for m in mine))[:MAX_TRACKS]
    out = []
    events.sort(key=lambda p: p[0])
    ranges.sort(key=lambda p: p[0])
    out += ["  " + text for _, text in sorted(massing, reverse=True)[:3]]
    out += ["  " + text for _, text in events[:MAX_CONTACTS]]
    out += ["  " + text for _, text in ranges[:MAX_RANGE_EVENTS]]
    out += ["  " + t for t in threats]
    for e in moving:
        snaps = " ".join(f"{s:.0f}s ({_at(tracks[e.id], s)[0]:.0f},{_at(tracks[e.id], s)[1]:.0f})" for s in SNAPSHOTS)
        guess = " (no known order: if it keeps its course of the last 10 s)" if e.id in trended else ""
        out.append(f"  enemy #{e.id} ({_kind(e)}) at {snaps}{guess}")
    out += ["  intercept: " + t for t in intercepts]
    if idle_line:
        out.append("  " + idle_line)
    out += ["  trend: " + t for _, t in sorted(trends, reverse=True)[:6]]
    out += ["  trend: " + t for t in supplies[:6]]
    out += ["  risk: " + t for _, t in sorted(risks)[:6]]
    if not out:
        return []
    return ["\nPREDICTED (if every unit keeps its current order - an enemy unit whose order is not known keeps its course"
            " of the last 10 s; fights and morale are not predicted):"] + out


# --- calibration: how good were the predictions? --------------------------------------------------------

def snapshot(state: State, start: BattleStart | None) -> dict:
    """What we predict now, to be checked later: positions of moving units in 5 and 10 s, and contacts."""
    tracks, contact = simulate(state, start)
    moving = [u.id for u in state.units if u.action in ("moving", "running") and u.id in tracks]
    return {
        "t": state.t,
        "pos": {h: {i: _at(tracks[i], h) for i in moving} for h in (5.0, 10.0)},
        "contacts": [(uid, next(u.target for u in state.units if u.id == uid), t) for uid, t in contact.items()
                     if t <= 10.0],
    }


def check(snap: dict, state: State, horizon: float) -> dict | None:
    """Compare the snapshot's positions at `horizon` and the contacts due by now with what happened."""
    by_id = {u.id: u for u in state.units}
    errs = sorted(math.dist(p, (by_id[i].x, by_id[i].y)) for i, p in snap["pos"][horizon].items()
                  if i in by_id and by_id[i].action != "dead")
    hits = total = 0
    for a, b, t in snap["contacts"]:
        if t > horizon or a not in by_id or b not in by_id:
            continue
        total += 1
        ua, ub = by_id[a], by_id[b]
        hits += int((ua.action == "melee" and ua.target == b) or math.dist((ua.x, ua.y), (ub.x, ub.y)) <= 30)
    if not errs and not total:
        return None
    return {"made_at": snap["t"], "horizon_s": horizon, "units": len(errs),
            "mean_err_m": round(sum(errs) / len(errs), 1) if errs else None,
            "median_err_m": round(errs[len(errs) // 2], 1) if errs else None,
            "contacts_predicted": total, "contacts_happened": hits}
