"""Wire protocol between the in-game hook (.so) and the Python AI process.

Transport: TCP on 127.0.0.1, newline-delimited JSON (one message per line).
The Python side is the server; the game hook connects as a client.

game -> ai:  hello, battle_start, state, battle_end
ai -> game:  orders

Order commands map 1:1 onto the game's own battle script commands
(unit_order_move, unit_order_attack_unit, unit_order_halt, ...), so the hook
only has to forward them to the matching engine handler.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field

PROTOCOL_VERSION = 1

# --- game -> ai -------------------------------------------------------------

UnitCategory = Literal["infantry", "cavalry", "missile", "siege", "general", "other"]
UnitClass = Literal["light", "heavy", "missile", "spearmen", "skirmish", "other"]
UnitAction = Literal["idle", "moving", "running", "melee", "shooting", "routing", "dead"]


class Unit(BaseModel):
    id: int
    alliance: int  # 0 = player side, 1 = AI side (as in the game's alliance index)
    name: str  # e.g. "roman hastati"
    category: UnitCategory
    cls: UnitClass = Field(alias="class")
    x: float  # metres, battle-map coordinates
    y: float
    heading: float  # degrees, 0 = +y ("north"), clockwise
    soldiers: int
    max_soldiers: int
    morale: float  # 0..1
    fatigue: float  # 0 = fresh, 1 = exhausted
    ammo: float | None = None  # 0..1, None for units without missiles
    formation: str = "unknown"  # current FORMATION_TYPE: square, phalanx, testudo, wedge, schiltrom, ...
    engines: int = 0  # siege engines carried (ram, ladders, siege tower)
    engine_type: int = -1  # raw SIEGE_ENGINE_TYPE
    ability: int = 0  # special ability type (0 = none; see units_db for what it is)
    ability_ready: bool = False  # can be used now
    ability_on: bool = False  # active now
    rally: int = -1  # general's rally ability: -1 not a general, 0 off, 1 on
    width_men: int = 0  # current formation width in men (files); frontage = width_men * the type's file spacing
    walk_speed: float = 0.0  # the type's top speed walking / running, m/s, from the game (0 = unknown)
    run_speed: float = 0.0
    charge_speed: float = 0.0  # speed of the last stretch of a charge (the "charge" animation), m/s
    dest: list[float] | None = None  # final move destination in the unit's order queue (whoever gave the order)
    order_run: bool = False  # its current order (move or attack) is at a run
    game_ai: bool = False  # left to the game's own AI while it uses its siege equipment (our orders are ignored)
    on_field: bool = True  # False: not on the battlefield (reinforcements not arrived yet, or withdrawn)
    action: UnitAction = "idle"
    target: int | None = None  # unit id this unit is engaging, if any

    model_config = {"populate_by_name": True}


class MapInfo(BaseModel):
    # Whole terrain size; the part units can actually use is `playable`.
    width: float = 4000.0
    height: float = 4000.0
    # Playable area read from the game (BATTLE_MAP): |x| <= playable[0], |y| <= playable[1]
    playable: list[float] | None = None
    # Coarse heightfield (rows = y, cols = x), metres. Optional, sent once.
    heights: list[list[float]] | None = None
    notes: str = ""  # free text: "river crossing", ...
    settlement: bool = False  # siege / street battle inside a town
    plaza: list[float] | None = None  # town square [x, y, size_m] read from the game (BATTLE_LOCATION)


class Hello(BaseModel):
    type: Literal["hello"] = "hello"
    version: int = PROTOCOL_VERSION
    game: str = "rome-remastered"


class BattleStart(BaseModel):
    type: Literal["battle_start"] = "battle_start"
    ai_alliance: int = 1
    player_is_attacker: bool = True
    battle_type: int = 2  # game conflict type: 0 successful ambush, 1 failed ambush, 2 normal, 3 siege, 4 sally
    can_withdraw: bool | None = None  # may the AI's units leave this battle with the withdraw order (None = unknown)
    map: MapInfo = MapInfo()
    # Filled in by the server when the hook's terrain message arrives (not part of the wire message).
    terrain: TerrainMsg | None = Field(default=None, exclude=True)
    masks: TerrainMasks | None = Field(default=None, exclude=True)
    # Chokepoints (label, x, y, width) shown to the general in the latest prompt.
    chokepoints: list = Field(default_factory=list, exclude=True)
    plaza: tuple | None = Field(default=None, exclude=True)  # town square (x, y)
    # Static battle description (maps, chokepoints) built once; goes into the cached system prompt.
    briefing: str | None = Field(default=None, exclude=True)
    buildings: dict = Field(default_factory=dict, exclude=True)  # id -> siege.Building (walls, gates, towers)
    # Town attack routes shown to the general: [(label, path goal-first, staging (x, y))], from where the army was.
    routes: list = Field(default_factory=list, exclude=True)
    streets: list | None = Field(default=None, exclude=True)  # Streets.instances from the hook
    wall_platforms: list | None = Field(default=None, exclude=True)  # WallsRaw.platforms from the hook
    crossings: list | None = Field(default=None, exclude=True)  # Crossings.links from the hook
    notes: list = Field(default_factory=list, exclude=True)  # (battle time, the general's note to himself) per plan
    crossing_banks: dict = Field(default_factory=dict, exclude=True)  # crossing index -> (your bank, enemy bank) at the start
    town_graph: object | None = Field(default=None, exclude=True)
    # town graph node of a gate / wall section -> "siege tower #id" etc.: where our own siege equipment went
    siege_targets: dict = Field(default_factory=dict, exclude=True)  # town_graph.TownGraph, built for the briefing
    routes_from: tuple | None = Field(default=None, exclude=True)  # where the army stood when routes were built


class State(BaseModel):
    type: Literal["state"] = "state"
    t: float  # battle time, seconds
    stage: int = -1  # the game's battle stage (BATTLE +0): 3 = deployment, 4 = the battle runs; -1 unknown
    units: list[Unit]
    # Filled in by the server, not sent by the hook: men lost per unit over the last LOSS_WINDOW seconds.
    recent_losses: dict[int, int] = Field(default_factory=dict, exclude=True)
    # Filled in by the server: units with a move order that made no progress for STUCK_AFTER seconds.
    stuck: set[int] = Field(default_factory=set, exclude=True)
    # Filled in by the server: where units with an unfinished move order are going (final point of the move).
    goals: dict[int, tuple[float, float]] = Field(default_factory=dict, exclude=True)
    # Filled in by the server: average velocity (m/s east, m/s north) over the last few seconds.
    velocity: dict[int, tuple[float, float]] = Field(default_factory=dict, exclude=True)
    # Filled in by the server: course of each moving enemy unit (m/s east, m/s north) over the last TREND_WINDOW
    # seconds, recent seconds weighted more; the predictions use it where the unit's order is not known.
    trend: dict[int, tuple[float, float]] = Field(default_factory=dict, exclude=True)
    # Filled in by the server: the plan's conditional orders, fired and waiting (lines for the report).
    reflex_status: list[str] = Field(default_factory=list, exclude=True)
    # Filled in by the server: remaining waypoints of our units (from the executor), for the predictions.
    own_paths: dict[int, list[tuple[float, float]]] = Field(default_factory=dict, exclude=True)
    # Filled in by the server: change per second of ammunition and fatigue (0..1) over the last ~10 s.
    ammo_rate: dict[int, float] = Field(default_factory=dict, exclude=True)
    fatigue_rate: dict[int, float] = Field(default_factory=dict, exclude=True)
    # units with men that are not on the battlefield (taken out of `units` by the server), and which of them
    # have left it (withdrawn) rather than not arrived yet
    off_field: list = Field(default_factory=list, exclude=True)
    left_field: set[int] = Field(default_factory=set, exclude=True)


class BattleEnd(BaseModel):
    type: Literal["battle_end"] = "battle_end"
    winner_alliance: int | None = None


class Pause(BaseModel):
    """No battle ticks for a few seconds: game paused, in a menu, or the battle was left."""

    type: Literal["pause"] = "pause"


class Log(BaseModel):
    """A line from the hook's log, mirrored over the socket."""

    type: Literal["log"] = "log"
    msg: str


