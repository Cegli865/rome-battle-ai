"""Turn raw battle state into compact text an LLM can reason about.

LLMs are bad at raw coordinate geometry, so besides positions we give
relative facts: which side of our line an enemy is on, distances to the
nearest threat, and per-unit condition in words.
"""

from __future__ import annotations

import math

from .geometry import SURROUNDED_M, alive, bearing, centroid, compass, dist, nearest, surrounded
from .protocol import BattleStart, State, Unit


UNDER_FIRE_NO_ENEMY_WITHIN = 40.0  # m; losses with no enemy this close = missiles or artillery
# BattleStart.battle_type is the game's conflict type: SuccessfulAmbush, FailedAmbush, Normal, Siege, SallyBesieger,
# Naval, Withdraw (in that order; seen in logs: 0 ambush, 2 field battle, 3 siege, 4 sally)
BATTLE_SUCCESSFUL_AMBUSH = 0
MOVING_MPS = 0.7  # slower than this counts as standing


_CAT = {"infantry": "inf", "cavalry": "cav", "missile": "msl", "siege": "art", "general": "GENERAL", "other": "oth"}
_CLS = {"heavy": "hvy", "light": "lgt", "missile": "msl", "spearmen": "spear", "skirmish": "skrm", "other": ""}


# the game's special ability types (UNIT 0x21fc1c0; names -> type in 0x1b3b580); 4 drop_engines is not sent
ABILITY_NAMES = {1: "testudo", 2: "phalanx", 3: "wedge", 5: "flaming ammo", 6: "warcry", 7: "druid chant",
                 8: "curse", 9: "berserk", 10: "rally", 11: "kill elephants", 12: "move and shoot",
                 13: "cantabrian circle", 14: "shield wall", 15: "stealth", 16: "feigned rout", 17: "schiltrom"}

def _cond(u: Unit, lost: int = 0, nearest_foe_m: float = 1e9, stuck: bool = False,
          goal: tuple[float, float] | None = None, velocity: tuple[float, float] | None = None) -> str:
    """Compact condition: '80/80 m90' + only what is notable."""
    parts = [f"{u.soldiers}/{u.max_soldiers} m{u.morale * 100:.0f}"]
    if lost > 0:
        under_fire = u.action not in ("melee", "routing") and nearest_foe_m > UNDER_FIRE_NO_ENEMY_WITHIN
        parts.append(f"LOST {lost}" + (" UNDER FIRE" if under_fire else ""))
    if u.fatigue > 0.5:
        parts.append("tired" if u.fatigue < 0.8 else "exhausted")
    if u.ammo is not None and u.ammo < 0.995:
        parts.append(f"ammo {u.ammo:.0%}")
    if u.formation in ("phalanx", "schiltrom", "testudo", "shield_wall", "wedge") \
            and not (u.ability_on and ABILITY_NAMES.get(u.ability, "").replace(" ", "_") == u.formation):
        parts.append(u.formation.upper())
    from .units_db import frontage_m

    fr = frontage_m(u)
    if fr:
        parts.append(f"front {fr:.0f}m ({u.width_men} men)")
    if u.rally == 1:
        parts.append("RALLY ON")
    elif u.rally == 0:
        parts.append("rally off")
    what = ABILITY_NAMES.get(u.ability, "ability")
    if u.ability == 10:  # the general's rally: shown above
        pass
    elif u.ability_on and u.ability in (7, 8):
        # druid chant / screeching women's curse: the order only toggles a mode (UNIT +0xf7a) and halts the unit;
        # the game makes it chant / curse only while it stands idle, so walking or fighting means no effect
        what_on, doing, effect = (("chant", "CHANTING", "CHANTING (nearby friendly units get the morale boost)")
                                  if u.ability == 7 else
                                  ("curse", "CURSING", "CURSING (nearby enemy units lose morale)"))
        if u.action in ("moving", "running"):
            parts.append(f"{what_on} switched on but NOT {doing} while moving - no effect until they stand still")
        elif u.action == "melee":
            parts.append(f"{what_on} switched on but NOT {doing} - fighting in the melee themselves")
        else:
            parts.append(effect)
    elif u.ability_on:
        # the game does not let a unit in phalanx / testudo / schiltrom run (UNIT vfunc 0x200)
        parts.append(f"{what.upper()} ON" + (" (cannot run)" if u.ability == 1 else ""))
    elif u.ability in (2, 17):
        parts.append(f"{what} off")  # phalanx / schiltrom: the executor switches it (off while running)
    elif u.ability_ready and u.ability != 1:  # testudo is switched off for now (executor.TESTUDO_ENABLED)
        parts.append(f"{what} ready")
    elif u.ability:
        parts.append(f"{what} not ready")
    if u.category == "siege":  # artillery: how many of its machines are still there
        parts.append(f"{u.engines} machine{'s' if u.engines != 1 else ''}" + (" - ALL DESTROYED, harmless" if u.engines == 0 else ""))
    if u.engines > 0 and u.category != "siege":  # artillery reports its own engine here, not carried equipment
        kind = {0: "A RAM", 1: "LADDERS", 2: "A SIEGE TOWER", 3: "A SAP POINT"}.get(u.engine_type, "SIEGE EQUIPMENT")
        parts.append(f"CARRIES {kind}")
    if u.game_ai and u.category == "siege":
        parts.append("COMMANDED BY THE GAME'S AI (siege battle: it aims and fires the artillery itself; your orders "
                     "are ignored until its ammunition runs out or its machines are gone - then the unit is yours)")
    elif u.game_ai:
        parts.append("COMMANDED BY THE GAME'S AI (it uses the siege equipment; your orders are ignored until the "
                     "equipment is used, dropped or destroyed - then the unit is yours)")
    if u.action != "idle":
        parts.append(u.action + (f">#{u.target}" if u.target is not None else ""))
    if velocity is not None and math.hypot(*velocity) >= MOVING_MPS:
        word = "" if u.action in ("moving", "running") else "going "  # "moving 173° 4m/s", "melee going 90° 3m/s"
        parts.append(f"{word}{bearing(*velocity)}° {math.hypot(*velocity):.0f}m/s")
    parts.append(f"facing {round(u.heading) % 360}°")
    if goal is not None:
        parts.append(f"going to ({goal[0]:.0f},{goal[1]:.0f}), {((goal[0] - u.x) ** 2 + (goal[1] - u.y) ** 2) ** 0.5:.0f}m left")
    if stuck:
        parts.append("STUCK (not carrying out its order: no progress for 12 s)")
    return " ".join(parts)


