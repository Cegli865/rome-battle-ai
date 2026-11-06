"""The general's battle plan: what every group of units does until the next plan.

The general (LLM) takes every decision, including the geometry of formations and the
routes; the executor only turns these parameters into per-unit game orders.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Task = Literal[
    "move",  # go to target_point (via waypoints / a path of town graph nodes) and stand there in the given shape
    "attack",  # every unit of the group attacks target_unit
    "attack_building",  # every unit attacks target_building (gate, wall section, tower)
    "halt",  # stop where they are
    "withdraw",  # leave the battlefield (the game's own withdraw order: the unit walks off the map and is safe)
]


class Target(BaseModel):
    """Which units a reflex watches: the reflex's own group, any unit of that side, listed units, or named groups."""

    kind: Literal["self", "any", "units", "groups"] = Field(
        default="any", description="self = the units of this group (ally_target only); any = every unit of that "
                                   "side; units = the ids in units; groups = every unit of the named groups (from "
                                   "unit_groups, or names of groups of this plan)")
    units: list[int] | None = None
    groups: list[str] | None = None


ReflexEvent = Literal[
    "near",  # an enemy of enemy_target comes within within_m of an ally of ally_target (at_least enemies)
    "engaged",  # an ally of ally_target fights an enemy of enemy_target hand to hand
    "routed",  # side=ally: an ally of ally_target routs or dies; side=enemy: an enemy of enemy_target does
    "in_zone",  # at_least enemies of enemy_target are in zone
    "morale_below",  # an ally of ally_target has morale below value (0..1)
    "arrived",  # this group has reached the place of its task
    "after",  # seconds have passed since this plan
    "out_of_ammo",  # an ally of ally_target has run out of missiles
    "predicted_contact",  # an enemy of enemy_target is predicted to reach an ally of ally_target within seconds
]

ReflexOrderType = Literal[
    "attack",  # attack the enemy that set the event off (or the nearest of enemy_target)
    "move",  # go to target_point
    "fall_back",  # back away distance_m from the enemy that set the event off
    "hold",  # stop where they are
    "withdraw",  # leave the battlefield
    "ability",  # switch the special ability on (on=true) or off
    "rally",  # the general's RALLY on / off
    "formation",  # adopt formation
]


class Event(BaseModel):
    type: ReflexEvent
    within_m: float | None = Field(default=None, description="near")
    at_least: int | None = Field(default=None, description="near / in_zone: how many enemies (default 1)")
    zone: str | None = Field(default=None, description="in_zone: a river crossing 'C1' (on it or at its exit on "
                                                       "your bank), the town square 'P', a town graph node id, or "
                                                       "a circle 'x,y,r' in metres")
    side: Literal["ally", "enemy"] | None = Field(default=None, description="routed: whose unit")
    value: float | None = Field(default=None, description="morale_below: 0..1")
    seconds: float | None = Field(default=None, description="after / predicted_contact")


class ReflexOrder(BaseModel):
    type: ReflexOrderType
    target_point: list[float] | None = Field(default=None, description="move: [x, y]")
    facing_deg: float | None = Field(default=None, description="move: front direction on arrival")
    distance_m: float | None = Field(default=None, description="fall_back (default 60)")
    attack_from: Literal["front", "left", "right", "rear"] | None = None
    run: bool = True
    melee: bool = Field(default=False, description="attack: missile units charge hand to hand")
    charge_without_phalanx: bool = Field(default=False, description="attack, phalanx / schiltrom units: charge at a run all the way and fight without the phalanx (a deliberate choice; by default they walk the last metres and fight in it)")
    on: bool | None = Field(default=None, description="ability / rally")
    formation: Literal["square", "horde", "wedge", "shield_wall"] | None = None


class Reflex(BaseModel):
    """When the event happens, the group drops its task and carries out the order instead (until the next plan).
    Checked twice a second by the executor, without asking you; each reflex fires once."""

    ally_target: Target = Field(default_factory=lambda: Target(kind="self"))
    enemy_target: Target = Field(default_factory=Target)
    event: Event
    order: ReflexOrder


