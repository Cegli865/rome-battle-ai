"""Unit types from the game's own data (export_descr_unit.txt), read-only.

Gives the general what each unit type in the battle is: stats, formations and special abilities.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)

GAME_DATA = Path(os.environ.get(
    "ROME_DATA", Path.home() / ".steam/debian-installation/steamapps/common/Total War ROME REMASTERED/share/data"))
# Rome first, then the expansions; the first definition of a type name wins
EDU_FILES = ["data/export_descr_unit.txt", "bi/data/export_descr_unit.txt", "alexander/data/export_descr_unit.txt"]

# attributes worth telling the general about (the rest - sea_faring, no_custom, ... - is campaign-only)
ATTRIBUTES = {
    "warcry": "WARCRY ability (use_ability: big attack bonus for a short time, unit stands still while shouting)",
    "druid": "DRUID CHANT ability (use_ability: raises the morale of nearby friendly units - only while the druids stand still; walking stops the chant)",
    "screeching_women": "SCREECHING WOMEN curse (use_ability: lowers the morale of nearby enemies - only while the women stand still; walking stops it)",
    "cantabrian_circle": "CANTABRIAN CIRCLE ability (use_ability: horse archers ride in a circle and shoot)",
    "frighten_foot": "frightens enemy infantry",
    "frighten_mounted": "frightens enemy cavalry",
    "can_run_amok": "may run amok and attack its own side when panicked",
    "hardy": "hardy (tires slowly)",
    "very_hardy": "very hardy (tires very slowly)",
    "hide_forest": "hides in forest",
    "hide_improved_forest": "hides well in forest",
    "hide_long_grass": "hides in long grass",
    "hide_anywhere": "hides anywhere",
    "can_sap": "can dig sap tunnels under walls",
    "command": "inspires nearby units",
    "general_unit": "general's bodyguard",
}
WEAPON_ATTR = {"ap": "armour-piercing", "bp": "body-piercing", "spear": "spear (good against cavalry)",
               "long_pike": "long pike", "short_pike": "short pike", "light_spear": "light spear",
               "prec": "throws before charging", "thrown": "thrown weapon", "launching": "launches",
               "area": "area attack", "fire": "fire"}


def _parse(text: str) -> dict[str, dict]:
    units: dict[str, dict] = {}
    cur: dict | None = None
    for raw in text.splitlines():
        line = raw.split(";", 1)[0].strip()
        if not line:
            continue
        key, _, val = line.partition(" ")
        val = val.strip()
        if key == "type":
            cur = {"type": val}
            units.setdefault(val, cur)
        elif cur is not None:
            cur[key] = val
    return units


@lru_cache(maxsize=1)
def database() -> dict[str, dict]:
    db: dict[str, dict] = {}
    for rel in EDU_FILES:
        p = GAME_DATA / rel
        try:
            for name, u in _parse(p.read_text(encoding="latin-1")).items():
                db.setdefault(name, u)
        except OSError:
            continue
    if not db:
        log.warning("no export_descr_unit.txt found under %s", GAME_DATA)
    return db


def _nums(s: str) -> list[str]:
    return [x.strip() for x in s.split(",")]


def describe_type(name: str) -> str | None:
    u = database().get(name)
    if u is None:
        return None
    out = [f"{u.get('category', '?')} {u.get('class', '?')}"]
    pri, sec = _nums(u.get("stat_pri", "")), _nums(u.get("stat_sec", ""))

    def weapon(st, attr):
        if len(st) < 6 or st[0] in ("", "0"):
            return None
        w = f"attack {st[0]} charge {st[1]}"
        if st[2] != "no":
            w = f"missile {st[2]} attack {st[0]}, range {st[3]}m, ammo {st[4]}"
        tags = [WEAPON_ATTR[a] for a in _nums(attr) if a in WEAPON_ATTR]
        return w + (f" ({', '.join(tags)})" if tags else "")

    w1 = weapon(pri, u.get("stat_pri_attr", "no"))
    w2 = weapon(sec, u.get("stat_sec_attr", "no"))
    if len(sec) > 2 and sec[2] != "no" and w2:  # artillery: the missile is the secondary weapon
        out.append(w2)
        if w1:
            out.append("crew melee " + w1)
    else:
        if w1:
            out.append(w1)
        if w2:
            out.append(("melee " if pri and len(pri) > 2 and pri[2] != "no" else "sidearm ") + w2)
    arm = _nums(u.get("stat_pri_armour", ""))
    if len(arm) >= 3:
        out.append(f"armour {arm[0]} defence {arm[1]} shield {arm[2]}")
    men = _nums(u.get("stat_mental", ""))
    if len(men) >= 3:
        out.append(f"morale {men[0]} {men[1]} discipline, {men[2]}")
    form = _nums(u.get("formation", ""))
    # testudo is switched off for now (executor.TESTUDO_ENABLED): the general is not told about it
    extra = [f for f in form[5:] if f and f != "square" and f != "testudo"]
    if extra:
        out.append("formations: " + ", ".join(extra))
    attrs = [ATTRIBUTES[a] for a in _nums(u.get("attributes", "")) if a in ATTRIBUTES]
    out += attrs
    return "; ".join(out)


DEFAULT_FILE_SPACING = {"infantry": 1.2, "missile": 1.6, "cavalry": 2.4, "general": 2.4, "siege": 6.0, "other": 1.2}


def file_spacing(name: str, category: str) -> float:
    """Metres per man across the front (export_descr_unit 'formation': first value = side spacing, close order)."""
    u = database().get(name)
    try:
        return float(_nums(u.get("formation", ""))[0]) if u else DEFAULT_FILE_SPACING.get(category, 1.2)
    except (ValueError, IndexError):
        return DEFAULT_FILE_SPACING.get(category, 1.2)


def missile_range(name: str) -> float | None:
    """Range of the unit type's missile weapon in metres (primary, or secondary for artillery), None if it has none."""
    u = database().get(name)
    if not u:
        return None
    for key in ("stat_pri", "stat_sec"):
        st = _nums(u.get(key, ""))
        if len(st) > 3 and st[0] not in ("", "0") and st[2] != "no":
            try:
                return float(st[3])
            except ValueError:
                return None
    return None


