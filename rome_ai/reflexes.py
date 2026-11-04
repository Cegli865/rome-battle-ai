"""Conditional orders from the plan (GroupOrder.reflexes): the executor checks every event twice a second and,
when one happens, switches the group to the reflex's order until the next plan - without asking the general.

A reflex: ally_target (whose units are watched on our side: the group itself, any, listed units, named groups),
enemy_target (the same for the enemy), event (what has to happen between them), order (what the group does then).
"""

from __future__ import annotations

import math

from .geometry import centroid, dist
from .plan import GroupOrder, Plan, Reflex, Target
from .protocol import BattleStart, State, Unit

ZONE_CROSSING_EXIT_M = 25.0
ZONE_NODE_M = 25.0
ZONE_PLAZA_M = 30.0
ENGAGED_M = 25.0
ARRIVED_M = 15.0
FALL_BACK_M = 60.0


def _fighting(u: Unit) -> bool:
    return u.on_field and u.soldiers > 0 and u.action not in ("dead", "routing")


class ReflexEngine:
    def __init__(self, start: BattleStart) -> None:
        self.start = start
        self.new_plan(Plan(intent="", note="", groups=[]))

    def new_plan(self, plan: Plan) -> None:
        self.plan = plan
        self.plan_t: float | None = None  # battle time of the first tick of this plan
        self.fired: dict[tuple[str, str], tuple[float, int | None, str]] = {}  # (group, label) -> (t, enemy, why)
        self.overrides: dict[str, GroupOrder] = {}  # group name -> the order it carries out now
        self.named: dict[str, set[int]] = {}
        for g in plan.groups:
            self.named.setdefault(g.name, set()).update(g.unit_ids)
        for ug in plan.unit_groups or []:
            self.named.setdefault(ug.name, set()).update(ug.units)
        self._sim_t: float | None = None
        self._sim = None

    # --- selection -----------------------------------------------------------

    @staticmethod
    def all_reflexes(g: GroupOrder) -> list[tuple[str, Reflex]]:
        """The group's reflexes with a label: its look-ahead ones ('2s', '5s', '10s') and the extra ones."""
        out = [(f"{la.in_s}s", la.reflex) for la in g.lookahead or [] if la.reflex is not None]
        out += [(f"#{n}", r) for n, r in enumerate(g.reflexes or [], 1)]
        return out

    def _select(self, t: Target, side_units: list[Unit], own_group: GroupOrder) -> list[Unit]:
        if t.kind == "self":
            ids = set(own_group.unit_ids)
            return [u for u in side_units if u.id in ids]
        if t.kind == "units":
            ids = set(t.units or [])
            return [u for u in side_units if u.id in ids]
        if t.kind == "groups":
            ids = set().union(*(self.named.get(n, set()) for n in (t.groups or []))) if t.groups else set()
            return [u for u in side_units if u.id in ids]
        return list(side_units)

    # --- zones ---------------------------------------------------------------

    def _in_zone(self, zone: str, u: Unit, state: State) -> bool:
        z = zone.strip()
        if z.upper() == "P":
            p = self.start.plaza or (tuple(self.start.map.plaza[:2]) if self.start.map.plaza else None)
            return p is not None and math.dist((u.x, u.y), p) <= ZONE_PLAZA_M
        if z[:1].upper() == "C" and z[1:].isdigit():
            from .describe import _near_segment, crossing_ends, river_crossings

            rows = river_crossings(self.start.crossings)
            i = int(z[1:])
            if not 1 <= i <= len(rows):
                return False
            c = rows[i - 1]
            own, other, _, _ = crossing_ends(c, self.start.ai_alliance, state, self.start.crossing_banks, i)
            return (math.dist((u.x, u.y), own) <= ZONE_CROSSING_EXIT_M
                    or _near_segment((u.x, u.y), own, other, c.get("width", 10) / 2 + 8))
        if z.count(",") == 2:
            try:
                x, y, r = (float(v) for v in z.split(","))
            except ValueError:
                return False
            return math.dist((u.x, u.y), (x, y)) <= r
        g = self.start.town_graph
        if g is not None and z in getattr(g, "nodes", {}):
            n = g.nodes[z]
            return math.dist((u.x, u.y), (n.x, n.y)) <= ZONE_NODE_M
        return False

    # --- events --------------------------------------------------------------

    def _simulated(self, state: State):
        if self._sim_t != state.t:
            from .predict import simulate

            self._sim = simulate(state, self.start)
            self._sim_t = state.t
        return self._sim

    def _contacts(self, state: State) -> dict[int, float]:
        return self._simulated(state)[1]

    def _expected(self, r: Reflex, g: GroupOrder, state: State) -> float | None:
        """When a waiting reflex is expected to fire if every unit keeps its order (near / in_zone /
        predicted_contact only), in seconds from now."""
        from types import SimpleNamespace

        from .predict import STEP_S

        e = r.event
        if e.type not in ("near", "in_zone", "predicted_contact"):
            return None
        ai = self.start.ai_alliance
        mine = self._select(r.ally_target, [u for u in state.units if u.alliance == ai], g)
        theirs = self._select(r.enemy_target, [u for u in state.units if u.alliance != ai and _fighting(u)], g)
        tracks, contact = self._simulated(state)
        if e.type == "predicted_contact":
            ids = {a.id for a in mine}
            ts = [t for f in theirs if (t := contact.get(f.id)) is not None and f.target in ids]
            return max(0.0, min(ts) - (e.seconds or 5.0)) if ts else None
        steps = len(next(iter(tracks.values()), []))
        for k in range(steps):
            if e.type == "near":
                within = e.within_m or 50.0
                n = sum(1 for f in theirs if f.id in tracks and any(
                    a.id in tracks and math.dist(tracks[f.id][k], tracks[a.id][k]) <= within for a in mine))
                if n >= (e.at_least or 1):
                    return k * STEP_S
            elif e.zone:
                n = sum(1 for f in theirs if f.id in tracks and self._in_zone(
                    e.zone, SimpleNamespace(x=tracks[f.id][k][0], y=tracks[f.id][k][1]), state))
                if n >= (e.at_least or 1):
                    return k * STEP_S
        return None

    def _event(self, r: Reflex, g: GroupOrder, allies: list[Unit], enemies: list[Unit], state: State,
               slots: dict) -> tuple[bool, Unit | None, str]:
        """(happened, the enemy that set it off, a short description)."""
        e = r.event
        mine = self._select(r.ally_target, allies, g)
        theirs = self._select(r.enemy_target, enemies, g)
        if e.type == "near":
            within = e.within_m or 50.0
            close = [f for f in theirs if any(dist(f, a) <= within for a in mine)]
            if len(close) >= (e.at_least or 1):
                f = min(close, key=lambda f: min(dist(f, a) for a in mine))
                return True, f, f"enemy #{f.id} within {within:.0f} m"
        elif e.type == "engaged":
            for a in mine:
                for f in theirs:
                    if dist(a, f) <= ENGAGED_M and (
                            (a.action == "melee" and a.target == f.id) or (f.action == "melee" and f.target == a.id)):
                        return True, f, f"#{a.id} fights enemy #{f.id}"
        elif e.type == "routed":
            pool = mine if (e.side or "ally") == "ally" else theirs
            for u in pool:
                if u.action in ("routing", "dead") or u.soldiers <= 0:
                    foe = min((f for f in theirs if _fighting(f)), key=lambda f: dist(f, u), default=None)
                    return True, foe, f"#{u.id} {u.action}"
        elif e.type == "in_zone" and e.zone:
            inside = [f for f in theirs if _fighting(f) and self._in_zone(e.zone, f, state)]
            if len(inside) >= (e.at_least or 1):
                c = centroid(mine) if mine else (0.0, 0.0)
                f = min(inside, key=lambda f: math.dist((f.x, f.y), c))
                return True, f, f"{len(inside)} enemy unit(s) in {e.zone}"
        elif e.type == "morale_below" and e.value is not None:
            for a in mine:
                if a.morale < e.value:
                    return True, self._nearest(a, theirs), f"#{a.id} morale {a.morale:.0%}"
        elif e.type == "arrived":
            units = [a for a in allies if a.id in set(g.unit_ids)]
            if units and all(a.id in slots and math.dist((a.x, a.y), slots[a.id]) <= ARRIVED_M for a in units):
                return True, self._nearest(units[0], theirs), "arrived"
        elif e.type == "after" and e.seconds is not None and self.plan_t is not None:
            if state.t - self.plan_t >= e.seconds:
                return True, self._nearest(mine[0], theirs) if mine else None, f"{e.seconds:.0f} s passed"
        elif e.type == "out_of_ammo":
            for a in mine:
                if a.ammo is not None and a.ammo <= 0.02:
                    return True, self._nearest(a, theirs), f"#{a.id} out of ammunition"
        elif e.type == "predicted_contact":
            within = e.seconds or 5.0
            ids = {a.id for a in mine}
            for f in theirs:
                t = self._contacts(state).get(f.id)
                if t is not None and t <= within and f.target in ids:
                    return True, f, f"enemy #{f.id} predicted to reach #{f.target} in ~{t:.0f}s"
        return False, None, ""

    @staticmethod
    def _nearest(u: Unit, pool: list[Unit]) -> Unit | None:
        alive = [f for f in pool if _fighting(f)]
        return min(alive, key=lambda f: dist(f, u)) if alive else None

    # --- orders --------------------------------------------------------------

    def _override(self, g: GroupOrder, r: Reflex, foe: Unit | None, allies: list[Unit]) -> GroupOrder:
        o = r.order
        upd: dict = {"reflexes": None, "lookahead": []}
        if o.type == "attack":
            if foe is None:
                return g
            upd.update(task="attack", target_unit=foe.id, attack_from=o.attack_from or "front", run=o.run,
                       melee=o.melee, positions=None, path=None, waypoints=None)
        elif o.type == "move" and o.target_point:
            upd.update(task="move", target_point=o.target_point, facing_deg=o.facing_deg, run=o.run,
                       positions=None, path=None, waypoints=None)
        elif o.type == "fall_back":
            d = o.distance_m or FALL_BACK_M
            pos = []
            for uid in g.unit_ids:
                u = next((a for a in allies if a.id == uid), None)
                if u is None or foe is None:
                    pos.append([u.x, u.y] if u else [0.0, 0.0])
                    continue
                dx, dy = u.x - foe.x, u.y - foe.y
                n = math.hypot(dx, dy) or 1.0
                x, y = u.x + dx / n * d, u.y + dy / n * d
                pos.append([round(x, 1), round(y, 1), round(math.degrees(math.atan2(foe.x - x, foe.y - y)) % 360, 1)])
            upd.update(task="move", positions=pos, run=o.run, path=None, waypoints=None)
        elif o.type == "hold":
            upd.update(task="halt")
        elif o.type == "withdraw":
            upd.update(task="withdraw")
        elif o.type == "ability":
            upd.update(use_ability=True if o.on is None else o.on, ability_enemy_within_m=None)
        elif o.type == "rally":
            upd.update(rally=True if o.on is None else o.on)
        elif o.type == "formation" and o.formation:
            upd.update(formation=o.formation)
        return g.model_copy(update=upd)

    # --- per tick ------------------------------------------------------------

    def effective(self, state: State, plan: Plan | None, slots: dict) -> tuple[list[GroupOrder], set[int]]:
        """The groups as they act now (reflex overrides applied), and the units whose order just changed."""
        if plan is None:
            return [], set()
        if self.plan_t is None:
            self.plan_t = state.t
        ai = self.start.ai_alliance
        allies = [u for u in state.units if u.alliance == ai]
        enemies = [u for u in state.units if u.alliance != ai and _fighting(u)]
        changed: set[int] = set()
        for g in plan.groups:
            for n, r in self.all_reflexes(g):
                if (g.name, n) in self.fired:
                    continue
                hit, foe, why = self._event(r, g, allies, enemies, state, slots)
                if hit:
                    self.fired[(g.name, n)] = (state.t, foe.id if foe else None, why)
                    self.overrides[g.name] = self._override(self.overrides.get(g.name, g), r, foe, allies)
                    changed.update(g.unit_ids)
        return [self.overrides.get(g.name, g) for g in plan.groups], changed

    def status(self, plan: Plan | None, state: State) -> list[str]:
        """For the battle report: what fired and what is still waiting."""
        out = []
        for g in plan.groups if plan else []:
            for n, r in self.all_reflexes(g):
                o = r.order
                what = o.type + (f" {o.formation}" if o.formation else "") + (
                    f" ({o.target_point[0]:.0f},{o.target_point[1]:.0f})" if o.type == "move" and o.target_point else "")
                hit = self.fired.get((g.name, n))
                if hit:
                    t, foe, why = hit
                    tgt = f" #{foe}" if o.type == "attack" and foe is not None else ""
                    out.append(f"  '{g.name}' reflex {n}: FIRED at {t:.0f}s ({why}) -> {what}{tgt}")
                else:
                    e = r.event
                    cond = e.type + "".join(f" {k}={v}" for k, v in (("within_m", e.within_m), ("zone", e.zone),
                                                                      ("at_least", e.at_least), ("value", e.value),
                                                                      ("seconds", e.seconds), ("side", e.side)) if v is not None)
                    exp = self._expected(r, g, state)
                    out.append(f"  '{g.name}' reflex {n}: waiting for {cond} -> {what}"
                               + (f" (expected in ~{exp:.0f}s if nothing changes)" if exp is not None else ""))
        return ["\nYOUR REFLEXES (conditional orders of the current plan):"] + out if out else []