class Lookahead(BaseModel):
    """One step of a group's look ahead: what you expect by then, and the reflex that answers it (or none)."""

    in_s: Literal[2, 5, 10] = Field(description="seconds from now: one entry each for 2, 5 and 10")
    expect: str = Field(description="what you expect this group to face or do by then, in a few words "
                                    "(e.g. 'enemy cavalry #10007 reaches our right flank', 'nothing new')")
    reflex: Reflex | None = Field(default=None, description="the conditional order that answers it the moment it "
                                                            "happens; null when nothing needs answering")


class UnitGroup(BaseModel):
    name: str
    units: list[int] = Field(description="unit ids, yours or the enemy's; a unit can be in several groups")


class GroupOrder(BaseModel):
    name: str = Field(description="Short label, e.g. 'left cavalry'")
    unit_ids: list[int] = Field(description="Units of the group. shape=line: west to east if the front faces "
                                            "north or south, north to south if it faces east or west (map "
                                            "directions, not the troops' left/right); shape=column: front to back")
    task: Task
    target_point: list[float] | None = Field(default=None, description="move: [x, y] in metres - centre of the line "
                                                                       "or head of the column; omitted with a path = "
                                                                       "its last node")
    shape: Literal["line", "column", "none"] = Field(
        default="none",
        description="move: line = side by side across the facing direction; column = one behind the other along the "
                    "way in; none = every unit goes to target_point itself",
    )
    spacing_m: float = Field(default=5.0, description="move: line = the GAP between the edges of neighbouring units' "
                                                      "fronts, NOT centre to centre (2-5 = a closed line, more leaves "
                                                      "holes); column = distance between unit centres. Metres")
    positions: list[list[float]] | None = Field(
        default=None, description="move: free formation - one [x, y] or [x, y, facing_deg] per unit, in the order of "
                                  "unit_ids: each unit goes to its own point (and turns its front that way). Any "
                                  "shape: a square facing all four sides, two lines, a wedge, a reserve behind the "
                                  "centre, a screen of skirmishers. Replaces shape/spacing_m/target_point; a "
                                  "point without facing uses facing_deg")
    facing_deg: float | None = Field(default=None, description="move: direction the front faces on arrival, degrees, "
                                                               "0 = north (+y), 90 = east; omitted = direction of march")
    ranks: int | None = Field(default=None, description="move: depth of each unit's formation in ranks (frontage = "
                                                        "men / ranks); omitted = the game's default")
    waypoints: list[list[float]] | None = Field(default=None, description="move: [[x, y], ...] points to pass on the "
                                                                          "way, in order, before target_point")
    path: list[str] | None = Field(default=None, description="move, in a town: node ids of the TOWN GRAPH to walk "
                                                             "through in order, e.g. ['G4', '85', '79', 'P'] or "
                                                             "['W27', 'T5w', '51', 'P']; used instead of waypoints")
    target_unit: int | None = Field(default=None, description="attack: enemy unit id")
    attack_from: Literal["front", "left", "right", "rear"] = Field(
        default="front", description="attack: side of the target to hit (its own left/right/rear, from the way it "
                                     "faces); for left/right/rear the units first go round to that side, then charge")
    on_target_lost: Literal["nearest", "halt"] = Field(
        default="nearest", description="attack: when the target dies or routs, each unit attacks the nearest "
                                       "fighting enemy (nearest) or stops (halt)")
    engage_within_m: float | None = Field(
        default=None, description="move/halt: reflex - a unit attacks any enemy that comes closer than this to "
                                  "its place, and goes back to its place when no enemy is that close; omitted = "
                                  "never leaves its place on its own")
    keep_range_m: float | None = Field(
        default=None, description="Reflex for any unit (any task except a melee attack, where it is ignored) - our "
                                  "own, not the game's skirmish mode: when an "
                                  "enemy comes closer than this, the unit runs back to keep this distance, then "
                                  "carries on with its task. Useful for missile units, and for the general to keep "
                                  "away from charging cavalry")
    target_building: int | str | None = Field(default=None, description="attack_building: town graph node of a gate, "
                                                                         "tower or wall section (e.g. 'G1', 'T5w', "
                                                                         "'W27')")
    run: bool = False
    formation: Literal["square", "horde", "wedge", "shield_wall"] | None = Field(
        default=None, description="Game formation to adopt; only works if the unit type supports it (phalanx and "
                                  "schiltrom are not set here: the executor handles them)"
    )
    charge_without_phalanx: bool = Field(default=False, description="attack, phalanx / schiltrom units: charge at a run all the way and fight without the phalanx (a deliberate choice; by default they walk the last metres and fight in it)")
    melee: bool = Field(default=False, description="attack: missile units (archers, skirmishers, horse archers) charge "
                                                   "the target in hand-to-hand combat instead of shooting at it")
    fire_at_will: bool | None = Field(default=None, description="Missile units: shoot at will (true) or only on order")
    skirmish: bool | None = Field(default=None, description="Missile units: skirmish mode (the game keeps them out of melee)")
    use_ability: bool | None = Field(
        default=None, description="true: units of the group switch on / use their special ability (warcry, druid "
                                  "chant, screeching women, cantabrian circle, flaming ammunition) as soon as it is "
                                  "ready; false: switch a toggled ability (e.g. flaming ammunition) off")
    ability_enemy_within_m: float | None = Field(
        default=None, description="With use_ability=true: use it only once the nearest enemy is closer than this "
                                  "(e.g. 40 for a warcry just before the charge); omitted = as soon as it is ready")
    rally: bool | None = Field(
        default=None, description="General's unit only: switch its RALLY ability on (true) or off (false) - it "
                                  "steadies the morale of nearby units")
    drop_equipment: bool = Field(
        default=False, description="Units of the group carrying a ram, ladders or a siege tower leave it on the "
                                   "spot (they move and fight faster without it, but can no longer use it)")
    last_resort: bool = Field(
        default=False, description="Marks a group that breaks a GAME RULE because the battle is otherwise lost."
    )
    lookahead: list[Lookahead] = Field(
        min_length=3, max_length=3,
        description="REQUIRED for every group: exactly three entries, for 2, 5 and 10 s ahead - what you expect "
                    "this group to face by then and a reflex to answer it (null if nothing needs answering)")
    reflexes: list[Reflex] | None = Field(
        default=None, description="Conditional orders: if the event happens before your next plan, the group "
                                  "switches to the order at once (e.g. charge the enemy that reaches the bridge "
                                  "exit, fall back when cavalry comes within 80 m)")


