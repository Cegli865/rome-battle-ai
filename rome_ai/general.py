"""The 'general': produces a Plan from the battle state every few seconds.

Backends:
  claude  - Anthropic API (official SDK, structured output)
  openai  - any OpenAI-compatible endpoint: ChatGPT, or a local model via
            Ollama / llama.cpp / vLLM (set --base-url)
  claude-code - Claude Code CLI in headless mode (`claude -p`), runs on your
            Claude subscription login instead of an API key
  rule    - no LLM; a simple heuristic general for offline testing
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Protocol

from .describe import describe
from .geometry import alive, centroid, dist, nearest
from .plan import GroupOrder, Plan
from .protocol import BattleStart, State

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are the commanding general of an army in a real-time ancient battle \
(Rome: Total War). You receive a snapshot of the battlefield every few seconds \
and answer with a battle plan for your units.

How your plan is executed: a subordinate turns each group's task into game orders several times per \
second until your next plan. It computes positions from the parameters you give, walks units through \
your waypoints, and runs only the reflexes you switch on per group (below) - they react within a second, \
while your next plan is 10-30 s away. Units you leave out of the plan stand still. Every decision - who \
goes where, in which shape, by which way, when to wait and when to strike, and which reflexes a group \
has - is yours, so give every unit a task.

Tasks (one per group):
- move: go to target_point [x, y] and stand there. shape "line" = units side by side across the facing \
  direction (unit_ids in map order: west to east when the front faces north/south, north to south when it \
  faces east/west), "column" = one behind the other back along the way in (unit_ids \
  front to back), "none" = each unit goes to target_point itself. spacing_m = in a line the GAP between \
  the edges of neighbouring units' fronts, NOT centre to centre (2-5 m = a closed line; each unit's front width \
  is on its line as "front Nm"), in a column the distance between unit centres. facing_deg = where the front faces on arrival (0 = north, \
  90 = east). ranks = depth of each unit's formation (frontage = men / ranks; fewer ranks = wider). \
  waypoints = points to pass in order on the way (e.g. a detour around a flank); in a town give path = \
  node ids of the TOWN GRAPH instead (target_point may then be left out: the last node). Units still on the \
  way show "going to (x,y), N m left": the shape is not formed until they arrive.
  Free formation: instead of shape give positions = one [x, y, facing_deg] per unit in the order of \
  unit_ids, and each unit goes to its own point and faces its own way - any shape you like: a square with \
  fronts to all four sides when you may be surrounded, a second line or a reserve behind the centre, a wedge \
  of units, refused or bent wings, skirmishers screening the infantry, a flank leaning on a wood. Work the \
  points out from each unit's "front Nm": neighbours side by side stand front/2 + gap + front/2 apart \
  centre to centre (gap 2-5 m), a second line about 30-50 m behind the first.
- attack: every unit of the group attacks target_unit. A unit gets the charge bonus only when it runs into the \
  enemy (run=true for the last stretch); changing the target of a unit that is already charging cancels the \
  charge, so keep targets steady once units are on their way. Missile units given an attack shoot at the target \
  while they have ammunition; with melee=true they charge it hand to hand instead (e.g. horse archers hitting a \
  unit that is already fighting in its flank or rear).
- attack_building: every unit attacks target_building = a town graph node (gate G.., tower T..w, wall section W..; \
  artillery bombards it, a unit carrying a RAM batters a gate, LADDERS / a SIEGE TOWER go onto a wall section).
- halt: stop where they are.
- withdraw: the units leave the battlefield by the game's own withdraw order (they walk off the map by \
  themselves and are saved). Use it instead of marching to a map edge.
Reflexes (per group, react between your plans):
- attack_from (attack): "front" (default), or "left" / "right" / "rear" of the target as it faces - the \
  units go round to that side first, then charge. Use it to hit flanks and rear, e.g. cavalry vs a phalanx.
- on_target_lost (attack): "nearest" (default) = when the target dies or routs, each unit attacks the \
  nearest fighting enemy; "halt" = it stops and waits for you.
- engage_within_m (move, halt): a unit attacks any enemy that comes this close to its place and returns \
  to its place when none is that close (e.g. 60-100 for cavalry guarding a flank, 30-40 for a line that \
  should meet a charge, omitted = never leaves its place on its own, even when enemies come near).
- keep_range_m (any unit, any task): when an enemy comes closer than this the unit runs back to keep \
  that distance (e.g. 60-80), then carries on. This is our own reflex, not the game's skirmish mode, so it \
  works for every unit - missile units, and also the general: with keep_range_m he backs off from cavalry \
  charging him (watch TARGETED BY on his line) without waiting for your next plan.
Per group also: run (true only for charges and urgent moves; running tires units), formation (phalanx, \
testudo, wedge, schiltrom, shield_wall, square, horde - only for unit types that have it), fire_at_will and \
skirmish for missile units, use_ability (true: units use / switch on their special ability - warcry, druid chant, screeching women, \
cantabrian circle, flaming ammunition - when it is ready, or only once an enemy is closer than \
ability_enemy_within_m; false: switch a toggled one off; the unit list shows "ability ready" / "ABILITY ON", the UNIT TYPES list says \
what each ability does), rally (general's unit: RALLY ability on / off - steadies the morale of nearby units), drop_equipment (units carrying a ram / ladders / a siege tower leave it, to move \
and fight freely), last_resort to mark a group that breaks a GAME RULE because the battle is \
otherwise lost.

Coordination is yours too: to hit from several sides at once, first move the groups to their starting \
points, and only when the unit list shows them there, order the attack in a later plan. To keep a group \
together, give it one move with a shape; to flank, give waypoints around the enemy and then attack.

Attack or defend: the unit list says which side you are. The ATTACKER must go and win: battles have a time \
limit and running out of time counts as a defeat for the attacker. The DEFENDER wins on time, but only \
while the army stands: holding a position is a choice to re-check every plan, not a default. Units standing \
under missile fire lose men every second, and enemy cavalry going round a flank (watch the direction and speed of \
enemy units, e.g. "moving S 4m/s") hits the side or rear.

Terrain: when a TERRAIN MAP is included, use it. High ground is a big advantage (missiles reach further, \
charges downhill hit harder, attackers arrive tired); avoid attacking uphill into a defended position.

Towns: the TOWN GRAPH is the game's own street network: places (nodes) joined by streets (with length and \
width) - units can only move along it. Plan routes as paths of node ids. Ways in depend on the walls: \
none - anywhere; wooden - through a gate or a broken palisade section (W..); stone - a gate (ram), a \
breached wall section (straight into the street) or ladders / a siege tower onto a wall section W.. (the \
unit is then ON THE WALL: walk along the wall to a tower or gatehouse and down its STAIRS). Units carrying \
a RAM / LADDERS / A SIEGE TOWER are commanded by the game's own AI, which uses the equipment by itself (marked \
COMMANDED BY THE GAME'S AI, your orders to them are ignored); they become yours once the equipment is used, \
dropped or destroyed - with ladders / a siege tower once the men are up on the wall. In siege battles the \
game's AI also fires the artillery (onagers, catapults, ballistas) until its ammunition runs out. Only rams, artillery and sapping damage gates and walls: ordinary infantry or \
cavalry given attack_building on a gate or wall section just stands there. The briefing has a DISTANCES table between key places; each plan shows \
where every unit is - at a node, or on the street between two nodes with the metres to each of them (a \
unit standing on a street blocks it: avoid both its nodes in route_requests to go round it), outside the town \
how far from the nearest node - for your units the walking distance and \
march time to the square, and which defences are damaged or open - use them to time groups that should \
arrive together. Units that STAND STILL / STUCK did not get anywhere - send them another way. \
ROUTES TO THE SQUARE give, for every gate and every open breach, the fastest route and an alternative one \
entering the square by another street. If you need other routes (e.g. around a street an enemy phalanx \
blocks), ask with route_requests (from a node, to a node or the square, avoiding nodes): they come as \
REQUESTED ROUTES in the next message.

Taking losses: units marked UNDER FIRE are being shot without fighting back.\
"""