CHANT_REACH_M = 50.0  # rough reach of the druid chant / screeching women's curse (not read from the game)


def _chant_reach(u: Unit, enemies: list[Unit], friends: list[Unit]) -> str:
    """A chanting / cursing unit far from where it matters: say so, the effect then reaches nobody."""
    if not (u.ability_on and u.ability in (7, 8)) or u.action in ("moving", "running", "melee"):
        return ""
    if u.ability == 7:
        fighting = [f for f in friends if f.id != u.id and f.ability not in (7, 8) and f.category not in ("siege", "missile")
                    and (f.action == "melee" or any(dist(f, e) < 60 for e in enemies))]
        if not fighting:
            return ""
        f = nearest(u, fighting)
        if dist(u, f) <= CHANT_REACH_M:
            return ""
        return (f" - BUT TOO FAR: the nearest of your units in combat (#{f.id}) is {dist(u, f):.0f} m away, the chant "
                f"reaches only about {CHANT_REACH_M:.0f} m - move them to 20-40 m behind your fighting units")
    e = nearest(u, enemies)
    if e is None or dist(u, e) <= CHANT_REACH_M:
        return ""
    return (f" - BUT TOO FAR: the nearest enemy is {dist(u, e):.0f} m away, the curse reaches only about "
            f"{CHANT_REACH_M:.0f} m - move them closer to the enemy line (out of the melee)")