class RouteRequest(BaseModel):
    from_node: str = Field(description="TOWN GRAPH node to start from: a gate G.., a breach W.. or any street node")
    to_node: str | None = Field(default=None, description="node to reach; omitted = the town square P")
    avoid: list[str] = Field(default_factory=list, description="nodes the route must not pass through, e.g. where "
                                                               "an enemy phalanx blocks the street")


class Plan(BaseModel):
    intent: str = Field(description="One or two sentences: the overall idea of the plan")
    note: str = Field(description="A short note (1-2 sentences) to yourself for your next plans - your memory of "
                                  "this battle: what you are trying over the next minute, what the enemy keeps "
                                  "doing, what worked or failed (e.g. 'his cavalry went round our west end twice - "
                                  "keep the reserve there'; 'chasing over the bridge cost us 2 units - stay on our "
                                  "bank'). Your last notes are shown in every report")
    groups: list[GroupOrder]
    unit_groups: list[UnitGroup] | None = Field(
        default=None, description="Optional named sets of units of either side, for reflex targets (e.g. 'enemy "
                                  "cavalry', 'right flank'); a unit can be in several. Redefine them in every plan")
    route_requests: list[RouteRequest] | None = Field(
        default=None, description="Town battles only, optional: ask for more routes (e.g. around a blocked street); "
                                  "they are shown as REQUESTED ROUTES in the next message")