class General(Protocol):
    rules: str  # game rules text, loaded from rules.md before each battle
    strategy: str  # tactical advice, loaded from strategy.md before each battle

    async def plan(self, start: BattleStart, state: State, previous: Plan | None) -> Plan: ...


def rule_lines(text: str) -> str:
    """Only the bullet points of rules.md / strategy.md go to the model; headings and notes are for humans."""
    return "\n".join(line.strip() for line in text.splitlines() if line.strip().startswith(("- ", "* ")))


def system_prompt(rules: str, briefing: str | None = None, strategy: str = "") -> str:
    """Everything that stays the same for a whole battle: instructions, game rules, strategy, maps.
    Kept byte-identical between plans so the CLI/API prompt cache can reuse it."""
    out = SYSTEM_PROMPT
    rules, strategy = rule_lines(rules), rule_lines(strategy)
    if rules:
        out += (
            "\n\nGAME RULES: facts about the game and hard rules of your command. They are always true - plan "
            "with them, and break one only when the battle is otherwise lost (then set last_resort=true on the "
            "group that breaks it):\n" + rules.strip()
        )
    if strategy:
        out += (
            "\n\nSTRATEGY: advice from your commander, sound in most situations. Weigh it, but depart from it "
            "when the situation clearly calls for something else:\n" + strategy.strip()
        )
    if briefing:
        out += "\n\nBATTLEFIELD (fixed for this battle):\n" + briefing
    return out


