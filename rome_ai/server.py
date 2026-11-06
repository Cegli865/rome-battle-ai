"""AI server: the game hook connects here, streams battle state, gets orders back.

    python -m rome_ai.server --backend rule
    python -m rome_ai.server --backend claude
    python -m rome_ai.server --backend claude-code     # Claude subscription via `claude -p`, no API key (Sonnet 5.5)
    python -m rome_ai.server --backend claude-code --model claude-haiku-4-5   # cheaper, a bit slower per plan
    python -m rome_ai.server --backend openai --model gpt-5
    python -m rome_ai.server --backend deepseek                # DEEPSEEK_API_KEY, model deepseek-flash
    python -m rome_ai.server --backend openai --model qwen3:14b --base-url http://localhost:11434/v1
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
import logging
import math
import time
from pathlib import Path

from .executor import ARRIVED_M, Executor
from .general import General, make_general
from .plan import Plan
from .protocol import BattleEnd, BattleStart, BuildingStatus, Buildings, Control, Hello, Log, Crossings, Orders, Pause, PlatformsRaw, Streets, WallsRaw, TerrainMasks, TerrainMsg, RawState, Resume, Retaken, State, dump, parse_game_message

log = logging.getLogger("rome_ai")


MAX_LINE = 16 * 1024 * 1024  # terrain messages from the hook are ~0.5-1 MB on one line


RULES_FILE = Path(__file__).resolve().parents[1] / "rules.md"  # facts about the game, hard rules
STRATEGY_FILE = Path(__file__).resolve().parents[1] / "strategy.md"  # tactical advice
LOG_DIR = Path(__file__).resolve().parents[1] / "logs"
LOSS_WINDOW = 15.0  # seconds of history for "recent losses"
STUCK_AFTER = 12.0  # s with a move order and < STUCK_PROGRESS_M progress -> reported to the general as STUCK
STUCK_PROGRESS_M = 8.0
VELOCITY_WINDOW = 5.0  # s of position history used for the "going NE 4m/s" info
TREND_WINDOW = 10  # s of position history for an enemy unit's course, used where its order is not known
TREND_MIN_SPEED = 0.3  # m/s; slower than this the unit counts as standing
REPLAN_LOSSES = 0.08  # a unit under fire losing this share of its men within the window triggers an early plan
REPLAN_MIN_GAP = 20.0  # s; early plans at most this often (each one costs a model call)
FAILS_BEFORE_FALLBACK = 2  # consecutive failed plans (usage limit, network...) -> hand the army back to the game's AI
QUIET_MAX_GAP = 30.0  # s; if nothing changed, still ask the general at least this often
QUIET_MOVE_M = 60.0  # a unit moving more than this since the last plan counts as a change
CONTACT_M = 200.0  # any of our units this close to an enemy unit: plan every cycle, every CONTACT_INTERVAL s
# BATTLE stage before the fighting starts (seen: 2 for the first seconds, 3 or 4 while the player deploys, 5 once
# he clicks the start of the battle)
DEPLOYMENT_STAGES = (1, 2, 3, 4)
PRED_SNAP_EVERY = 5.0  # s of battle time between saved predictions (checked 5 and 10 s later, in the battle log)
CONTACT_INTERVAL = 4.0  # s between plans (from the start of one to the start of the next) while in contact


class BattleSession:
    def __init__(self, general: General, plan_interval: float, writer: asyncio.StreamWriter) -> None:
        self.general = general
        self.plan_interval = plan_interval
        self.writer = writer
        self.start: BattleStart | None = None
        self.executor: Executor | None = None
        self.positions: collections.deque = collections.deque()  # (battle time, {unit: (x, y)}) for STUCK detection
        self.state: State | None = None
        self.plan: Plan | None = None
        self.plan_since: float | None = None  # battle time the current plan was put into force
        self.planner: asyncio.Task | None = None
        self.history: collections.deque = collections.deque()  # (battle time, men, {unit: (ammo, fatigue)})
        self.pred_snaps: list[dict] = []  # predictions waiting to be checked against what happened
        self.last_pred_snap = -1e9
        self.replan = asyncio.Event()
        self.battle_log = None
        self.last_plan_at = 0.0
        self.planned_state: State | None = None  # the state the current plan was made for
        self.failures = 0
        self.fallback = False  # True while the game's built-in AI is in charge
        self.skipped = 0
        self.last_rate_limit: dict | None = None
        self.goal_since: dict[int, tuple[tuple[float, float], float]] = {}  # unit -> (move goal, battle time set)
        self.seen_on_field: set[int] = set()  # units that have been on the battlefield in this battle
        self.sides_checked = False

    def on_battle_start(self, msg: BattleStart) -> None:
        self.stop()
        self.start, self.executor, self.state, self.plan = msg, Executor(msg), None, None
        self.goal_since = {}
        self.seen_on_field = set()
        self.sides_checked = False  # every battle: the hook's attacker flag is unreliable in sieges
        self.positions.clear()
        for attr, path in (("rules", RULES_FILE), ("strategy", STRATEGY_FILE)):
            try:  # re-read every battle so edits apply without a restart
                setattr(self.general, attr, path.read_text(encoding="utf-8"))
            except OSError:
                setattr(self.general, attr, "")
        from .general import rule_lines

        n_rules = len(rule_lines(self.general.rules).splitlines())
        n_strategy = len(rule_lines(self.general.strategy).splitlines())
        self.history.clear()
        self.pred_snaps.clear()
        self.last_pred_snap = -1e9
        LOG_DIR.mkdir(exist_ok=True)
        if self.battle_log:
            self.battle_log.close()
        path = LOG_DIR / time.strftime("battle-%Y%m%d-%H%M%S.jsonl")
        self.battle_log = open(path, "a", encoding="utf-8")
        self._log_event("battle_start", start=msg.model_dump(), rules=self.general.rules, strategy=self.general.strategy,
                        general=self.general.describe() if hasattr(self.general, "describe") else None)
        log.info("battle log: %s", path)
        self.planner = asyncio.create_task(self._plan_loop())
        log.info(
            "battle started (%s, %s), AI controls alliance %d, %d rules from %s, %d strategy points from %s",
            "settlement" if msg.map.settlement else f"type {msg.battle_type}",
            "AI attacks" if not msg.player_is_attacker else "AI defends",
            msg.ai_alliance, n_rules, RULES_FILE.name, n_strategy, STRATEGY_FILE.name,
        )

    def _log_event(self, kind: str, **data) -> None:
        if self.battle_log:
            self.battle_log.write(json.dumps({"event": kind, "wall": round(time.time(), 2), **data}, default=str) + "\n")
            self.battle_log.flush()

    def _track_losses(self, msg: State) -> None:
        now = msg.t  # battle time: a pause is not a gap in the fighting
        if self.history and now < self.history[-1][0]:
            self.history.clear()  # battle time went back: a restarted battle
        counts = {u.id: u.soldiers for u in msg.units}
        self.history.append((now, counts, {u.id: (u.ammo, u.fatigue) for u in msg.units}))
        while self.history and now - self.history[0][0] > LOSS_WINDOW:
            self.history.popleft()
        old = self.history[0][1]
        msg.recent_losses = {i: old[i] - n for i, n in counts.items() if i in old and old[i] > n}
        # ammunition and fatigue trends over the last ~10 s (for the predictions)
        past = next((h for h in self.history if now - h[0] <= 10.0), None)
        if past is not None and now - past[0] >= 3.0:
            dt = now - past[0]
            for u in msg.units:
                a0, f0 = past[2].get(u.id, (None, None))
                if a0 is not None and u.ammo is not None and a0 > u.ammo:
                    msg.ammo_rate[u.id] = (a0 - u.ammo) / dt
                if f0 is not None and u.fatigue > f0:
                    msg.fatigue_rate[u.id] = (u.fatigue - f0) / dt
        if (self.start is not None and not self.replan.is_set()
                and time.monotonic() - self.last_plan_at >= REPLAN_MIN_GAP):
            from .describe import UNDER_FIRE_NO_ENEMY_WITHIN
            from .geometry import alive, dist, nearest

            foes = alive(u for u in msg.units if u.alliance != self.start.ai_alliance)
            for u in alive(u for u in msg.units if u.alliance == self.start.ai_alliance):
                lost = msg.recent_losses.get(u.id, 0)
                e = nearest(u, foes)
                under_fire = u.action != "melee" and (e is None or dist(u, e) > UNDER_FIRE_NO_ENEMY_WITHIN)
                if under_fire and lost >= max(5, REPLAN_LOSSES * u.max_soldiers):
                    log.info("unit #%d under fire, lost %d men in %.0fs - asking the general now", u.id, lost, LOSS_WINDOW)
                    self.replan.set()
                    break

    def _check_sides(self, msg: State) -> None:
        """In a siege the defender starts inside the town - more reliable than the hook's attacker flag. Counts the
        units standing in the settlement zone (an attacker may come in several armies from different sides, whose
        average position can fall inside the town)."""
        if self.sides_checked or self.start is None or not self.start.map.settlement or self.start.masks is None:
            return
        from .geometry import alive
        from .terrain import _cell_rc, decode_masks

        self.sides_checked = True
        m = self.start.masks
        _, zone = decode_masks(m)
        mine = alive(u for u in msg.units if u.alliance == self.start.ai_alliance)
        theirs = alive(u for u in msg.units if u.alliance != self.start.ai_alliance)
        if not mine or not theirs:
            return

        def share_inside(units) -> float:
            n = 0
            for u in units:
                r, c = _cell_rc(m, u.x, u.y)
                n += bool(zone[max(0, r - 3): r + 4, max(0, c - 3): c + 4].any())
            return n / len(units)

        ai_in, foe_in = share_inside(mine), share_inside(theirs)
        log.info("siege sides: %.0f%% of the AI's units and %.0f%% of the enemy's stand in the town", ai_in * 100, foe_in * 100)
        if abs(ai_in - foe_in) >= 0.3:
            ai_defends = ai_in > foe_in
            if self.start.player_is_attacker != ai_defends:
                log.info("siege sides corrected from positions: AI %s the town", "defends" if ai_defends else "attacks")
            self.start.player_is_attacker = ai_defends
            self._log_event("sides", ai_defends=ai_defends, ai_inside=round(ai_in, 2), enemy_inside=round(foe_in, 2))

    def _track_trend(self, msg: State) -> None:
        """Course of every enemy unit over the last TREND_WINDOW s: the velocity of each of those seconds, the
        newest weighted 1.0, the one before 0.9 ... the oldest 0.1, divided by the sum of the weights."""
        hist = self.positions

        def at(t: float) -> tuple[float, dict] | None:  # the position record closest to battle time t
            best = min(hist, key=lambda h: abs(h[0] - t))
            return best if abs(best[0] - t) <= 0.5 else None

        marks = [at(msg.t - s) for s in range(TREND_WINDOW + 1)]  # 0, 1, ... TREND_WINDOW s ago
        for u in msg.units:
            if u.alliance == self.start.ai_alliance:
                continue
            sx = sy = sw = 0.0
            for i in range(1, TREND_WINDOW + 1):
                new, old = marks[i - 1], marks[i]
                if new is None or old is None or new[0] - old[0] < 0.5 or u.id not in new[1] or u.id not in old[1]:
                    continue
                w = (TREND_WINDOW + 1 - i) / TREND_WINDOW
                dt = new[0] - old[0]
                sx += w * (new[1][u.id][0] - old[1][u.id][0]) / dt
                sy += w * (new[1][u.id][1] - old[1][u.id][1]) / dt
                sw += w
            if sw >= 1.5 and math.hypot(sx / sw, sy / sw) >= TREND_MIN_SPEED:  # at least ~2 s of history
                msg.trend[u.id] = (sx / sw, sy / sw)

    def _track_stuck(self, msg: State) -> None:
        """Information for the general only: where units with an unfinished move are going, and which of
        them don't get anywhere."""
        now = msg.t  # battle time: it stands still while the game is paused, so a pause is not "no progress"
        hist = self.positions
        if hist and now < hist[-1][0]:  # battle time went back (a restarted battle): old positions mean nothing
            hist.clear()
        hist.append((now, {u.id: (u.x, u.y) for u in msg.units}))
        while hist and now - hist[0][0] > STUCK_AFTER:
            hist.popleft()
        # velocity over the last ~VELOCITY_WINDOW s (both armies), for the unit lines
        past = next(((t, pos) for t, pos in hist if now - t <= VELOCITY_WINDOW), None)
        if past is not None and now - past[0] > 1:
            dt = now - past[0]
            for u in msg.units:
                if u.id in past[1]:
                    msg.velocity[u.id] = ((u.x - past[1][u.id][0]) / dt, (u.y - past[1][u.id][1]) / dt)
        if self.start is not None:
            self._track_trend(msg)
        if self.executor is None:
            return
        old = hist[0][1] if now - hist[0][0] >= STUCK_AFTER - 1 else {}
        for u in msg.units:
            last = self.executor.last.get(u.id)
            if (last and last[0][0] in ("attack", "attack_building") and u.action in ("idle", "moving", "running")
                    and u.id in old and msg.t - last[1] > STUCK_AFTER  # last[1] is battle time
                    and ((u.x - old[u.id][0]) ** 2 + (u.y - old[u.id][1]) ** 2) ** 0.5 < STUCK_PROGRESS_M):
                msg.stuck.add(u.id)  # ordered to attack, but neither fighting nor getting closer
                continue
            path = self.executor.paths.get(u.id)
            if not path or u.action in ("melee", "shooting", "dead", "routing"):
                continue
            tx, ty = path[-1]
            if ((u.x - tx) ** 2 + (u.y - ty) ** 2) ** 0.5 <= ARRIVED_M:
                continue
            msg.goals[u.id] = (tx, ty)
            since = self.goal_since.get(u.id)
            if since is None or math.dist(since[0], (tx, ty)) > 1:  # a new goal: give it STUCK_AFTER to get going
                self.goal_since[u.id] = since = ((tx, ty), msg.t)
            if (msg.t - since[1] > STUCK_AFTER and u.id in old
                    and ((u.x - old[u.id][0]) ** 2 + (u.y - old[u.id][1]) ** 2) ** 0.5 < STUCK_PROGRESS_M):
                msg.stuck.add(u.id)

    async def on_state(self, msg: State) -> None:
        # units not on the battlefield (reinforcements still to come, or withdrawn) stand at a placeholder
        # position: keep them out of everything, the general only gets a list of them
        for u in msg.units:
            if u.on_field and u.soldiers > 0:
                self.seen_on_field.add(u.id)
        msg.off_field = [u for u in msg.units if not u.on_field and u.soldiers > 0]
        msg.left_field = {u.id for u in msg.off_field if u.id in self.seen_on_field}
        msg.units = [u for u in msg.units if u.on_field or u.soldiers <= 0]
        for u in msg.units:  # the commanding character may lead an ordinary unit; it has the rally ability
            if u.rally >= 0:
                u.category = "general"
        if self.state is not None and msg.stage != self.state.stage:
            log.info("battle stage %d -> %d%s", self.state.stage, msg.stage,
                     " (deployment: no plans until the battle starts)" if msg.stage in DEPLOYMENT_STAGES else "")
            self._log_event("stage", t=msg.t, old=self.state.stage, new=msg.stage)
            if self.state.stage in DEPLOYMENT_STAGES and self.plan is not None:
                self.replan.set()  # the battle has started: re-plan at once
        self._check_sides(msg)
        self._track_losses(msg)
        self._track_stuck(msg)
        self.state = msg
        if self.fallback:  # the game's AI is commanding; don't fight it with our orders
            return
        if int(msg.t * 2) % 20 == 0:  # every ~10 s
            self._log_event("state", state=msg.model_dump(by_alias=True), losses=msg.recent_losses)
        if self.executor is None:
            return
        orders = self.executor.tick(msg, self.plan)
        msg.reflex_status = self.executor.reflexes.status(self.plan, msg)
        msg.plan_since = self.plan_since
        msg.own_paths = {k: list(v) for k, v in self.executor.paths.items()}
        self._calibrate(msg)
        if orders:
            self.writer.write(dump(Orders(orders=orders)))
            await self.writer.drain()
            log.debug("t=%.1f sent %d orders", msg.t, len(orders))

    def _calibrate(self, msg: State) -> None:
        """Every ~5 s: remember what the predictions say, and log how well older ones came true."""
        from .predict import check, snapshot

        if self.pred_snaps and msg.t < self.pred_snaps[-1]["t"]:
            self.pred_snaps.clear()  # battle time went back
        for snap in list(self.pred_snaps):
            for h in (5.0, 10.0):
                if h not in snap["checked"] and msg.t >= snap["t"] + h:
                    snap["checked"].add(h)
                    res = check(snap, msg, h)
                    if res:
                        self._log_event("prediction_check", **res)
            if len(snap["checked"]) == 2:
                self.pred_snaps.remove(snap)
        if msg.t - self.last_pred_snap >= PRED_SNAP_EVERY:
            self.last_pred_snap = msg.t
            snap = snapshot(msg, self.start)
            snap["checked"] = set()
            self.pred_snaps.append(snap)

    def retaken(self, unit: int) -> None:
        """The hook took a unit back from the game's AI: send it our order again on the next tick."""
        log.info("unit #%d was taken back from the game's AI - re-sending its order", unit)
        self._log_event("retaken", t=self.state.t if self.state else None, unit=unit)
        if self.executor is not None:
            self.executor.last.pop(unit, None)
            self.executor._forget({unit})

    def _deploying(self) -> bool:
        """The battlefield is shown but the player has not started the battle yet (deployment): no plans then.
        A safety net in case a stage value means something else: any unit in melee means the battle is on."""
        s = self.state
        return s is not None and s.stage in DEPLOYMENT_STAGES and not any(u.action == "melee" for u in s.units)

    def _in_contact(self) -> bool:
        """Any of our units within CONTACT_M of a fighting enemy unit."""
        if self.state is None or self.start is None:
            return False
        ai = self.start.ai_alliance
        ours = [u for u in self.state.units if u.alliance == ai and u.soldiers > 0 and u.action != "dead"]
        foes = [u for u in self.state.units if u.alliance != ai and u.soldiers > 0 and u.action not in ("dead", "routing")]
        return any((u.x - f.x) ** 2 + (u.y - f.y) ** 2 < CONTACT_M ** 2 for u in ours for f in foes)

    def _changed_since_plan(self) -> str | None:
        """Why the battle looks different from when the last plan was made, or None if it doesn't."""
        old, new = self.planned_state, self.state
        if old is None or self.plan is None:
            return "first plan"
        if time.monotonic() - self.last_plan_at >= QUIET_MAX_GAP:
            return "periodic"
        if self.replan.is_set():
            return "heavy losses"
        if self._in_contact():
            return f"enemy within {CONTACT_M:.0f} m"
        before = {u.id: u for u in old.units}
        for u in new.units:
            o = before.get(u.id)
            if o is None:
                return f"new unit #{u.id}"
            if o.soldiers - u.soldiers >= max(3, 0.03 * u.max_soldiers):
                return f"#{u.id} lost men"
            if (u.action in ("routing", "melee", "dead")) != (o.action in ("routing", "melee", "dead")) or (u.action == "routing") != (o.action == "routing"):
                return f"#{u.id} {o.action} -> {u.action}"
            if ((u.x - o.x) ** 2 + (u.y - o.y) ** 2) ** 0.5 > QUIET_MOVE_M:
                return f"#{u.id} moved"
        return None

    async def _plan_loop(self) -> None:
        while True:
            if self.state is None:
                await asyncio.sleep(0.2)
                continue
            if self._deploying():  # deployment: wait for the start of the battle, no model calls
                await asyncio.sleep(0.5)
                continue
            why = self._changed_since_plan()
            if why is None:  # nothing happened: keep the current plan, save a model call
                self.skipped += 1
                try:
                    await asyncio.wait_for(self.replan.wait(), self.plan_interval)
                except asyncio.TimeoutError:
                    pass
                continue
            began = time.monotonic()
            self.planned_state = self.state
            self.last_plan_at = began
            self.replan.clear()
            try:
                from .general import _user_message

                prompt = _user_message(self.start, self.state, self.plan)
                plan = await self.general.plan(self.start, self.state, self.plan)
            except Exception as e:  # keep the last plan; the executor keeps acting on it
                log.warning("general failed: %s", e)
                self._log_event("plan_error", error=str(e))
                self.failures += 1
                if self.failures >= FAILS_BEFORE_FALLBACK and not self.fallback:
                    self.fallback = True
                    self.writer.write(dump(Control(take=False)))
                    log.warning("%d failed plans in a row - handing the army back to the game's own AI", self.failures)
                    self._log_event("fallback", on=True, error=str(e))
                if self.fallback:  # don't hammer a hit usage limit; try again in a minute
                    await asyncio.sleep(60)
                    continue
            else:
                self.failures = 0
                if self.fallback:
                    self.fallback = False
                    self.writer.write(dump(Control(take=True)))
                    log.info("the general answers again - taking the army back from the game's AI")
                    self._log_event("fallback", on=False)
                self.plan = plan
                self.plan_since = self.state.t if self.state else None
                if self.state is not None:  # the next plan may be asked for before a new state comes in
                    self.state.plan_since = self.plan_since
                self.executor.new_plan(plan)
                if plan.note.strip():
                    self.start.notes.append((self.state.t, plan.note.strip()))
                if self.start.briefing and self.start.briefing != getattr(self, "_logged_briefing", None):
                    self._logged_briefing = self.start.briefing  # the static part of the report (map, river, town)
                    self._log_event("briefing", text=self.start.briefing)
                self._log_event("plan", t=self.state.t, prompt=prompt, plan=plan.model_dump(), seconds=round(time.monotonic() - began, 1),
                                why=why, skipped_before=self.skipped, usage=getattr(self.general, "last_usage", None))
                rl = getattr(self.general, "rate_limit", None)
                if rl and rl != self.last_rate_limit:  # subscription usage (claude-code backend), for the overlay
                    self.last_rate_limit = rl
                    self._log_event("rate_limit", info=rl)
                self.skipped = 0
                log.info(
                    "new plan (%.1fs, %s): %s | %s",
                    time.monotonic() - began,
                    why,
                    plan.intent,
                    "; ".join(f"{g.name}: {g.task} {g.unit_ids}->{g.target_unit or g.target_point or ''}" for g in plan.groups),
                )
            try:  # wait for the next cycle, or earlier if a unit suddenly takes heavy losses
                interval = min(self.plan_interval, CONTACT_INTERVAL) if self._in_contact() else self.plan_interval
                await asyncio.wait_for(self.replan.wait(), max(0.0, interval - (time.monotonic() - began)))
            except asyncio.TimeoutError:
                pass

    def pause(self) -> None:
        if self.planner is not None:
            self.planner.cancel()
            self.planner = None
            log.info("game paused - general waits")

    def resume(self) -> None:
        if self.start is not None and self.planner is None:
            self.planner = asyncio.create_task(self._plan_loop())
            log.info("game resumed")

    def stop(self) -> None:
        if self.planner is not None:
            self.planner.cancel()
            self.planner = None

    def _close_general(self) -> None:
        """End the model process kept for the battle (claude-code backend)."""
        close = getattr(self.general, "close", None)
        if close is not None:
            asyncio.get_event_loop().create_task(close())


