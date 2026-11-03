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
    formation: Literal["square", "horde", "wedge", "testudo", "phalanx", "schiltrom", "shield_wall"] | None = Field(
        default=None, description="Game formation to adopt; only works if the unit type supports it (e.g. phalanx "
                                  "for hoplites/pikemen)"
    )
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


class RouteRequest(BaseModel):
    from_node: str = Field(description="TOWN GRAPH node to start from: a gate G.., a breach W.. or any street node")
    to_node: str | None = Field(default=None, description="node to reach; omitted = the town square P")
    avoid: list[str] = Field(default_factory=list, description="nodes the route must not pass through, e.g. where "
                                                               "an enemy phalanx blocks the street")


class Plan(BaseModel):
    intent: str = Field(description="One or two sentences: the overall idea of the plan")
    groups: list[GroupOrder]
    route_requests: list[RouteRequest] | None = Field(
        default=None, description="Town battles only, optional: ask for more routes (e.g. around a blocked street); "
                                  "they are shown as REQUESTED ROUTES in the next message")