def frontage_m(u) -> float | None:
    """Width of the unit's front in metres, from the game's current formation width."""
    if not getattr(u, "width_men", 0):
        return None
    return u.width_men * file_spacing(u.name, u.category)


def line_gaps(units, facing_deg: float | None = None) -> list[str]:
    """Gaps between neighbouring units of a group standing side by side: along the front, edge to edge, and units
    standing out of line (in front of / behind their neighbours)."""
    import math

    us = [u for u in units if frontage_m(u)]
    if len(us) < 2:
        return []
    if facing_deg is None:  # mean heading of the units
        sx = sum(math.sin(math.radians(u.heading)) for u in us)
        sy = sum(math.cos(math.radians(u.heading)) for u in us)
        facing_deg = math.degrees(math.atan2(sx, sy))
    fx, fy = math.sin(math.radians(facing_deg)), math.cos(math.radians(facing_deg))
    rx, ry = fy, -fx
    us.sort(key=lambda u: u.x * rx + u.y * ry)
    out = []
    for a, b in zip(us, us[1:]):
        along = (b.x - a.x) * rx + (b.y - a.y) * ry
        gap = along - (frontage_m(a) + frontage_m(b)) / 2
        depth = (b.x - a.x) * fx + (b.y - a.y) * fy
        if gap > 5:
            out.append(f"#{a.id}-#{b.id} gap {gap:.0f}m")
        elif gap < -5 and abs(depth) < 10:
            out.append(f"#{a.id}-#{b.id} overlap {-gap:.0f}m")
        if abs(depth) > 15:
            out.append(f"#{b.id} {abs(depth):.0f}m {'ahead of' if depth > 0 else 'behind'} #{a.id}")
    return out


def describe_battle_units(units) -> str:
    """One line per unit type present in the battle (both sides)."""
    seen: dict[str, str] = {}
    speeds: dict[str, tuple[float, float, float]] = {}
    siege: set[str] = set()
    for u in units:
        if u.name not in seen:
            seen[u.name] = describe_type(u.name) or "no data"
        if u.category == "siege":
            siege.add(u.name)
        if getattr(u, "run_speed", 0) > 0:
            speeds.setdefault(u.name, (u.walk_speed, u.run_speed, getattr(u, "charge_speed", 0.0)))
    if not seen:
        return ""
    for name, (walk, run, charge) in speeds.items():
        sp = f"walk {walk:.1f} m/s, RUN {run:.1f} m/s (100 m in {100 / run:.0f} s running)" + (
            f", CHARGE {charge:.1f} m/s" if charge > 0 else "")
        if name in siege:  # the game's speeds are the crew's; the machines are pushed very slowly
            seen[name] += f"; speed: very slow while it moves with its machines (about 0.5 m/s, pushed by the " \
                           f"crew); only the crew on foot, once the machines are gone or left behind: {sp}"
        else:
            seen[name] += f"; speed {sp}"
    return "UNIT TYPES IN THIS BATTLE (from the game's data; speeds are top speeds on open flat ground - " \
           "use them to judge how soon enemy units can reach a point and who can catch whom; CHARGE is the speed " \
           "of the last dash into the enemy):\n" + "\n".join(
        f"  {name}: {d}" for name, d in sorted(seen.items()))