class TerrainMsg(BaseModel):
    """Coarse PHYSICAL_GRID sample from the hook; see terrain.Terrain for the decoded form."""

    type: Literal["terrain"] = "terrain"
    n: int
    step: float
    origin: float
    base: float
    min: float = 0.0
    max: float = 0.0
    heights_dm: list[int]  # row-major, row j = y, decimetres; samples cover origin .. origin + n * step
    cells_hex: str = ""  # raw PHYSICAL_GRID cell bytes (32 per sample), for decoding ground types later


class TerrainMasks(BaseModel):
    """Full-resolution PHYSICAL_GRID masks, 1 bit per 2 m cell (row-major, row = y, LSB first), base64."""

    type: Literal["terrain_masks"] = "terrain_masks"
    cells: int
    cell_size: float
    origin: float
    obstacles_b64: str  # building, wall or tree in the cell
    settlement_b64: str  # cell is inside the settlement


BattleStart.model_rebuild()


class Buildings(BaseModel):
    """Every battle building (BUILDING_BATTLE) once per battle: id, kind (0 plain 1 major 2 wall 3 tower 4 gate),
    damage/max_hp and the raw object bytes (the position is found by siege.locate)."""

    type: Literal["buildings"] = "buildings"
    items: list[dict]


class BuildingStatus(BaseModel):
    type: Literal["building_status"] = "building_status"
    items: list[list[int]]  # [id, hit points left, max_hp, flag(, gate open, alliance controlling the gate)]


