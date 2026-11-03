"""Turns the general's Plan into per-unit game orders, several times per second.

Every number (shape, spacing, facing, ranks, path, run) comes from the plan. The executor does the
geometry - where each unit of a line or column stands - walks units through their waypoints, and
runs the reflexes the plan switched on for a group, which need a reaction faster than the general
can re-plan:
  attack_from     go round to the target's left / right / rear, then charge
  on_target_lost  "nearest": a new target when the old one dies or routs
  engage_within_m attack enemies that come this close to the unit's place, then go back
  keep_range_m    missile units back off from enemies closer than this
"""

from __future__ import annotations

import math

from .geometry import centroid, dist, surrounded, unit_vec
from .plan import GroupOrder, Plan
from .protocol import Attack, AttackBuilding, BattleStart, DropEngine, Rally, SpecialAbility, Withdraw, FireAtWill, Formation, Halt, Move, Order, Skirmish, State, Unit

# Physical size of a man in formation (metres of frontage per man), used to turn "ranks" into a width in men and
# a line into unit frontages. Not tactics, just geometry.
M_PER_MAN = {"infantry": 1.5, "missile": 1.8, "cavalry": 2.6, "general": 2.6, "siege": 6.0, "other": 1.5}
WAYPOINT_REACHED_M = 15.0
ARRIVED_M = 12.0
RESEND_AFTER = 8.0  # s; re-issue an unchanged order only to a unit that has stood idle this long without doing it
# formations that are special-ability toggles in this game -> the unit's special ability type (hook "ability")
TOGGLE_FORMATIONS = {"phalanx": 2, "testudo": 1}
ARTILLERY_RANGE_M = 300.0
FRIENDLY_FIRE_M = 30.0  # a target whose line of fire passes this close to our own siege equipment is not shot at
FLANK_STANDOFF_M = 45.0  # attack_from: point this far from the target on the chosen side, reached before charging
FLANK_REACHED_M = 20.0


def _bearing(dx: float, dy: float) -> float:
    return (math.degrees(math.atan2(dx, dy)) + 360) % 360