def _near_segment(p, a, b, r: float) -> bool:
    """Is point p within r metres of the segment a-b?"""
    ax, ay = a
    dx, dy = b[0] - ax, b[1] - ay
    t = max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / (dx * dx + dy * dy or 1)))
    return ((p[0] - ax - t * dx) ** 2 + (p[1] - ay - t * dy) ** 2) ** 0.5 <= r


def _ensure_briefing(start: BattleStart, state: State) -> None:
    if start.briefing is None and (start.terrain is not None or start.masks is not None):
        from .describe import describe_static

        start.briefing = describe_static(start, state)


def _user_message(start: BattleStart, state: State, previous: Plan | None) -> str:
    _ensure_briefing(start, state)
    msg =describe(start, state, start.terrain, static=start.briefing is None)
    if previous is not None:
        msg += "\n\nYOUR CURRENT PLAN:\n" + previous.model_dump_json(indent=None, exclude_defaults=True)
        if start.town_graph is not None and not start.player_is_attacker:
            from .town_graph import approach_summary

            kind = {0: "ram", 1: "ladders", 2: "siege tower"}
            units = {u.id: u for u in state.units if u.alliance == start.ai_alliance}
            for grp in previous.groups:
                targets = [grp.target_building] if grp.task == "attack_building" and grp.target_building else (grp.path or [])
                nodes = [str(t) for t in targets
                         if (n := start.town_graph.nodes.get(str(t))) is not None and n.kind in ("gate", "wall")]
                for uid in grp.unit_ids:
                    u = units.get(uid)
                    if u is not None and u.engines > 0 and u.engine_type in kind and nodes:
                        start.siege_targets[nodes[0]] = f"{kind[u.engine_type]} #{uid}"
            # equipment the game's AI is using: the gate / wall section it stands at
            for u in units.values():
                if u.game_ai and u.engines > 0 and u.engine_type in kind:
                    near = [n for n in start.town_graph.nodes.values() if n.kind in ("gate", "wall")
                            and (n.x - u.x) ** 2 + (n.y - u.y) ** 2 < 40 ** 2]
                    if near:
                        n = min(near, key=lambda n: (n.x - u.x) ** 2 + (n.y - u.y) ** 2)
                        start.siege_targets[n.id] = f"{kind[u.engine_type]} #{u.id}"
            if start.siege_targets:
                msg += ("\nYOUR SIEGE EQUIPMENT GOES TO / STANDS AT: " + "; ".join(
                    f"{n} ({w})" for n, w in sorted(start.siege_targets.items()))
                    + " - do NOT bombard these with your artillery, you would destroy your own equipment")
                from .town_graph import to_gatehouse

                g = start.town_graph
                # units whose ladders / tower were placed: up on the wall - the way to the nearest gatehouse
                up = []
                for node, what in start.siege_targets.items():
                    if "ram" in what:
                        continue
                    uid = int(what.split("#")[1])
                    u = units.get(uid)
                    n = g.nodes.get(node)
                    if u is None or u.engines > 0 or n is None or ((u.x - n.x) ** 2 + (u.y - n.y) ** 2) ** 0.5 > 25:
                        continue
                    r = to_gatehouse(g, node)
                    if r:
                        up.append(f"#{uid} at {node} -> gatehouse {r[0][-1]} along the wall: path {r[0]} ({r[1]:.0f}m)")
                if up:
                    msg += ("\nUNITS UP ON THE WALL (their ladders / tower are placed): " + "; ".join(up)
                            + " - send them to that gatehouse to take the gate and open it for the army, do not let "
                              "them go down to fight outside")
                # artillery shots at a target behind one of those wall sections hit the section on the way
                warn = []
                for grp in previous.groups:
                    if grp.task != "attack" or grp.target_unit is None:
                        continue
                    t = next((x for x in state.units if x.id == grp.target_unit), None)
                    for uid in grp.unit_ids:
                        a = units.get(uid)
                        if a is None or a.category != "siege" or t is None:
                            continue
                        for node in start.siege_targets:
                            n = g.nodes.get(node)
                            if n is not None and _near_segment((n.x, n.y), (a.x, a.y), (t.x, t.y), 30):
                                warn.append(f"#{uid} shooting at #{t.id} fires over {node}")
                if warn:
                    msg += ("\nWARNING - YOUR ARTILLERY HITS YOUR OWN SIEGE EQUIPMENT: " + "; ".join(warn)
                            + " - shots aimed past that wall section hit it; pick a target elsewhere")

            mine = [u for u in state.units if u.alliance == start.ai_alliance]
            a = approach_summary(start.town_graph, previous, mine)
            if a:
                msg += "\n" + a
        msg += _line_gaps(start, state, previous)
        ids = {u.id: u for u in state.units}
        away = {u.id for u in state.off_field}
        bad = [f"{grp.name!r} -> #{grp.target_unit} ("
               + ("not on the battlefield" if grp.target_unit in away else
                  "your own unit" if grp.target_unit in ids else "no such unit") + ")"
               for grp in previous.groups if grp.task == "attack" and grp.target_unit is not None
               and (grp.target_unit not in ids or ids[grp.target_unit].alliance == start.ai_alliance)]
        if bad:
            msg += ("\nWRONG TARGETS in your last plan (the units attack the nearest enemy instead): " + "; ".join(bad)
                    + " - target_unit must be an ENEMY unit id")
    if previous is not None and previous.route_requests and start.town_graph is not None:
        from .town_graph import requested_routes

        rr = requested_routes(start.town_graph, start.buildings, previous.route_requests)
        if rr:
            msg += "\n" + rr
    msg += _artillery_reminder(start, state, previous)
    msg += "\n\nGive the updated plan."
    return msg