def make_handler(general: General, plan_interval: float):
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        log.info("game connected from %s", peer)
        session = BattleSession(general, plan_interval, writer)
        try:
            while line := await reader.readline():
                try:
                    msg = parse_game_message(line)
                except Exception as e:
                    log.warning("bad message %r: %s", line[:200], e)
                    continue
                if isinstance(msg, Hello):
                    log.info("hello: %s v%d", msg.game, msg.version)
                elif isinstance(msg, BattleStart):
                    session.on_battle_start(msg)
                    writer.write(dump(Control(take=True)))  # hand our alliance from the game AI to us
                elif isinstance(msg, State):
                    await session.on_state(msg)
                elif isinstance(msg, RawState):
                    log.debug("raw_state with %d units (hook can't read unit fields yet)", len(msg.units))
                elif isinstance(msg, TerrainMsg):
                    if session.start is not None:
                        session.start.terrain = msg
                        session.start.briefing = None  # rebuilt with the map on the next plan
                    log.info("terrain received: %dx%d samples, heights %.0f..%.0f m", msg.n, msg.n,
                             min(msg.heights_dm) / 10, max(msg.heights_dm) / 10)
                elif isinstance(msg, TerrainMasks):
                    if session.start is not None:
                        session.start.masks = msg
                        session.start.briefing = None
                        session._log_event("terrain_masks", masks=msg.model_dump())  # for offline analysis of towns
                    log.info("terrain masks received (obstacles, settlement)")
                elif isinstance(msg, Buildings):
                    if session.start is not None:
                        from .siege import locate

                        session.start.buildings, used = locate(msg.items, session.start.masks)
                        session.start.briefing = None
                        session._log_event("buildings", items=msg.items, position_offset=used)
                        kinds = {}
                        for b in session.start.buildings.values():
                            kinds[b.kind] = kinds.get(b.kind, 0) + 1
                        log.info("buildings: %s (positions %s)", kinds, "found" if used else "unknown")
                elif isinstance(msg, Streets):
                    if session.start is not None:
                        session.start.streets = msg.instances
                        session.start.town_graph, session.start.briefing = None, None
                        n = sum(len(i.get("nodes", [])) for i in msg.instances)
                        log.info("street graph: %d plan instance(s), %d nodes", len(msg.instances), n)
                        session._log_event("streets", instances=msg.instances)
                elif isinstance(msg, Crossings):
                    river = [c for c in msg.links if c.get("kind") in (1, 4)]
                    log.info("map area links: %d, river crossings: %d", len(msg.links), len(river))
                    session._log_event("crossings", links=msg.links)
                    if session.start is not None:
                        session.start.crossings = msg.links
                        session.start.briefing = None  # rebuilt with the river
                elif isinstance(msg, PlatformsRaw):
                    log.info("other platforms: %d of %d (raw, logged)", len(msg.platforms), msg.count)
                    session._log_event("platforms_raw", count=msg.count, platforms=msg.platforms)
                elif isinstance(msg, WallsRaw):
                    if session.start is not None:
                        session.start.wall_platforms = msg.platforms
                        session.start.town_graph, session.start.briefing = None, None
                    log.info("wall platforms: %d (raw, logged)", len(msg.platforms))
                    session._log_event("walls_raw", platforms=msg.platforms)
                elif isinstance(msg, BuildingStatus):
                    if session.start is not None:
                        changed = []
                        for bid, dmg, hp, flag, *gate in msg.items:
                            b = session.start.buildings.get(bid)
                            if b is not None:
                                opened, owner = (gate + [-1, -1])[:2]
                                if (b.damage, b.flag, b.open, b.owner) != (dmg, flag, opened, owner):
                                    changed.append([bid, dmg, hp, flag, opened, owner])
                                b.damage, b.max_hp, b.flag, b.open, b.owner = dmg, hp, flag, opened, owner
                        if changed:
                            session._log_event("building_status", changed=changed)
                elif isinstance(msg, Log):
                    log.info("[hook] %s", msg.msg)
                elif isinstance(msg, Pause):
                    session.pause()
                elif isinstance(msg, Retaken):
                    session.retaken(msg.unit)
                elif isinstance(msg, Resume):
                    session.resume()
                    writer.write(dump(Control(take=True)))
                elif isinstance(msg, BattleEnd):
                    log.info("battle over, winner alliance %s", msg.winner_alliance)
                    session.stop()
                    session._close_general()
        finally:
            session.stop()
            session._close_general()
            writer.close()
            log.info("game disconnected")

    return handle