class Streets(BaseModel):
    """The town's street graph from the game's own pathfinder (BATTLE_MAP_STREETS), once per battle.
    Per plan instance: nodes [[x, y], ...] in world metres, links [[node a, node b, width m, length m], ...]."""

    type: Literal["streets"] = "streets"
    instances: list[dict]


class WallsRaw(BaseModel):
    """Raw bytes of the wall platforms (BATTLE_MAP_WALLS) and their platform links, logged for decoding."""

    type: Literal["walls_raw"] = "walls_raw"
    platforms: list[dict]


class PlatformsRaw(BaseModel):
    """Raw bytes of the non-wall PHYSICAL_PLATFORMs (tower floors etc.), logged for decoding."""

    type: Literal["platforms_raw"] = "platforms_raw"
    count: int = 0
    platforms: list[dict]


class Crossings(BaseModel):
    """Links between map areas (BATTLE_MAP_AREAS): kind 1 and 4 are river crossings (bridges / fords)."""

    type: Literal["crossings"] = "crossings"
    links: list[dict]


class Resume(BaseModel):
    type: Literal["resume"] = "resume"


class Retaken(BaseModel):
    """The game's AI had taken one of our units back (reinforcements entering the field) and the hook took it
    again: whatever the unit is doing now is the game AI's order, so ours has to be sent again."""

    type: Literal["retaken"] = "retaken"
    unit: int


class RawUnit(BaseModel):
    id: int  # (alliance + 1) * 10000 + army * 100 + index
    alliance: int
    army: int
    index: int
    controller: int  # UNIT+0x34: 1 = game AI, 2 = script (us)
    flags: int  # UNIT+0xd50: 0x2 fire at will, 0x4 skirmish


class RawState(BaseModel):
    """What the hook can send while unit field offsets are still being discovered."""

    type: Literal["raw_state"] = "raw_state"
    t: float
    units: list[RawUnit]


GameMessage = Annotated[Union[Hello, BattleStart, State, RawState, BattleEnd, Pause, Resume, Retaken, Log, TerrainMsg, TerrainMasks, Buildings, BuildingStatus, Streets, WallsRaw, PlatformsRaw, Crossings], Field(discriminator="type")]