def _line_gaps(start: BattleStart, state: State, previous: Plan) -> str:
    """Holes and misalignments in the lines the general ordered (edge to edge, from each unit's real front width)."""
    from .units_db import line_gaps

    units = {u.id: u for u in state.units if u.alliance == start.ai_alliance and u.soldiers > 0}
    out = []
    for i, grp in enumerate(previous.groups):
        if grp.task not in ("move", "halt") or grp.shape != "line":
            continue
        gaps = line_gaps([units[x] for x in grp.unit_ids if x in units], grp.facing_deg)
        if gaps:
            out.append(f"group {i + 1} {[x for x in grp.unit_ids if x in units]}: " + ", ".join(gaps))
    if not out:
        return ""
    return ("\nLINE GAPS (edge to edge, as they stand now; spacing_m is this edge-to-edge gap): "
            + "; ".join(out))


def _artillery_reminder(start: BattleStart, state: State, previous: Plan | None) -> str:
    """Attacking stone walls with artillery that still has ammunition: say plainly what the strategy wants
    and what the artillery is doing now (the general tends to forget those orders among the many others)."""
    g = start.town_graph
    if g is None or start.player_is_attacker or g.walls != "stone":
        return ""
    arty = [u for u in state.units if u.alliance == start.ai_alliance and u.category == "siege" and u.engines > 0
            and (u.ammo is None or u.ammo > 0.05)]
    if not arty:
        return ""
    doing = []
    for u in arty:
        grp = next((x for x in (previous.groups if previous else []) if u.id in x.unit_ids), None)
        if grp is None:
            doing.append(f"#{u.id} has no task")
        elif grp.task == "attack_building":
            doing.append(f"#{u.id} bombards {grp.target_building}")
        elif grp.task == "attack":
            doing.append(f"#{u.id} shoots at enemy unit #{grp.target_unit}")
        else:
            doing.append(f"#{u.id} {grp.task}")
    ammo = ", ".join(f"#{u.id} {'?' if u.ammo is None else f'{u.ammo:.0%}'}" for u in arty)
    return ("\nARTILLERY vs STONE WALLS (strategy): first shoot off the artillery's ammunition at the town "
            "defences - towers that shoot your men, or wall sections away from your own siege equipment "
            "(task=attack_building, target_building=T..w / W..) - and start the assault with rams, towers and "
            f"ladders only after that. Ammunition left: {ammo}. Now: {'; '.join(doing)}.")