def _line(u: Unit, enemies: list[Unit], lost: int = 0, stuck: bool = False,
          goal: tuple[float, float] | None = None, velocity: tuple[float, float] | None = None,
          friends: list[Unit] | None = None) -> str:
    e = nearest(u, enemies)
    kind = "/".join(k for k in (_CAT.get(u.category, u.category), _CLS.get(u.cls, u.cls)) if k)
    s = f"#{u.id} {u.name} {kind} ({u.x:.0f},{u.y:.0f}) " + _cond(u, lost, dist(u, e) if e is not None else 1e9, stuck, goal, velocity)
    if friends is not None:
        s += _chant_reach(u, enemies, friends)
    if e is not None:
        s += f"; foe #{e.id} {dist(u, e):.0f}m {compass(e.x - u.x, e.y - u.y)}"
    from .units_db import missile_range

    rng = missile_range(u.name)
    if rng and e is not None and (u.ammo is None or u.ammo > 0) and (u.category != "siege" or u.engines > 0):
        n_in = sum(1 for f in enemies if dist(u, f) <= rng)
        s += (f"; range {rng:.0f}m - " + (f"{n_in} enem{'y' if n_in == 1 else 'ies'} in range" if n_in
                                           else f"OUT OF RANGE (nearest foe {dist(u, e):.0f}m)"))
    if goal is None and u.dest is not None:
        d = math.hypot(u.dest[0] - u.x, u.dest[1] - u.y)
        if d > 10:
            s += (f"; ORDERED TO ({u.dest[0]:.0f},{u.dest[1]:.0f}) {d:.0f}m {compass(u.dest[0] - u.x, u.dest[1] - u.y)}"
                  f" at a {'RUN' if u.order_run else 'walk'}")
    if surrounded(u, enemies):
        s += f"; SURROUNDED (enemies on all sides within {SURROUNDED_M:.0f} m - no way out)"
    attackers = [f for f in enemies if f.target == u.id]
    if attackers:
        s += "; TARGETED BY " + ", ".join(
            f"#{f.id} {_CAT.get(f.category, f.category)} {dist(u, f):.0f}m {f.action}"
            + (" at a RUN" if f.order_run and f.action not in ("melee", "running") else "") for f in attackers)
    return s


def _off_field(state: State, ai: int, own: bool) -> list[str]:
    """Units that are not on the battlefield: reinforcements still to come, or units that have left it."""
    us = [u for u in state.off_field if (u.alliance == ai) == own]
    out = []
    coming = [u for u in us if u.id not in state.left_field]
    left = [u for u in us if u.id in state.left_field]
    if coming:
        out.append("  NOT ON THE BATTLEFIELD YET (reinforcements, they arrive later - cannot be ordered or attacked "
                   "until they appear): " + ", ".join(f"#{u.id} {u.name} {u.soldiers} men" for u in coming))
    if left:
        out.append("  LEFT THE BATTLEFIELD (withdrawn): " + ", ".join(f"#{u.id} {u.name} {u.soldiers} men" for u in left))
    return out


UNIT_LEGEND = (
    "Unit lines: #id name kind (x,y) men/max mMORALE% [LOST n = men lost in the last 15 s, UNDER FIRE = losses "
    "without an enemy within 40 m] [formation] [action>#target]; foe = nearest enemy, distance and direction; "
    "TARGETED BY = enemy units whose current order is to attack this unit (distance, what they are doing). "
    "SURROUNDED = enemies on all sides close by, there is no free side to get away through. "
    "range = the reach of the unit's missile weapon and how many enemies are within it (OUT OF RANGE = none). "
    "ORDERED TO = where the unit has been ordered to go (for enemy units: the enemy player's own order - where "
    "he is sending it, at a walk or a RUN), distance and direction from the unit. "
    "kind: inf/cav/msl(missile)/art(artillery)/GENERAL, spear/hvy/lgt/skrm. "
    "facing D° = where the unit's front faces; moving / running / going D° Nm/s = the direction it is actually "
    "moving in and its speed (it can differ from facing, e.g. a unit backing away). Degrees: 0 = north, 90 = east, "
    "180 = south, 270 = west, clockwise - the same as facing_deg in your plan."
)


CROSSING_KINDS = {1: "BRIDGE", 4: "FORD"}  # BATTLE_MAP_AREA_LINK types the game's own AI treats as river crossings
BLOCK_M = 15.0  # a blocking unit stands this far beyond the end of a crossing, on its own bank


def river_crossings(links: list | None) -> list[dict]:
    return [c for c in links or [] if c.get("kind") in CROSSING_KINDS]


def _bank(c: dict, x: float, y: float) -> int:
    """Which bank of the river (as crossing c sees it) a point is on: +1 = the p2 side, -1 = the p1 side."""
    (x1, y1), (x2, y2) = c["p1"], c["p2"]
    return 1 if (x - (x1 + x2) / 2) * (x2 - x1) + (y - (y1 + y2) / 2) * (y2 - y1) >= 0 else -1