class _GameEnvelope(BaseModel):
    msg: GameMessage


def parse_game_message(line: str | bytes) -> Hello | BattleStart | State | RawState | BattleEnd | Pause | Resume | Retaken | Log | TerrainMsg | TerrainMasks | Buildings | BuildingStatus | Streets | WallsRaw | PlatformsRaw | Crossings:
    import json

    return _GameEnvelope.model_validate({"msg": json.loads(line)}).msg


# --- ai -> game -------------------------------------------------------------


class Move(BaseModel):
    cmd: Literal["move"] = "move"  # unit_order_move (unit_order_move_to_orientation when facing is set)
    unit: int
    x: float
    y: float
    run: bool = False
    facing: float | None = None  # degrees, 0 = north (+y), clockwise; front of the unit when it arrives
    width: int | None = None  # frontage in men (ranks = soldiers / width)


class Attack(BaseModel):
    cmd: Literal["attack"] = "attack"  # unit_order_attack_unit
    unit: int
    target: int
    run: bool = True
    melee: bool = False  # missile units: charge in hand to hand instead of shooting (the game's ATTACK_TYPE)


class AttackBuilding(BaseModel):
    cmd: Literal["attack_building"] = "attack_building"  # UNIT::order_attack(BUILDING_BATTLE*): gates, walls, towers
    unit: int
    building: int
    run: bool = False


class Halt(BaseModel):
    cmd: Literal["halt"] = "halt"  # unit_order_halt
    unit: int


class SpecialAbility(BaseModel):
    cmd: Literal["special_ability"] = "special_ability"  # unit_use_special_ability (warcry, chant, ...)
    unit: int


class Withdraw(BaseModel):
    cmd: Literal["withdraw"] = "withdraw"  # UNIT::order_withdraw: the unit leaves the battlefield
    unit: int


class Rally(BaseModel):
    cmd: Literal["rally"] = "rally"  # general's rally ability on / off
    unit: int
    on: bool = True


class DropEngine(BaseModel):
    cmd: Literal["drop_engine"] = "drop_engine"  # UNIT::perform_order_unit_drop_engine: leave the ram/ladders/tower
    unit: int


class Turn(BaseModel):
    cmd: Literal["turn"] = "turn"  # unit_order_turn
    unit: int
    heading: float


class Formation(BaseModel):
    cmd: Literal["formation"] = "formation"  # unit_order_change_formation
    unit: int
    # FORMATION_TYPE: horde 0, column 1, square 2, wedge 3, square_hollow 4, testudo 5, phalanx 6, schiltrom 7, shield_wall 8.
    # Only formations the unit supports have an effect (phalanx for pikes, testudo for legionaries, ...).
    formation: Literal["square", "horde", "column", "wedge", "square_hollow", "testudo", "phalanx", "schiltrom", "shield_wall"]


class FireAtWill(BaseModel):
    cmd: Literal["fire_at_will"] = "fire_at_will"  # unit_set_fire_at_will_mode
    unit: int
    on: bool


class Skirmish(BaseModel):
    cmd: Literal["skirmish"] = "skirmish"  # unit_set_skirmish_mode
    unit: int
    on: bool


Order = Annotated[Union[Move, Attack, AttackBuilding, Halt, DropEngine, SpecialAbility, Rally, Withdraw, Turn, Formation, FireAtWill, Skirmish], Field(discriminator="cmd")]


class Orders(BaseModel):
    type: Literal["orders"] = "orders"
    orders: list[Order]


class Control(BaseModel):
    """Take the AI alliance away from the game's AI (take=True) or hand it back; with unit=id only that unit."""

    type: Literal["control"] = "control"
    take: bool
    unit: int | None = None


def dump(msg: BaseModel) -> bytes:
    return (msg.model_dump_json(by_alias=True, exclude_none=True) + "\n").encode()