class ClaudeGeneral:
    rules = ""
    strategy = ""

    def __init__(self, model: str = "claude-opus-5-5", effort: str = "low") -> None:
        import anthropic

        self.client = anthropic.AsyncAnthropic()
        self.model = model
        # Battles run in real time: low effort keeps latency down. Raise it
        # (or pause the game while thinking) if the plans are too naive.
        self.effort = effort

    async def plan(self, start: BattleStart, state: State, previous: Plan | None) -> Plan:
        response = await self.client.beta.messages.parse(
            model=self.model,
            max_tokens=16000,
            system=system_prompt(self.rules, start.briefing, self.strategy),
            messages=[{"role": "user", "content": _user_message(start, state, previous)}],
            output_format=Plan,
            output_config={"effort": self.effort},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if response.stop_reason == "refusal" or response.parsed_output is None:
            raise RuntimeError(f"no plan (stop_reason={response.stop_reason})")
        return response.parsed_output


def _find_claude_cli() -> str:
    import glob
    import shutil

    found = shutil.which("claude")
    if found:
        return found
    # the copy bundled with the Claude desktop app (path changes with each update)
    bundled = sorted(glob.glob(os.path.expanduser("~/.config/Claude/claude-code/*/*/claude")), key=os.path.getmtime)
    if bundled:
        return bundled[-1]
    raise SystemExit("claude CLI not found; install Claude Code or pass --claude-bin")


class ClaudeCodeGeneral:
    rules = ""
    strategy = ""
    """Uses `claude -p` (headless Claude Code) with structured output. No tools, no session state."""

    def __init__(self, model: str = "claude-sonnet-5-5", effort: str = "low", binary: str | None = None,
                 fresh_process: bool = False) -> None:
        self.bin = binary or _find_claude_cli()
        self.plans = 0
        self.tokens_in = self.tokens_out = 0
        self.cost_usd = 0.0  # what the same calls would cost on the API; on a subscription they count toward plan limits
        self.model = model
        self.effort = effort
        self.schema = json.dumps(Plan.model_json_schema())
        # one `claude` process per battle (stream-json in/out, the conversation goes on) unless fresh_process
        self.fresh_process = fresh_process
        self.proc: asyncio.subprocess.Process | None = None
        self.proc_system: tuple | None = None  # (battle, system prompt) the running process was started for
        self.pending = 0  # messages sent to the process whose answer has not been read yet
        self.rate_limit: dict | None = None  # last rate_limit_event info: subscription 5-hour / 7-day usage

    def describe(self) -> dict:
        return {"backend": "claude-code", "model": self.model, "effort": self.effort,
                "process": "fresh per plan" if self.fresh_process else "one per battle"}

    def _args(self) -> tuple[list[str], dict | None]:
        haiku = "haiku" in self.model
        # Haiku has no effort levels, and with Claude Code's default thinking budget a plan took ~65 s;
        # MAX_THINKING_TOKENS=0 brings it to ~10 s.
        env = dict(os.environ, MAX_THINKING_TOKENS="0") if haiku else None
        effort = [] if haiku else ["--effort", self.effort]
        args = ["--model", self.model, *effort, "--json-schema", self.schema, "--tools", "",
                "--no-session-persistence", "--strict-mcp-config", "--setting-sources", ""]
        return args, env

    async def close(self) -> None:
        """End the battle's claude process (a new battle starts a new one)."""
        proc, self.proc, self.proc_system, self.pending = self.proc, None, None, 0
        if proc is not None and proc.returncode is None:
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass

    async def plan(self, start: BattleStart, state: State, previous: Plan | None) -> Plan:
        if self.fresh_process:
            return await self._plan_fresh(start, state, previous)
        _ensure_briefing(start, state)  # before the system prompt, which carries it
        system = system_prompt(self.rules, start.briefing, self.strategy)
        key = (id(start), system)  # a new battle or a changed briefing (e.g. the town graph arrived)
        if self.proc is None or self.proc.returncode is not None or self.proc_system != key:
            # a new battle, or the briefing changed (e.g. the town graph arrived): a new conversation
            await self.close()
            args, env = self._args()
            self.proc = await asyncio.create_subprocess_exec(
                self.bin, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                "--system-prompt", system, *args,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                cwd=os.path.expanduser("~"), env=env, limit=64 * 1024 * 1024,
            )
            self.proc_system = key
            log.info("claude process started for this battle (pid %d)", self.proc.pid)
        msg = {"type": "user", "message": {"role": "user", "content": _user_message(start, state, previous)}}
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())
        await self.proc.stdin.drain()
        self.pending += 1
        try:
            # answers come in order; a plan cancelled on pause leaves its answer in the stream - skip those
            while True:
                res = await asyncio.wait_for(self._read_result(), timeout=120)
                self.pending -= 1
                if self.pending <= 0:
                    self.pending = 0
                    break
                log.info("skipped a stale answer (from a plan interrupted by a pause)")
        except (asyncio.TimeoutError, RuntimeError):
            await self.close()  # don't reuse a process in an unknown state
            raise
        return self._finish(res)

    async def _read_result(self) -> dict:
        while True:
            line = await self.proc.stdout.readline()
            if not line:
                raise RuntimeError("claude process ended")
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "rate_limit_event":
                self.rate_limit = ev.get("rate_limit_info")
            if ev.get("type") == "result":
                return ev

    def _finish(self, res: dict) -> Plan:
        if res.get("is_error"):
            raise RuntimeError(f"claude: {res.get('result')}")
        u = res.get("usage") or {}
        tin = u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
        self.plans += 1
        self.tokens_in += tin
        self.tokens_out += u.get("output_tokens", 0)
        self.cost_usd += res.get("total_cost_usd") or 0.0
        log.info(
            "usage: %.1fs, %d in / %d out tokens (~$%.3f API-equivalent) | since server start: %d plans, %d in / %d out, ~$%.2f",
            (res.get("duration_ms") or 0) / 1000, tin, u.get("output_tokens", 0), res.get("total_cost_usd") or 0.0,
            self.plans, self.tokens_in, self.tokens_out, self.cost_usd,
        )
        data = res.get("structured_output")
        if data is None:  # fall back to JSON in the text result
            text = res.get("result", "")
            data = json.loads(text[text.find("{") : text.rfind("}") + 1])
        return Plan.model_validate(data)

    async def _plan_fresh(self, start: BattleStart, state: State, previous: Plan | None) -> Plan:
        """The old way: a new `claude -p` for every plan."""
        args, env = self._args()
        proc = await asyncio.create_subprocess_exec(
            self.bin, "-p", _user_message(start, state, previous),
            "--output-format", "json",
            "--system-prompt", system_prompt(self.rules, start.briefing, self.strategy),
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=os.path.expanduser("~"),
            env=env,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=120)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError("claude -p timed out")
        try:
            res = json.loads(out)
        except json.JSONDecodeError:
            raise RuntimeError(f"claude -p: unexpected output {out[:300]!r} {err[:300]!r}")
        return self._finish(res)