def crossing_ends(c: dict, ai_alliance: int, state: State, banks: dict | None = None,
                  key: int = 0) -> tuple[tuple, tuple, float, bool]:
    """(your-bank exit, enemy-bank exit, facing from your exit towards the crossing, armies on opposite banks).
    The banks are those the armies stood on when first asked (kept in `banks`): later the enemy may be across."""
    if banks is not None and key in banks:
        mb, eb = banks[key]
    else:
        mine = alive([u for u in state.units if u.alliance == ai_alliance])
        foes = alive([u for u in state.units if u.alliance != ai_alliance])
        mb = _bank(c, *centroid(mine)) if mine else 1
        eb = _bank(c, *centroid(foes)) if foes else -mb
        if banks is not None:
            banks[key] = (mb, eb)
    p1, p2 = tuple(c["p1"]), tuple(c["p2"])
    own, other = (p2, p1) if mb > 0 else (p1, p2)
    facing = math.degrees(math.atan2(other[0] - own[0], other[1] - own[1])) % 360
    return own, other, facing, mb != eb


def _block_point(own: tuple, other: tuple) -> tuple[float, float]:
    dx, dy = own[0] - other[0], own[1] - other[1]
    n = math.hypot(dx, dy) or 1.0
    return own[0] + dx / n * BLOCK_M, own[1] + dy / n * BLOCK_M


def describe_crossings(links: list, ai_alliance: int, state: State, banks: dict | None = None) -> str:
    """River crossings from the game's map areas: the only places where units can get over the river."""
    rows = river_crossings(links)
    if not rows:
        return ""
    out = [f"RIVER: a river crosses the battlefield. Units can get over it ONLY at these {len(rows)} crossing(s) "
           "(everywhere else the river is impassable). C1, C2... name them in every report:"]
    for i, c in enumerate(rows, 1):
        own, other, facing, split = crossing_ends(c, ai_alliance, state, banks, i)
        bx, by = _block_point(own, other)
        length = math.hypot(other[0] - own[0], other[1] - own[1])
        sides = ("exit on YOUR bank" if split else "end A") + f" ({own[0]:.0f},{own[1]:.0f}), " + \
                ("exit on the ENEMY bank" if split else "end B") + f" ({other[0]:.0f},{other[1]:.0f})"
        out.append(f"  C{i}: {CROSSING_KINDS[c['kind']]}, {length:.0f} m long, {c.get('width', 0):.0f} m wide; {sides}"
                   + (f"; to BLOCK it stand at ({bx:.0f},{by:.0f}) facing {facing:.0f} deg" if split else ""))
    if not any(crossing_ends(c, ai_alliance, state, banks, i)[3] for i, c in enumerate(rows, 1)):
        out.append("  Both armies start on the same bank: the river only limits where you can go.")
    out.append("A unit on a bridge or in a ford is in a narrow column and cannot form a line: whoever holds the exit "
               "on his own bank fights it a few men at a time. Each report says who can reach which exit first.")
    return "\n".join(out)


def _eta(u, x: float, y: float, can_run: bool = False) -> float:
    """Seconds for unit u to get to (x, y): at a run, or walking when it cannot run now (phalanx and the like)
    unless can_run (the formation switched off first)."""
    walk_only = u.ability_on and u.ability in (1, 2, 17) and not can_run
    v = (u.walk_speed if walk_only else u.run_speed) or u.walk_speed or 1.5
    return math.hypot(u.x - x, u.y - y) / v


