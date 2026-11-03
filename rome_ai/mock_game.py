"""A toy battle simulator that speaks the same protocol as the in-game hook.

Lets the AI side be developed and tested before the real hook exists:

    python -m rome_ai.server --backend rule      # terminal 1
    python -m rome_ai.mock_game --speed 4        # terminal 2

Alliance 0 is a dumb scripted 'player' (everyone charges the nearest enemy),
alliance 1 is controlled by the AI server. The physics are deliberately crude.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
from dataclasses import dataclass, field

from .protocol import BattleEnd, BattleStart, Hello, MapInfo, State, Unit, dump

TEMPLATES = {
    # name: (category, class, soldiers, walk, run, melee, defence, missile, charge)
    "hastati": ("infantry", "heavy", 160, 2.0, 4.5, 7, 6, 0, 3),
    "triarii": ("infantry", "spearmen", 80, 2.0, 4.0, 7, 9, 0, 2),
    "town watch": ("infantry", "spearmen", 240, 2.0, 4.0, 3, 5, 0, 1),
    "warband": ("infantry", "light", 240, 2.4, 5.0, 8, 3, 0, 6),
    "archers": ("missile", "missile", 120, 2.0, 4.5, 2, 2, 6, 0),
    "skirmishers": ("missile", "skirmish", 120, 2.4, 5.0, 3, 2, 5, 0),
    "equites": ("cavalry", "light", 60, 4.0, 10.0, 6, 4, 0, 8),
    "heavy horse": ("cavalry", "heavy", 54, 3.5, 9.0, 8, 6, 0, 10),
    "general": ("general", "heavy", 30, 3.5, 9.0, 9, 8, 0, 9),
}


@dataclass
class SimUnit:
    id: int
    alliance: int
    name: str
    x: float
    y: float
    heading: float
    soldiers: float = 0
    max_soldiers: int = 0
    morale: float = 1.0
    fatigue: float = 0.0
    ammo: float | None = None
    action: str = "idle"
    target: int | None = None
    dest: tuple[float, float] | None = None
    running: bool = False
    stats: tuple = field(default=())

    def __post_init__(self) -> None:
        self.stats = TEMPLATES[self.name]
        self.soldiers = self.max_soldiers = self.stats[2]
        if self.stats[7]:
            self.ammo = 1.0

    @property
    def category(self) -> str:
        return self.stats[0]

    @property
    def fighting(self) -> bool:
        return self.action not in ("dead", "routing")

    def to_wire(self) -> Unit:
        return Unit(
            id=self.id, alliance=self.alliance, name=self.name, category=self.stats[0], cls=self.stats[1],
            x=self.x, y=self.y, heading=self.heading % 360, soldiers=int(self.soldiers), max_soldiers=self.max_soldiers,
            morale=max(0.0, self.morale), fatigue=self.fatigue, ammo=self.ammo, action=self.action, target=self.target,
        )


ARMY = ["general", "hastati", "hastati", "triarii", "warband", "archers", "skirmishers", "equites", "heavy horse"]


def deploy(rng: random.Random) -> list[SimUnit]:
    units = []
    for alliance, y0, heading in ((0, -350.0, 0.0), (1, 350.0, 180.0)):
        uid = (alliance + 1) * 10000  # as the hook numbers them: (alliance + 1) * 10000 + army * 100 + n
        names = ARMY[:]
        rng.shuffle(names)
        foot = [n for n in names if TEMPLATES[n][0] in ("infantry", "missile")]
        horse = [n for n in names if n not in foot]
        for i, n in enumerate(foot):
            units.append(SimUnit(uid, alliance, n, (i - (len(foot) - 1) / 2) * 45, y0, heading)); uid += 1
        for i, n in enumerate(horse):
            side = -1 if i % 2 == 0 else 1
            units.append(SimUnit(uid, alliance, n, side * (200 + 40 * (i // 2)), y0 - math.copysign(40, y0) * -1, heading)); uid += 1
    return units


class Sim:
    def __init__(self, units: list[SimUnit]) -> None:
        self.units = {u.id: u for u in units}
        self.t = 0.0

    def order(self, o: dict) -> None:
        u = self.units.get(o.get("unit"))
        if u is None or not u.fighting:
            return
        cmd = o["cmd"]
        if cmd == "move":
            u.dest, u.target, u.running = (o["x"], o["y"]), None, o.get("run", False)
        elif cmd == "attack" and o["target"] in self.units:
            u.target, u.dest, u.running = o["target"], None, o.get("run", True)
        elif cmd == "halt":
            u.dest = u.target = None

    def player_ai(self) -> None:
        for u in self.units.values():
            if u.alliance == 0 and u.fighting and (u.target is None or not self.units[u.target].fighting):
                foes = [f for f in self.units.values() if f.alliance != 0 and f.fighting]
                if foes:
                    u.target = min(foes, key=lambda f: math.dist((u.x, u.y), (f.x, f.y))).id
                    u.running = True

    def step(self, dt: float) -> None:
        self.t += dt
        losses: dict[int, float] = {}
        for u in self.units.values():
            if u.action == "dead":
                continue
            cat, _, _, walk, run, melee, _, missile, charge = u.stats
            if u.action == "routing":
                self._move_towards(u, u.x, 1200 if u.alliance == 1 else -1200, run, dt)
                if abs(u.y) > 1000:
                    u.action = "dead"
                continue
            tgt = self.units.get(u.target) if u.target is not None else None
            if tgt is not None and not tgt.fighting:
                u.target, tgt = None, None
            u.action = "idle"
            if tgt is not None:
                d = math.dist((u.x, u.y), (tgt.x, tgt.y))
                if missile and u.ammo and d <= 160:
                    u.action = "shooting"
                    u.heading = math.degrees(math.atan2(tgt.x - u.x, tgt.y - u.y))
                    hits = u.soldiers * missile * 0.0012 * dt
                    losses[tgt.id] = losses.get(tgt.id, 0) + hits
                    u.ammo = max(0.0, u.ammo - 0.012 * dt)
                elif d <= 12:
                    u.action = "melee"
                    flank = self._flank_factor(u, tgt)
                    dmg = u.soldiers ** 0.5 * melee * 0.05 * flank * (1.6 - u.fatigue) * dt
                    losses[tgt.id] = losses.get(tgt.id, 0) + dmg / max(1.0, tgt.stats[6] / 4)
                    if flank > 1.2:
                        tgt.morale -= 0.04 * dt
                else:
                    self._move_towards(u, tgt.x, tgt.y, run if u.running else walk, dt)
                    u.action = "running" if u.running else "moving"
            elif u.dest is not None:
                if math.dist((u.x, u.y), u.dest) < 3:
                    u.dest = None
                else:
                    self._move_towards(u, *u.dest, run if u.running else walk, dt)
                    u.action = "running" if u.running else "moving"
            u.fatigue = min(1.0, max(0.0, u.fatigue + dt * (0.006 if u.action in ("running", "melee") else -0.003)))

        for uid, dead in losses.items():
            v = self.units[uid]
            before = v.soldiers
            v.soldiers = max(0.0, v.soldiers - dead)
            v.morale -= (before - v.soldiers) / v.max_soldiers * 1.8
            if v.soldiers <= 0:
                v.action = "dead"
                if v.category == "general":
                    for o in self.units.values():
                        if o.alliance == v.alliance:
                            o.morale -= 0.25
            elif v.morale < 0.15 and v.action != "routing":
                v.action, v.target, v.dest = "routing", None, None
        for u in self.units.values():
            if u.fighting and u.action != "melee":
                u.morale = min(1.0, u.morale + 0.004 * dt)

    def _flank_factor(self, a: SimUnit, t: SimUnit) -> float:
        ang = math.degrees(math.atan2(a.x - t.x, a.y - t.y))
        off = abs((ang - t.heading + 180) % 360 - 180)  # 0 = attacked from the front
        return 1.0 if off < 60 else 1.6 if off < 120 else 2.2

    @staticmethod
    def _move_towards(u: SimUnit, x: float, y: float, speed: float, dt: float) -> None:
        dx, dy = x - u.x, y - u.y
        d = math.hypot(dx, dy)
        if d < 1e-6:
            return
        step = min(d, speed * dt)
        u.x += dx / d * step
        u.y += dy / d * step
        u.heading = math.degrees(math.atan2(dx, dy))

    def winner(self) -> int | None:
        left = {u.alliance for u in self.units.values() if u.fighting}
        return left.pop() if len(left) == 1 else (None if left else -1)

    def snapshot(self) -> State:
        return State(t=round(self.t, 2), units=[u.to_wire() for u in self.units.values()])


async def run(host: str, port: int, speed: float, limit: float, seed: int) -> None:
    rng = random.Random(seed)
    sim = Sim(deploy(rng))
    reader, writer = await asyncio.open_connection(host, port)
    writer.write(dump(Hello()))
    writer.write(dump(BattleStart(ai_alliance=1, player_is_attacker=True, map=MapInfo(notes="open grassland, gentle slope"))))
    await writer.drain()

    n_orders = 0

    async def read_orders() -> None:
        nonlocal n_orders
        while line := await reader.readline():
            msg = json.loads(line)
            for o in msg.get("orders", []):
                sim.order(o)
                n_orders += 1

    reader_task = asyncio.create_task(read_orders())
    dt, report_every, next_report = 0.1, 0.5, 0.0
    try:
        while sim.t < limit and sim.winner() is None:
            sim.player_ai()
            sim.step(dt)
            if sim.t >= next_report:
                next_report += report_every
                writer.write(dump(sim.snapshot()))
                await writer.drain()
            if int(sim.t / dt) % int(10 / dt) == 0:
                print(_summary(sim), f"| AI orders so far: {n_orders}")
            await asyncio.sleep(dt / speed)
    finally:
        w = sim.winner()
        writer.write(dump(BattleEnd(winner_alliance=w if w not in (None, -1) else None)))
        await writer.drain()
        reader_task.cancel()
        writer.close()
    print("\nRESULT:", {None: "time limit", -1: "mutual destruction", 0: "player (scripted) won", 1: "AI won"}[sim.winner()])
    print(_summary(sim))


def _summary(sim: Sim) -> str:
    parts = []
    for a in (0, 1):
        us = [u for u in sim.units.values() if u.alliance == a]
        men = sum(u.soldiers for u in us if u.fighting)
        parts.append(f"{'player' if a == 0 else 'AI'}: {int(men)} men, {sum(u.fighting for u in us)}/{len(us)} units")
    return f"t={sim.t:5.0f}s  " + "  vs  ".join(parts)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=7777)
    p.add_argument("--speed", type=float, default=1.0, help="simulation speed multiplier")
    p.add_argument("--limit", type=float, default=900.0, help="battle time limit, seconds")
    p.add_argument("--seed", type=int, default=1)
    a = p.parse_args()
    asyncio.run(run(a.host, a.port, a.speed, a.limit, a.seed))


if __name__ == "__main__":
    main()