async def serve(host: str, port: int, general: General, plan_interval: float) -> None:
    server = await asyncio.start_server(make_handler(general, plan_interval), host, port, limit=MAX_LINE)
    log.info("listening on %s:%d", host, port)
    async with server:
        await server.serve_forever()


def main() -> None:
    global RULES_FILE
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--backend", choices=["claude", "claude-code", "openai", "deepseek", "rule"], default="rule")
    p.add_argument("--model")
    p.add_argument("--base-url", help="OpenAI-compatible endpoint, e.g. Ollama")
    p.add_argument("--effort", default="low", help="Claude effort: low|medium|high|xhigh|max")
    p.add_argument("--claude-bin", help="path to the claude CLI (claude-code backend)")
    p.add_argument("--fresh-process", action="store_true",
                   help="claude-code: start a new claude process for every plan instead of one per battle")
    p.add_argument("--rules", type=Path, help=f"game rules file (default {RULES_FILE}; strategy.md next to it is always used)")
    p.add_argument("--plan-interval", type=float, default=8.0, help=f"seconds between plans (while any unit is within {CONTACT_M:.0f} m of the enemy: {CONTACT_INTERVAL:.0f} s)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=7777)
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    for noisy in ("httpx", "httpcore", "anthropic", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if a.rules:
        RULES_FILE = a.rules
    general = make_general(a.backend, a.model, a.base_url, a.effort, a.claude_bin, a.fresh_process)
    asyncio.run(serve(a.host, a.port, general, a.plan_interval))


if __name__ == "__main__":
    main()