def crossings_now(start: BattleStart, state: State) -> list[str]:
    """Per report: who gets to each crossing's exit on your bank first, and who is on the crossing now."""
    rows = river_crossings(start.crossings)
    if not rows:
        return []
    ai = start.ai_alliance
    mine = alive([u for u in state.units if u.alliance == ai])
    foes = alive([u for u in state.units if u.alliance != ai])
    out = ["\nRIVER CROSSINGS NOW:"]
    for i, c in enumerate(rows, 1):
        own, other, _, split = crossing_ends(c, ai, state, start.crossing_banks, i)
        if not split:
            continue
        (x1, y1), (x2, y2) = own, other
        seg = max(math.hypot(x2 - x1, y2 - y1), 1.0)
        on = [u for u in foes if _near_segment((u.x, u.y), own, other, c.get("width", 10) / 2 + 8)]
        crossed = [u for u in foes if _bank(c, u.x, u.y) == _bank(c, *own) and u not in on]
        parts = []
        inf = [u for u in mine if u.category == "infantry"]
        for who, us in (("yours", mine), ("your infantry", inf), ("enemy", [u for u in foes if u not in crossed])):
            if us:
                u = min(us, key=lambda u: _eta(u, *own))
                if who == "your infantry" and u is min(mine, key=lambda u: _eta(u, *own)):
                    continue
                t, t_run = _eta(u, *own), _eta(u, *own, can_run=True)
                parts.append(f"first of {who}: #{u.id} in ~{t:.0f} s"
                             + (f" (~{t_run:.0f} s with its formation off, at a run)" if t_run < t - 3 else ""))
        line = f"  C{i} exit on your bank ({x1:.0f},{y1:.0f}): " + ", ".join(parts)
        if on:
            line += "; ON THE CROSSING NOW: " + ", ".join(f"#{u.id}" for u in on)
        if crossed:
            line += f"; enemy units already over on your bank: {len(crossed)}"
        out.append(line)
    return out if len(out) > 1 else []


def _near_segment(p, a, b, r: float) -> bool:
    ax, ay = a
    dx, dy = b[0] - ax, b[1] - ay
    t = max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / (dx * dx + dy * dy or 1)))
    return math.hypot(p[0] - ax - t * dx, p[1] - ay - t * dy) <= r