class Executor:
    def __init__(self, start: BattleStart) -> None:
        self.start = start
        self.last: dict[int, tuple[tuple, float]] = {}  # unit -> (order key, time sent)
        self.modes: dict[tuple[int, str], bool] = {}  # (unit, mode) -> on
        self.skirmish_auto_off: set[int] = set()  # units whose skirmish mode we switched off for a melee attack
        self.formations: dict[int, str] = {}  # unit -> formation last ordered
        self.dropped: dict[int, float] = {}  # unit -> battle time a drop_engine was last sent
        self.rally_sent: dict[int, float] = {}  # general -> battle time rally was last toggled
        self.ability_sent: dict[int, float] = {}  # unit -> battle time its special ability was last ordered
        self.new_plan(Plan(intent="", groups=[]))

    def new_plan(self, plan: Plan) -> None:
        self.slots: dict[int, tuple[float, float]] = {}  # unit -> final position in its group's shape
        self.facings: dict[int, float] = {}  # unit -> its own facing from the plan's positions
        self.paths: dict[int, list[tuple[float, float]]] = {}  # unit -> remaining waypoints (incl. its final slot)
        self.final_sent: set[int] = set()  # units that got the order for their final slot in this plan
        self.targets: dict[int, int] = {}  # unit -> enemy it attacks (may differ from the plan's after on_target_lost)
        self.flanked: set[int] = set()  # units that reached their attack_from side and now charge
        self.homes: dict[int, tuple[float, float]] = {}  # halt + engage_within_m: where the unit stood

    # --- main ---------------------------------------------------------------

    def tick(self, state: State, plan: Plan | None) -> list[Order]:
        ai = self.start.ai_alliance
        by_id = {u.id: u for u in state.units}
        self.foes = [u for u in state.units if u.alliance != ai and u.action not in ("dead", "routing") and u.soldiers > 0]
        out: list[Order] = []
        for g in plan.groups if plan else []:
            # units the game's AI has while they use their siege equipment take no orders from us
            units = [by_id[i] for i in g.unit_ids if i in by_id and by_id[i].alliance == ai
                     and by_id[i].action not in ("dead", "routing") and not by_id[i].game_ai]
            if not units:
                continue
            for u in units:
                out += self._modes(u, g, state.t)
                near = g.ability_enemy_within_m is None or any(
                    dist(u, f) < g.ability_enemy_within_m for f in self.foes)
                if ((g.use_ability is True and u.ability_ready and not u.ability_on and near)
                        or (g.use_ability is False and u.ability_on)) \
                        and state.t - self.ability_sent.get(u.id, -1e9) > RESEND_AFTER:
                    self.ability_sent[u.id] = state.t
                    out.append(SpecialAbility(unit=u.id))
                if g.rally is not None and u.rally >= 0 and bool(u.rally) != g.rally \
                        and state.t - self.rally_sent.get(u.id, -1e9) > RESEND_AFTER:
                    self.rally_sent[u.id] = state.t
                    out.append(Rally(unit=u.id, on=g.rally))
                if g.drop_equipment and u.engines > 0 and state.t - self.dropped.get(u.id, -1e9) > RESEND_AFTER:
                    self.dropped[u.id] = state.t
                    out.append(DropEngine(unit=u.id))
            orders = {
                "move": self._move,
                "attack": self._attack,
                "attack_building": self._attack_building,
                "halt": self._halt,
                "withdraw": self._withdraw,
            }[g.task](g, units, by_id)
            for u in units:
                o = self._reflexes(u, g, orders.get(u.id))
                orders[u.id] = o
            for u in units:
                o = orders.get(u.id)
                if o is not None and self._changed(u, o, state.t):
                    out.append(o)
        return out

    # --- tasks ----------------------------------------------------------------

    def _withdraw(self, g: GroupOrder, units: list[Unit], by_id) -> dict[int, Order]:
        return {u.id: Withdraw(unit=u.id) for u in units}

    def _halt(self, g: GroupOrder, units: list[Unit], by_id) -> dict[int, Order]:
        return {u.id: Halt(unit=u.id) for u in units if u.action in ("moving", "running")}

    def _attack(self, g: GroupOrder, units: list[Unit], by_id) -> dict[int, Order]:
        out: dict[int, Order] = {}
        for u in units:
            t = by_id.get(self.targets.get(u.id, g.target_unit)) if g.target_unit is not None else None
            if t is not None and t.alliance == self.start.ai_alliance:
                t = None
            if t is None or t.action in ("dead", "routing") or t.soldiers <= 0:
                t = None
                if g.on_target_lost == "nearest" and self.foes:
                    t = min(self.foes, key=lambda f: dist(u, f))
                    self.targets[u.id] = t.id
                    self.flanked.add(u.id)  # a reflex target is attacked straight away
            if t is None:
                if u.action in ("moving", "running"):
                    out[u.id] = Halt(unit=u.id)
                continue
            if g.attack_from != "front" and u.id not in self.flanked and t.target != u.id:
                p = self._flank_point(u, t, g.attack_from)
                if p is not None:
                    out[u.id] = Move(unit=u.id, x=round(p[0], 1), y=round(p[1], 1), run=g.run)
                    continue
                self.flanked.add(u.id)
            if u.category == "siege" and self._fires_over_own_equipment(u, t):
                # hard safety: artillery never shoots over our own siege tower / ladders / ram - take the nearest
                # enemy whose line of fire is clear, else hold fire
                clear = [f for f in self.foes
                         if dist(u, f) < ARTILLERY_RANGE_M and not self._fires_over_own_equipment(u, f)]
                if not clear:
                    if u.action in ("shooting", "moving", "running"):
                        out[u.id] = Halt(unit=u.id)
                    continue
                t = min(clear, key=lambda f: dist(u, f))
            out[u.id] = Attack(unit=u.id, target=t.id, run=g.run, melee=g.melee)
        return out

    def _fires_over_own_equipment(self, u: Unit, t: Unit) -> bool:
        g = self.start.town_graph
        if g is None or not self.start.siege_targets:
            return False
        for node in self.start.siege_targets:
            n = g.nodes.get(node)
            if n is not None and _near_segment((n.x, n.y), (u.x, u.y), (t.x, t.y), FRIENDLY_FIRE_M):
                return True
        return False

    def _flank_point(self, u: Unit, t: Unit, side: str) -> tuple[float, float] | None:
        """Next point on the way to the target's side; None once the unit is there."""
        fx, fy = math.sin(math.radians(t.heading)), math.cos(math.radians(t.heading))  # target's front
        rx, ry = fy, -fx  # target's right
        sides = {"left": (-rx, -ry), "right": (rx, ry), "rear": (-fx, -fy)}
        sx, sy = sides[side]
        goal = (t.x + sx * FLANK_STANDOFF_M, t.y + sy * FLANK_STANDOFF_M)
        if math.hypot(u.x - goal[0], u.y - goal[1]) < FLANK_REACHED_M:
            return None
        if side == "rear" and (u.x - t.x) * fx + (u.y - t.y) * fy > 0:
            # still in front of the target: pass by whichever side is nearer first
            near = min(("left", "right"), key=lambda k: math.hypot(u.x - t.x - sides[k][0] * 60, u.y - t.y - sides[k][1] * 60))
            px, py = t.x + sides[near][0] * 60, t.y + sides[near][1] * 60
            if math.hypot(u.x - px, u.y - py) > FLANK_REACHED_M:
                return px, py
        return goal

    # --- reflexes the plan switched on ---------------------------------------------

    def _reflexes(self, u: Unit, g: GroupOrder, o: Order | None) -> Order | None:
        # surrounded: no side to back away to - backing off from whichever enemy is nearest would only turn the
        # unit back and forth on the spot, so it carries on with its task
        # a melee attack (a unit without missiles, or melee=true) has to reach the enemy: backing off would cancel it
        charging = g.task == "attack" and (g.melee or u.ammo is None)
        if g.keep_range_m and self.foes and not charging and not surrounded(u, self.foes, g.keep_range_m):
            e = min(self.foes, key=lambda f: dist(u, f))
            d = dist(u, e)
            if d < g.keep_range_m and e.category != "siege":
                dx, dy = unit_vec(u.x - e.x, u.y - e.y)
                back = g.keep_range_m - d + 20
                return Move(unit=u.id, x=round(u.x + dx * back, 1), y=round(u.y + dy * back, 1), run=True)
        if g.engage_within_m and g.task in ("move", "halt") and self.foes:
            home = self.slots.get(u.id) or self.homes.setdefault(u.id, (u.x, u.y))
            e = min(self.foes, key=lambda f: math.hypot(f.x - home[0], f.y - home[1]))
            if math.hypot(e.x - home[0], e.y - home[1]) < g.engage_within_m:
                return Attack(unit=u.id, target=e.id, run=True, melee=g.melee)
            if g.task == "halt" and math.hypot(u.x - home[0], u.y - home[1]) > 15:
                return Move(unit=u.id, x=home[0], y=home[1], run=False)  # back to its place after a fight
        return o

    def _graph_node(self, nid):
        g = self.start.town_graph
        return g.nodes.get(str(nid)) if g is not None and nid is not None else None

    def _building(self, ref):
        """A building by town graph node id ('G1', 'W27', 'T5w') or by building id."""
        n = self._graph_node(ref) if isinstance(ref, str) else None
        bid = n.building if n is not None else (ref if isinstance(ref, int) else None)
        return self.start.buildings.get(bid) if bid is not None else None

    def _target_xy(self, g: GroupOrder) -> tuple[float, float] | None:
        pts = [p for p in (g.positions or []) if len(p) >= 2]
        if pts:
            return sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)
        if g.target_point:
            return g.target_point[0], g.target_point[1]
        n = self._graph_node(g.path[-1]) if g.path else None
        return (n.x, n.y) if n is not None else None

    def _use_equipment(self, u: Unit, g: GroupOrder) -> Order | None:
        """A unit carrying a ram / ladders / a siege tower uses it on the first still-closed way in on its path:
        the ram on a gate, ladders and siege towers on a wall section."""
        if u.engines <= 0 or not g.path or u.engine_type not in (0, 1, 2) or g.drop_equipment:
            return None
        want = "gate" if u.engine_type == 0 else "wall"
        for nid in g.path:
            n = self._graph_node(nid)
            if n is None or n.kind != want or n.building is None:
                continue
            b = self.start.buildings.get(n.building)
            if b is not None and b.health > 0:
                return AttackBuilding(unit=u.id, building=b.id, run=g.run)
        return None

    def _attack_building(self, g: GroupOrder, units: list[Unit], by_id) -> dict[int, Order]:
        b = self._building(g.target_building) if g.target_building is not None else None
        if b is None or b.health == 0:
            return self._halt(g, units, by_id)
        return {u.id: AttackBuilding(unit=u.id, building=b.id, run=g.run) for u in units}

    def _move(self, g: GroupOrder, units: list[Unit], by_id) -> dict[int, Order]:
        if self._target_xy(g) is None:
            return self._halt(g, units, by_id)
        if any(u.id not in self.slots for u in units):
            self._layout(g, units)
        orders: dict[int, Order] = {}
        for u in units:
            siege = self._use_equipment(u, g)
            if siege is not None:
                orders[u.id] = siege
                continue
            path = self.paths.get(u.id) or [self.slots[u.id]]
            while len(path) > 1 and math.hypot(u.x - path[0][0], u.y - path[0][1]) < WAYPOINT_REACHED_M:
                path.pop(0)  # waypoint reached, on to the next one
            x, y = path[0]
            final = len(path) == 1
            # Never halt a unit near its slot: the game would freeze it mid-step, short of the slot and facing the
            # way it was walking. Send the final order once (even to a unit already close, so it takes its exact
            # place and facing) and let the game finish it.
            if final and u.id in self.final_sent and math.hypot(u.x - x, u.y - y) < ARRIVED_M:
                continue
            if final:
                self.final_sent.add(u.id)
            facing = self.facings.get(u.id, g.facing_deg) if final else None
            width = self._width(u, g.ranks) if final else None
            orders[u.id] = Move(unit=u.id, x=x, y=y, run=g.run,
                                facing=None if facing is None else round(facing % 360, 1), width=width)
        return orders

    # --- geometry ---------------------------------------------------------------

    def _route_points(self, g: GroupOrder) -> list[tuple[float, float]]:
        """The group's waypoints in marching order (not including target_point)."""
        if g.path and self.start.town_graph is not None:
            # graph nodes in order; points on top of the wall are left out on the way (the game's own pathfinder
            # takes units up ladders / siege towers and down tower stairs), only the destination may be on the wall
            pts = []
            for nid in g.path[:-1]:
                n = self._graph_node(nid)
                if n is not None and n.level == "ground":
                    pts.append((n.x, n.y))
            return pts
        if g.waypoints:
            return [(p[0], p[1]) for p in g.waypoints]
        return []

    def _width(self, u: Unit, ranks: int | None) -> int | None:
        return max(4, round(u.soldiers / ranks)) if ranks else None

    def _frontage(self, u: Unit, ranks: int | None) -> float:
        from .units_db import file_spacing

        if ranks is None and u.width_men:  # the unit's real current width
            return u.width_men * file_spacing(u.name, u.category)
        men = self._width(u, ranks) or max(4, round(u.soldiers / 4))
        return men * file_spacing(u.name, u.category)

    def _layout(self, g: GroupOrder, units: list[Unit]) -> None:
        """Final positions from the plan's shape / spacing / facing; units in the order of unit_ids."""
        tx, ty = self._target_xy(g)
        route = self._route_points(g)
        order = [u for i in g.unit_ids for u in units if u.id == i]
        # direction the shape faces: the plan's facing, else the direction of the last leg of the march
        if g.facing_deg is not None:
            facing = g.facing_deg
        else:
            fx, fy = (route[-1] if route else centroid(order))
            facing = _bearing(tx - fx, ty - fy)
        ux, uy = math.sin(math.radians(facing)), math.cos(math.radians(facing))  # front direction
        rx, ry = uy, -ux  # to the right when facing the front (the line's axis)
        pos_of = dict(zip(g.unit_ids, g.positions or []))
        if g.positions:
            # free formation: every unit has its own point (and facing) from the plan
            for u in order:
                p = pos_of.get(u.id)
                if p is None or len(p) < 2:  # more units than points: they gather at the centre
                    self.slots[u.id] = (tx, ty)
                    continue
                self.slots[u.id] = (p[0], p[1])
                if len(p) >= 3:
                    self.facings[u.id] = p[2]
        elif g.shape == "line":
            # unit_ids run west -> east when the front faces roughly north/south, north -> south when it faces
            # roughly east/west: the axis is the right-hand vector, flipped to point east or south
            if abs(uy) >= abs(ux):
                ax, ay = (rx, ry) if rx >= 0 else (-rx, -ry)
            else:
                ax, ay = (rx, ry) if ry <= 0 else (-rx, -ry)
            fronts = [self._frontage(u, g.ranks) for u in order]
            total = sum(fronts) + g.spacing_m * (len(order) - 1)
            pos = -total / 2
            for u, f in zip(order, fronts):
                off = pos + f / 2
                self.slots[u.id] = (tx + ax * off, ty + ay * off)
                pos += f + g.spacing_m
        elif g.shape == "column":
            # one behind the other, back along the way in (the route if given, else straight back)
            back = [(tx, ty)] + list(reversed(route)) if route else [(tx, ty), (tx - ux * 1000, ty - uy * 1000)]
            for k, u in enumerate(order):
                self.slots[u.id] = _point_back_along(back, k * g.spacing_m)
        else:
            for u in order:
                self.slots[u.id] = (tx, ty)
        if self.start.masks is not None and self.start.map.settlement:
            # a point inside a building (e.g. a temple) cannot be reached: the unit would circle at its wall
            from .terrain import _cell_rc, decode_masks, snap_free

            obst, _ = decode_masks(self.start.masks)
            for uid, (sx, sy) in list(self.slots.items()):
                r, c = _cell_rc(self.start.masks, sx, sy)
                if 0 <= r < obst.shape[0] and 0 <= c < obst.shape[1] and obst[r, c]:
                    self.slots[uid] = snap_free(self.start.masks, sx, sy)
        for u in order:
            # march through the waypoints still ahead of the unit, then to its slot
            pts = list(route) + [self.slots[u.id]]
            if route:
                nearest = min(range(len(route)), key=lambda i: math.hypot(route[i][0] - u.x, route[i][1] - u.y))
                pts = pts[nearest:]
            # a new plan must not send a unit back to a waypoint it has already passed: drop a waypoint when
            # the unit is closer to the next point than that waypoint is
            while len(pts) > 1 and math.hypot(u.x - pts[1][0], u.y - pts[1][1]) < math.dist(pts[0], pts[1]):
                pts.pop(0)
            self.paths[u.id] = pts

    # --- unit modes from the plan -------------------------------------------------

    def _modes(self, u: Unit, g: GroupOrder, now: float = 0.0) -> list[Order]:
        out: list[Order] = []
        toggle = TOGGLE_FORMATIONS.get(g.formation or "")
        if toggle is not None and u.ability == toggle:
            # phalanx / testudo are special-ability toggles in this game: a formation order does not switch them on
            if not u.ability_on and u.ability_ready and now - self.ability_sent.get(u.id, -1e9) > RESEND_AFTER:
                self.ability_sent[u.id] = now
                out.append(SpecialAbility(unit=u.id))
        elif g.formation and u.ability in TOGGLE_FORMATIONS.values() and u.ability_on:
            # another formation wanted: switch the phalanx / testudo off first
            if now - self.ability_sent.get(u.id, -1e9) > RESEND_AFTER:
                self.ability_sent[u.id] = now
                out.append(SpecialAbility(unit=u.id))
        elif g.formation and self.formations.get(u.id) != g.formation:
            self.formations[u.id] = g.formation
            out.append(Formation(unit=u.id, formation=g.formation))
        skirmish = g.skirmish
        if skirmish is not None:  # the general decides himself
            self.skirmish_auto_off.discard(u.id)
        elif g.task == "attack" and g.melee and u.ammo is not None:  # only missile units have a skirmish mode
            skirmish = False  # skirmish mode makes the unit back off from the very melee it was sent into
            self.skirmish_auto_off.add(u.id)
        elif u.id in self.skirmish_auto_off:
            skirmish = True  # the melee charge is over: give the unit its skirmish mode back
            self.skirmish_auto_off.discard(u.id)
        for mode, on, cls in (("fire_at_will", g.fire_at_will, FireAtWill), ("skirmish", skirmish, Skirmish)):
            if on is not None and self.modes.get((u.id, mode)) != on:
                self.modes[(u.id, mode)] = on
                out.append(cls(unit=u.id, on=on))
        return out

    # --- de-dup ---------------------------------------------------------------------

    def _changed(self, u: Unit, o: Order, now: float) -> bool:
        if isinstance(o, Move):
            key: tuple = ("move", round(o.x / 10), round(o.y / 10), o.run, o.facing, o.width)
        elif isinstance(o, Attack):
            key = ("attack", o.target, o.run, o.melee)
        elif isinstance(o, AttackBuilding):
            key = ("attack_building", o.building, o.run)
        else:
            key = (o.cmd, *o.model_dump(exclude={"cmd", "unit"}).values())
        prev = self.last.get(u.id)
        if prev and prev[0] == key:
            # The same order is never re-issued while the unit is carrying it out: re-sending restarts the
            # approach (spoils a charge, stops a ram). Only a unit that has stood idle for RESEND_AFTER seconds
            # without having done it gets it again (the game dropped it). A building attack is sent only once.
            if key[0] == "attack_building" or u.action != "idle" or now - prev[1] < RESEND_AFTER:
                return False
        self.last[u.id] = (key, now)
        return True


def _near_segment(p, a, b, r: float) -> bool:
    """Is point p within r metres of the segment a-b?"""
    ax, ay = a
    dx, dy = b[0] - ax, b[1] - ay
    t = max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / (dx * dx + dy * dy or 1)))
    return math.hypot(p[0] - ax - t * dx, p[1] - ay - t * dy) <= r


def _point_back_along(path: list[tuple[float, float]], dist_m: float) -> tuple[float, float]:
    """The point dist_m back along a polyline that starts at the column head."""
    walked = 0.0
    for (x0, y0), (x1, y1) in zip(path, path[1:]):
        seg = math.hypot(x1 - x0, y1 - y0)
        if seg > 0 and walked + seg >= dist_m:
            t = (dist_m - walked) / seg
            return x0 + (x1 - x0) * t, y0 + (y1 - y0) * t
        walked += seg
    return path[-1]