class OpenAIGeneral:
    rules = ""
    strategy = ""
    """ChatGPT, DeepSeek, or a local model behind an OpenAI-compatible server (Ollama: --base-url http://localhost:11434/v1).

    json_schema=False is for servers that only have JSON mode (response_format json_object), e.g. DeepSeek:
    the Plan schema then goes into the system prompt and the reply is validated here.
    """

    def __init__(self, model: str, base_url: str | None = None, api_key: str | None = None, json_schema: bool = True) -> None:
        import openai

        if api_key is None and base_url is not None:
            api_key = os.environ.get("OPENAI_API_KEY", "local")  # local servers accept anything
        self.client = openai.AsyncOpenAI(base_url=base_url, api_key=api_key)
        self.model = model
        self.json_schema = json_schema

    async def plan(self, start: BattleStart, state: State, previous: Plan | None) -> Plan:
        if not self.json_schema:
            return await self._plan_json_mode(start, state, previous)
        response = await self.client.chat.completions.parse(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt(self.rules, start.briefing, self.strategy)},
                {"role": "user", "content": _user_message(start, state, previous)},
            ],
            response_format=Plan,
        )
        parsed = response.choices[0].message.parsed
        if parsed is None:
            raise RuntimeError("no plan from model")
        return parsed

    async def _plan_json_mode(self, start: BattleStart, state: State, previous: Plan | None) -> Plan:
        from pydantic import ValidationError

        system = (
            system_prompt(self.rules, start.briefing, self.strategy)
            + "\n\nAnswer with a single json object that matches this JSON Schema (no other text):\n"
            + json.dumps(Plan.model_json_schema(), separators=(",", ":"))
            + '\nExample: {"intent":"Hold the hill, cavalry on the left flank.","groups":[{"name":"line","unit_ids":[65537,65538],'
            '"task":"move","target_point":[120,-300],"shape":"line","spacing_m":4,"facing_deg":180},'
            '{"name":"horse","unit_ids":[65540],"task":"move","target_point":[60,-420],"waypoints":[[40,-350]],"run":true}]}'
        )
        last_error = ""
        for _ in range(2):  # JSON mode can return empty or invalid content now and then
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": _user_message(start, state, previous)},
                ],
                response_format={"type": "json_object"},
                max_tokens=4000,
            )
            text = response.choices[0].message.content or ""
            u = response.usage
            if u is not None:
                log.info("usage: %d in (%s cached) / %d out tokens", u.prompt_tokens,
                         getattr(u, "prompt_cache_hit_tokens", "?"), u.completion_tokens)
            try:
                return Plan.model_validate_json(text)
            except ValidationError as e:
                last_error = f"invalid plan JSON: {str(e)[:300]} | {text[:200]!r}"
        raise RuntimeError(last_error)