def describe_static(start: BattleStart, state: State) -> str:
    """The parts that don't change during a battle (terrain, town plan). Built once, sent in the cached
    system prompt instead of every request."""
    if start.map.playable:
        hx, hy = start.map.playable
        area = (f"Playable battlefield: x from {-hx:.0f} to {hx:.0f}, y from {-hy:.0f} to {hy:.0f} - units cannot "
                     "go beyond it.")
    else:
        area = ""
    if start.can_withdraw is True:
        area += " WITHDRAWAL IS POSSIBLE in this battle: task=withdraw takes units off the battlefield."
    elif start.can_withdraw is False:
        area += (" WITHDRAWAL IS NOT POSSIBLE in this battle (the game does not allow your army to leave): units "
                 "can only fight - do not use task=withdraw and do not try to march off the map.")
    lines = [
        f"Map {start.map.width:.0f}x{start.map.height:.0f} m, x = east, y = north. " + area + " "
        f"You are {'DEFENDING' if start.player_is_attacker else 'ATTACKING'}.",
        UNIT_LEGEND,
    ]
    from .units_db import describe_battle_units

    for side, title in ((start.ai_alliance, "YOUR"), (None, "ENEMY")):
        units = [u for u in state.units if (u.alliance == side if side is not None else u.alliance != start.ai_alliance)]
        armies: dict[int, list] = {}
        for u in units:
            armies.setdefault(u.id // 100 % 100, []).append(u)  # id = (alliance + 1) * 10000 + army * 100 + n
        if len(armies) > 1:
            parts = []
            for k, us in sorted(armies.items()):
                cx, cy = centroid(us)
                parts.append(f"army {k + 1}: {len(us)} units around ({cx:.0f},{cy:.0f}) - #" + ", #".join(str(u.id) for u in us))
            lines.append(f"{title} FORCES ARE {len(armies)} SEPARATE ARMIES (main army and reinforcements, starting in "
                         "different places): " + "; ".join(parts))

    types = describe_battle_units(state.units)
    if types:
        lines.append(types)
    if start.map.settlement:
        lines.append("This is a SETTLEMENT battle: fighting happens in streets, walls and gates.")
    if start.map.notes:
        lines.append(f"Terrain: {start.map.notes}")
    if start.terrain is not None:
        from .terrain import describe_terrain

        lines.append("\n" + describe_terrain(start.terrain, start.ai_alliance, state, start.masks, show_units=False))
    if start.crossings:
        river = describe_crossings(start.crossings, start.ai_alliance, state, start.crossing_banks)
        if river:
            lines.append("\n" + river)
    from . import town_graph

    g = town_graph.for_battle(start)
    if g is not None:
        lines.append("\n" + town_graph.describe(g))
    elif start.masks is not None:
        from .terrain import describe_town, town_bbox

        bbox = town_bbox(start.masks)
        near = bbox is not None and any(
            bbox[0] - 300 <= u.x <= bbox[2] + 300 and bbox[1] - 300 <= u.y <= bbox[3] + 300 for u in state.units
        )
        if start.map.settlement or near:
            lines.append("\n" + describe_town(
                start.masks, start.ai_alliance, state, out_chokepoints=start.chokepoints,
                plaza_xy=(start.map.plaza[0], start.map.plaza[1]) if start.map.plaza else None, show_units=False,
            ))
    if start.map.plaza:
        start.plaza = (start.map.plaza[0], start.map.plaza[1])
    return "\n".join(lines)


def describe(start: BattleStart, state: State, terrain=None, static: bool = True) -> str:
    """Current situation. With static=False the terrain/town maps are left out (they're in start.briefing)."""
    ai = start.ai_alliance
    mine = [u for u in state.units if u.alliance == ai]
    theirs = [u for u in state.units if u.alliance != ai]
    mine_alive, theirs_alive = alive(mine), alive(theirs)

    mx, my = centroid(mine_alive)
    ex, ey = centroid(theirs_alive)
    role = (
        "YOU ARE THE DEFENDER - but battles are almost never decided by the time limit: win the fight, and "
        "answer every move the enemy makes."
        if start.player_is_attacker
        else "YOU ARE THE ATTACKER: you have to beat his army - waiting gains you nothing."
    )
    if start.battle_type == BATTLE_SUCCESSFUL_AMBUSH:
        # the game's attacker flag names the army that marched on the campaign map, i.e. the one that got ambushed
        role = ("AMBUSH - YOU SET IT: the enemy army walked into your ambush while on the march. Strike now with "
                "everything, before it can form up - waiting throws the ambush away."
                if start.player_is_attacker else
                "AMBUSH - YOU WERE AMBUSHED on the march: form up fast and fight your way out, or break through.")
    lines = [
        f"Battle time {state.t:.0f}s. {role}",
        f"Your army centre ({mx:.0f},{my:.0f}); enemy centre ({ex:.0f},{ey:.0f}), "
        f"{dist_xy(mx, my, ex, ey):.0f}m to the {compass(ex - mx, ey - my)}.",
    ]
    if static:
        lines.insert(0, f"Map {start.map.width:.0f}x{start.map.height:.0f}m, x=east, y=north. "
                        f"You are {'defending' if start.player_is_attacker else 'attacking'}. {UNIT_LEGEND}")
    if static and start.map.settlement:
        lines.append("This is a SETTLEMENT battle: fighting happens in streets, walls and gates.")
    if static and start.map.notes:
        lines.append(f"Terrain: {start.map.notes}")

    lines.append(f"\nYOUR UNITS ({len(mine_alive)} fighting):")
    lines += ["  " + _line(u, theirs_alive, state.recent_losses.get(u.id, 0), u.id in state.stuck,
                           state.goals.get(u.id), state.velocity.get(u.id), friends=mine_alive) for u in mine_alive]
    gone = [u for u in mine if u not in mine_alive]
    if gone:
        lines.append("  out of action: " + ", ".join(f"#{u.id} {u.name} ({u.action})" for u in gone))
    lines += _off_field(state, start.ai_alliance, True)

    lines.append(f"\nENEMY UNITS ({len(theirs_alive)} fighting):")
    lines += ["  " + _line(u, mine_alive, state.recent_losses.get(u.id, 0), velocity=state.velocity.get(u.id))
              for u in theirs_alive]
    gone = [u for u in theirs if u not in theirs_alive]
    if gone:
        lines.append("  out of action: " + ", ".join(f"#{u.id} {u.name} ({u.action})" for u in gone))
    lines += _off_field(state, start.ai_alliance, False)
    lines += crossings_now(start, state)
    from .predict import predictions

    pred = predictions(start, state)
    lines += pred
    lines += state.reflex_status
    threat = any(w in line for line in pred for w in ("FLANK", "REAR", "BEHIND YOUR LINE"))
    if threat and not any("waiting for" in line for line in state.reflex_status):
        lines.append("NO REFLEX IS WAITING for the threats PREDICTED above: without one, your army answers them only "
                     "in your next plan, seconds too late. Give the groups that should answer them reflexes now "
                     "(e.g. a reserve that attacks the enemy who engages your line, units that turn or fall back "
                     "when enemy cavalry comes near).")
    from . import town_graph

    g = start.town_graph
    if g is not None:
        lines.append("ON THE TOWN GRAPH - yours: " + "; ".join(
            town_graph.unit_positions(g, mine_alive, start.buildings, True, attacker=not start.player_is_attacker,
                                     velocity=state.velocity)))
        lines.append("ON THE TOWN GRAPH - enemy: " + "; ".join(
            town_graph.unit_positions(g, theirs_alive, start.buildings, False, velocity=state.velocity)))
        lines.append(town_graph.status(g, start.buildings, start.ai_alliance))
        if not start.player_is_attacker:  # we attack the town
            routes = town_graph.entrance_routes(g, start.buildings, start.ai_alliance)
            if routes:
                lines.append(routes)
    if g is None and start.masks is not None and start.map.settlement and not start.player_is_attacker and start.map.plaza:
        from .terrain import exit_routes

        inf = [u for u in mine_alive if u.category in ("infantry", "general")] or mine_alive
        cx, cy = centroid(inf)
        if start.routes_from is None or dist_xy(cx, cy, *start.routes_from) > 120:  # recompute when the army moved
            goal = (start.map.plaza[0], start.map.plaza[1])
            found = exit_routes(start.masks, (cx, cy), goal)
            start.routes = [(f"E{i + 1}", p, stg) for i, (p, stg, _b, _l) in enumerate(found)]
            start.routes_from = (cx, cy)
        if start.routes:
            def side(sx, sy):
                return compass(sx - start.map.plaza[0], sy - start.map.plaza[1])

            lines.append("EXITS of the town square (streets leading onto it; route=E1.. walks a group along that street): "
                         + "; ".join(f"{lab} the {side(*stg)} street, gather at ({stg[0]:.0f},{stg[1]:.0f}), "
                                     f"{sum(dist_xy(*a, *b) for a, b in zip(p, p[1:])):.0f} m march" for lab, p, stg in start.routes))
    if start.map.settlement and start.map.plaza:
        px, py = start.map.plaza[0], start.map.plaza[1]
        ours = [u for u in mine_alive if dist_xy(u.x, u.y, px, py) < 40]
        theirs_on = [u for u in theirs_alive if dist_xy(u.x, u.y, px, py) < 60]
        if theirs_on and start.player_is_attacker:
            lines.append(f"!!! ENEMY ON THE TOWN SQUARE ({px:.0f},{py:.0f}): "
                         + ", ".join(f"#{u.id} {u.name}" for u in theirs_on)
                         + " - if they hold it the town falls. Retake it NOW with everything close.")
        elif start.player_is_attacker:
            lines.append(f"Town square ({px:.0f},{py:.0f}): " + (f"held by #{ours[0].id}" if ours else "EMPTY - put a unit on it"))
        else:
            lines.append(f"Town square ({px:.0f},{py:.0f}): " + ("enemy there" if theirs_on else "free")
                         + ("; you hold it" if ours else ""))
    if g is None and start.buildings and start.map.settlement and not start.player_is_attacker:
        from .siege import describe_buildings

        d = describe_buildings(start.buildings, (mx, my))
        if d:
            lines.append("\n" + d)
    if not static:
        return "\n".join(lines)
    if terrain is not None:
        from .terrain import describe_terrain

        lines.append("\n" + describe_terrain(terrain, start.ai_alliance, state, start.masks))
    if start.masks is not None:
        from .terrain import describe_town, town_bbox

        bbox = town_bbox(start.masks)
        near = bbox is not None and any(
            bbox[0] - 300 <= u.x <= bbox[2] + 300 and bbox[1] - 300 <= u.y <= bbox[3] + 300 for u in state.units
        )
        if start.map.settlement or near:
            lines.append("\n" + describe_town(
                start.masks, start.ai_alliance, state, out_chokepoints=start.chokepoints,
                plaza_xy=(start.map.plaza[0], start.map.plaza[1]) if start.map.plaza else None,
            ))
            if start.map.plaza:
                start.plaza = (start.map.plaza[0], start.map.plaza[1])
    return "\n".join(lines)


def dist_xy(ax: float, ay: float, bx: float, by: float) -> float:
    return ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5