class RuleGeneral:
    rules = ""  # ignored
    strategy = ""
    """Heuristic general for offline tests: infantry and general attack the nearest main enemy, cavalry
    goes round a flank (waypoint) onto missile troops, missile units shoot from where they are."""

    async def plan(self, start: BattleStart, state: State, previous: Plan | None) -> Plan:
        ai = start.ai_alliance
        mine = alive(u for u in state.units if u.alliance == ai)
        foes = alive(u for u in state.units if u.alliance != ai)
        if not mine or not foes:
            return Plan(intent="nothing to do", groups=[])
        foe_missiles = [f for f in foes if f.category == "missile"]
        foe_main = [f for f in foes if f.category in ("infantry", "general")] or foes
        groups: list[GroupOrder] = []
        inf = [u for u in mine if u.category in ("infantry", "general")]
        if inf:
            tgt = nearest(_proxy(centroid(inf), inf[0]), foe_main)
            groups.append(GroupOrder(name="infantry", unit_ids=[u.id for u in inf], task="attack", target_unit=tgt.id))
        cav = [u for u in mine if u.category == "cavalry"]
        if cav:
            prey = min(foe_missiles or foe_main, key=lambda f: min(dist(c, f) for c in cav))
            cx, cy = centroid(cav)
            side = 1 if cx >= prey.x else -1  # go round on the side the cavalry already is
            groups.append(GroupOrder(name="horse", unit_ids=[c.id for c in cav], task="move", run=True,
                                     waypoints=[[prey.x + side * 200, prey.y]], target_point=[prey.x, prey.y]))
            if min(dist(c, prey) for c in cav) < 120:
                groups[-1] = GroupOrder(name="horse", unit_ids=[c.id for c in cav], task="attack", target_unit=prey.id,
                                        run=True, attack_from="rear")
        miss = [u for u in mine if u.category == "missile"]
        if miss:
            tgt = nearest(_proxy(centroid(miss), miss[0]), foes)
            groups.append(GroupOrder(name="archers", unit_ids=[u.id for u in miss], task="attack", target_unit=tgt.id,
                                     fire_at_will=True, skirmish=True, keep_range_m=60))
        rest = [u for u in mine if u.category in ("siege", "other")]
        if rest:
            groups.append(GroupOrder(name="rest", unit_ids=[u.id for u in rest], task="halt"))
        return Plan(intent="pin them with the infantry, cavalry round the flank", groups=groups)


def _proxy(xy: tuple[float, float], like):
    return like.model_copy(update={"x": xy[0], "y": xy[1]})


def make_general(backend: str, model: str | None, base_url: str | None, effort: str, claude_bin: str | None = None,
                 fresh_process: bool = False) -> General:
    if backend == "claude":
        return ClaudeGeneral(model or "claude-opus-5-5", effort)
    if backend == "claude-code":
        return ClaudeCodeGeneral(model or "claude-sonnet-5-5", effort, claude_bin, fresh_process)
    if backend == "openai":
        if not model:
            raise SystemExit("--model is required for the openai backend (e.g. gpt-5 or qwen3:14b)")
        return OpenAIGeneral(model, base_url)
    if backend == "deepseek":
        key = os.environ.get("DEEPSEEK_API_KEY")
        if not key:
            raise SystemExit("set DEEPSEEK_API_KEY (platform.deepseek.com) for the deepseek backend")
        return OpenAIGeneral(model or "deepseek-flash", base_url or "https://api.deepseek.com", key, json_schema=False)
    if backend == "rule":
        return RuleGeneral()
    raise SystemExit(f"unknown backend {backend!r}")
