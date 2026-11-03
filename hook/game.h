/* Addresses and layouts in bin/Rome (Total War: ROME REMASTERED, Linux/Feral).
 * BuildID a0f41e24cf9e5fa4e7b9f6b03f0ce10d512993b6. Non-PIE, so these are absolute.
 * Found by reverse engineering the game binary for interoperability; see the comments next to each value. */
#pragma once
#include <stdint.h>

/* --- hook point --- */
#define ADDR_GAME_UPDATE_TICK        0x1c11d10UL /* BATTLE_GAME_HANDLER::game_update_tick(this) */
#define ADDR_GAME_UPDATE_TICK_SLOT   0x3f9cfd8UL /* vtable BATTLE_GAME_HANDLER (0x3f9ce80) slot 43 */

/* --- battle data --- */
#define ADDR_BATTLE_COUNTS_PTR       0x757cc40UL /* *(void**) -> +0x14180 u32 num alliances */
#define OFF_BATTLE_TYPE              0x4    /* in *ADDR_BATTLE_COUNTS_PTR: u32, 3 = siege (BATTLE_IS_SIEGE_BATTLE) */
#define OFF_ALLIANCE_IN_BATTLE       0x26a0 /* u32 in an alliance: non-zero = has armies in the battle (0x1436de0) */
#define BATTLE_TYPE_SIEGE            3
#define ADDR_BATTLE_PTR              0x757cc50UL /* *(void**) -> arg for alliance_get */
#define ADDR_ALLIANCE_GET            0x1358530UL /* BATTLE_ALLIANCE* (void* battle, uint32 idx) */

#define OFF_NUM_ALLIANCES            0x14180
#define OFF_ALLIANCE_NARMIES_A       0x26a0
#define OFF_ALLIANCE_NARMIES_B       0x27d8
#define OFF_ALLIANCE_ARMIES          0xa8   /* BATTLE_ARMY_DATA[64], stride 0x98, first qword = army* */
#define ALLIANCE_ARMY_STRIDE         0x98
#define OFF_ARMY_UNITS               0x90   /* UNIT** */
#define OFF_ARMY_NUNITS              0x9c   /* u32 */
#define MAX_ALLIANCES                8
#define MAX_ARMIES                   64

/* --- battle buildings (walls, gates, towers) ---
 * BUILDING_BATTLE_MANAGER = *(0x757cbf8) (passed to BATTLE_MAP_BUILDING_OWNERSHIP::open).
 * Settlement record: mgr + 0x40e8 + i*0x120 (i < 0xd4), the first with byte[0] != 0 and u32[+0x58] == 0 (0x15f9000).
 * Buildings: holder = *(rec+0x60); BUILDING_BATTLE** = *(holder+0x20); count = *(int*)(holder+0x2c)  (0x13d5730).
 * Position: x = float +0x4c, y = float +0x54 (BATTLE_MAP_BUILDING_OWNERSHIP::update).
 * NOTE: *(0x757ca48) is NOT buildings - it is the SIEGE_RAM list (0x13d81e0 looks rams up by id). */
#define ADDR_BUILDING_MANAGER_PTR    0x757cbf8UL
#define OFF_BMGR_SETTLEMENTS         0x40e8
#define BMGR_SETTLEMENT_STRIDE       0x120
#define BMGR_SETTLEMENT_COUNT        0xd4
#define OFF_SETT_REC_INACTIVE        0x58
#define OFF_SETT_REC_HOLDER          0x60
#define OFF_HOLDER_BUILDINGS         0x20
#define OFF_HOLDER_COUNT             0x2c
#define OFF_BUILDING_X               0x4c
#define OFF_BUILDING_Y               0x54
#define OFF_BUILDING_DAMAGE          0x68   /* u32: hit points LEFT (== max at battle start), not damage taken */
#define OFF_BUILDING_MAX_HP          0x70   /* u32 */
#define OFF_BUILDING_FLAG74          0x74   /* u8, vfunc slot 10 */
/* BUILDING_BATTLE_GATE (ctor 0x15f4b20) embeds a BUILDING_BATTLE_GATE_CONTROLLER (vtable 0x3edd5d0) at +0x490:
 * is_open() = u8 +0x10, open requests u16 +0x12, disabled u8 +0x14, can_be_controlled_by(a) = !disabled &&
 * *(int*)(owner +0xb0) == a with owner = ptr +0x18 (0x15f1230). Seen used by act_move.cpp (0x12ce440). */
#define OFF_GATE_OPEN                0x4a0  /* u8 */
#define OFF_GATE_DISABLED            0x4a4  /* u8 */
#define OFF_GATE_OWNER               0x4a8  /* ptr; its +0xb0 = alliance that controls the gate */
#define OFF_GATE_OWNER_ALLIANCE      0xb0
/* BATTLE_MAP_STREETS: the street graph the game's own pathfinder uses in towns (battle_map_streets.cpp).
 * Loaded from data/settlement_plans/street_plans/<plan>.cas; add_plan_instance 0x14020c0, load_plan 0x1401320,
 * load_link 0x1400450, node -> world transform 0x1403360. Pointer passed to PATHFINDER::open (0x1e363f0). */
#define ADDR_STREETS_PTR             0x757ca30UL
/* BATTLE_MAP_WALLS (battle_map_walls.cpp; open 0x140dc40, add 0x14122b0, calculate_neighbours 0x140fec0):
 * +0x18 BATTLE_MAP_WALL_PLATFORM*[400], count +0xc98. A wall platform (0x110 bytes, ctor 0x1411600) is a
 * PHYSICAL_PLATFORM (+0xc type, 1 = wall): +0x70 list of platform links (+0x10 next, +0x1c other platform id),
 * +0x80 active, +0xe0 group, +0xf0..+0xfc two end points (x0, y0, x1, y1). Being decoded from dumps. */
#define ADDR_WALLS_PTR               0x757ca20UL
#define WALLS_PLATFORMS              0x18
#define WALLS_COUNT                  0xc98
#define ADDR_PLATFORM_LINKS_PTR      0x757ca40UL
/* BATTLE_MAP_AREAS (battle_map_areas.cpp): the land areas of the map and the links between them.
 * add_link 0x13c1ba0 (type, p1, p2, width), allocate_link 0x13bc800 (max 100), get_area 0x13bbfb0.
 * A link (0x78 bytes): +4 type, +8 area1*, +0x10 p1 (x, y), +0x28 area2*, +0x30 p2 (x, y), +0x50 width.
 * The game's crossing AI (0x9cdea0) treats types 1 and 4 as river crossings; type 1 is built from bridge
 * pieces (0x13cc390), type 4 (fords) from other lists - both confirmed in battles. An area's first int is its handle. */
#define ADDR_AREAS_PTR               0x757ca60UL
#define AREAS_LINKS                  0x18
#define AREAS_NUM_LINKS              0x5c
#define AREA_LINK_SIZE               0x78
/* PHYSICAL_PLATFORM::unique_instance_retrieve 0x1e56460: every platform by id (walls, tower floors, ...) */
#define ADDR_PLATFORM_COUNT          0x64aba00UL /* int */
#define ADDR_PLATFORM_TABLE          0x64aba20UL /* PHYSICAL_PLATFORM*[count] */
#define STREETS_NUM_INSTANCES        0x1c8  /* int, <= 5 */
#define STREETS_INSTANCES            0x100  /* PLAN_INSTANCE[5], stride 0x28 */
#define STREETS_INSTANCE_STRIDE      0x28
#define INST_PLAN                    0x00   /* PLAN* */
#define INST_X                       0x08   /* float, world */
#define INST_Y                       0x0c
#define INST_ORIENT                  0x10   /* int: 0 (x,y)  1 (y,-x)  2 (-x,-y)  3 (-y,x), then + (INST_X, INST_Y) */
#define PLAN_LINKS                   0x10   /* LINK*, stride 0x20 */
#define PLAN_NUM_LINKS               0x18
#define PLAN_NODES                   0x20   /* NODE*, stride 0x68 */
#define PLAN_NUM_NODES               0x28
#define NODE_STRIDE                  0x68   /* +0 x, +4 y (plan coords), +0x10 LINK*[10], +0x60 int num links */
#define LINK_STRIDE                  0x20   /* +0 float width, +4 float length, +8 NODE* a, +0x10 NODE* b */
#define ADDR_SIEGE_RAM_LIST_PTR      0x757ca48UL /* *(p): SIEGE_RAM** list, *(u32*)(p+0x10) count, ram id at +0x90 */
/* UNIT::order_attack(BUILDING): the UI command executor 0x230f8a0 calls this */
#define ADDR_UNIT_ORDER_ATTACK_BUILDING 0x21ff150UL /* void (UNIT*, origin, bool queue, BUILDING_BATTLE*, bool run) */
/* UNIT::order_attack(SIEGE_ENGINE) - attack enemy rams/towers (their crew if manned): 0x230f7e0 calls this */
#define ADDR_UNIT_ORDER_ATTACK_ENGINE 0x22032e0UL /* void (UNIT*, origin, bool queue, SIEGE_ENGINE*, ATTACK_TYPE, bool run) */

/* Building kind by vtable (NEVER call building vfuncs: the manager's list also holds other classes whose
 * slot 15 is not the type getter - calling it crashed the game). Kinds as vfunc slot 15 would return. */
#define VTABLE_BUILDING_MAJOR        0x3edd890UL /* kind 1 */
#define VTABLE_BUILDING_WALL         0x3edfcc0UL /* kind 2 */
#define VTABLE_BUILDING_TOWER        0x3edf7e0UL /* kind 3 */
#define VTABLE_BUILDING_GATE         0x3edd608UL /* kind 4 */
#define VTABLE_BUILDING_WALL_FEATURE 0x3edc368UL /* kind 0 */

/* --- settlement battles: the plaza (town square) is a BATTLE_LOCATION ---
 * UNIT_TASK_FLEE::set_flee_details (FLEE_TO_PLAZA): loc = *( *( *(0x757ce00) + 0x28 ) + 0x98 )
 * BATTLE_LOCATION::random_position_within: centre VECTOR_2 at +0x2c; +0x28 u8 circle?; radius +0x34;
 * rectangle half extents +0x4c / +0x50 (along axes +0x3c and +0x44). */
#define ADDR_SETTLEMENT_BATTLE_PTR   0x757ce00UL
#define OFF_SBD_SETTLEMENT           0x28
#define OFF_SETTLEMENT_PLAZA         0x98
#define OFF_LOC_IS_CIRCLE            0x28
#define OFF_LOC_CENTRE               0x2c
#define OFF_LOC_RADIUS               0x34
#define OFF_LOC_HALF_A               0x4c
#define OFF_LOC_HALF_B               0x50

/* --- terrain: PHYSICAL_GRID (physical_grid.h; cell_get / save_height_map) --- */
#define ADDR_PHYSICAL_GRID_PTR       0x757cdf8UL /* *(PHYSICAL_GRID**) */
/* BATTLE_MAP: playable area is |x| <= half_x, |y| <= half_y (BATTLE_MAP::clamp_unit_position_to_playable_area
 * 0x13aff60, called with *(0x757cdf0) by 0x2253660). */
#define ADDR_BATTLE_MAP_PTR          0x757cdf0UL
#define OFF_MAP_HALF_X               0x14
#define OFF_MAP_HALF_Y               0x18
/* UNIT::order_withdraw(origin, queue) 0x22007c0 (the UI "withdraw" order, UNIT_UISHELL 0x230dd90): the unit leaves
 * the battle. Allowed only if 0x1358000(*(BATTLE**)0x757cde0, unit) - the army may withdraw. */
#define ADDR_UNIT_ORDER_WITHDRAW     0x22007c0UL
/* Order queue (UNIT battle data = *(UNIT+0x90)): entries of 0xe0 bytes at +0x148, head index u32 +0xf48 (mod 16),
 * count u32 +0xf50. UNIT::order_attack (0x2202a40) writes type 0xe at +0 and copies the target's handle - the
 * target UNIT's bytes +0x78..+0x8b - to entry +8; so entry+8 == target+0x78 (8 bytes) identifies the target. */
#define OFF_UNIT_BATTLE_DATA         0x90
#define OFF_BD_ORDERS                0x148
#define OFF_BD_ORDER_HEAD            0xf48
#define OFF_BD_ORDER_COUNT           0xf50
#define ORDER_STRIDE                 0xe0
#define ORDER_TYPE_ATTACK_UNIT       0xe
#define ORDER_TYPE_MOVE              0xb    /* UNIT::order_move 0x2204ba0 */
#define OFF_ORDER_MOVE_DEST          0x20   /* float x, y: the move description's destination */
#define OFF_ORDER_RUN                0x98   /* u8: run (move: already and-ed with "can run"; attack: run/charge) */
#define OFF_ORDER_TARGET_HANDLE      0x8
#define UNIT_HANDLE_LEN              0x14
/* UNIT vfuncs used by UNIT_GROUP (0x2241ae0) for the group's speed bounds: plain float getters, metres/second.
   UNIT: *(UNIT+0xb8)+0x4fc / +0x500 / +0x504; UNIT_CAVALRY: mount data *(*(*(UNIT+0xb8)+0x438)+0x10)+0xae0 / +0xae4 */
#define VF_UNIT_WALK_SPEED           0x458
#define VF_UNIT_RUN_SPEED            0x460
#define VF_UNIT_CHARGE_SPEED         0x468 /* +0x504 / mount +0xae8: speed of the skeleton's "charge" animation */
#define OFF_UNIT_HANDLE              0x78
#define ADDR_BATTLE_CAN_WITHDRAW     0x1358000UL
#define ADDR_BATTLE_OBJ_PTR          0x757cde0UL
#define GRID_NUM_CELLS               1000        /* 1000 x 1000 cells of 2 m: map spans -1000..1000 on both axes */
#define GRID_CELL_SIZE               2.0f
#define GRID_HALF_EXTENT             1000.0f
#define GRID_CELL_BYTES              0x20        /* cell(cx, cz) = grid + (cz * 1000 + cx) * 0x20, cx from x, cz from y */
#define OFF_CELL_OBJECT              0x0         /* u64: non-zero = physical object here (building, wall, tree) */
#define OFF_CELL_GROUND              0x8         /* u8 ground type (e.g. 145 = settlement paving); not fully decoded */
#define OFF_CELL_ZONE                0x9         /* u8: 0 inside the settlement */
#define OFF_CELL_HEIGHT              0xc         /* u32 quantized: height = q * 0.0015625 + base */
#define GRID_HEIGHT_SCALE            0.0015625f
#define OFF_GRID_BASE_HEIGHT         32000000    /* float, right after the cells */
#define OFF_GRID_MIN_HEIGHT          (OFF_GRID_BASE_HEIGHT + 4)
#define OFF_GRID_MAX_HEIGHT          (OFF_GRID_BASE_HEIGHT + 8)

/* --- UNIT --- */
#define OFF_UNIT_CONTROLLER          0x34   /* u32: 1 = AI, 2 = script/nobody, 0 = player (?) */
#define OFF_UNIT_FORMATION_OBJ       0x90   /* ptr; +0x70 u16 TAB_ANGLE heading (65536 = 360 deg) */
#define OFF_FORMATION_HEADING        0x70
/* FORMATION_DATA at UNIT+0xd60 (FORMATION::width_set 0x222bf30): u16 +0x24 men in formation, u16 +0x26 width in men */
#define OFF_UNIT_FORMATION_DATA      0xd60
#define OFF_FDATA_WIDTH_MEN          0x26
#define OFF_UNIT_STATE               0xcc   /* u32 enum UNIT_STATE, see unit_state_name() */
#define OFF_UNIT_FATIGUE_LEVEL       0x118  /* u32 FATIGUE 0..5 (fresh .. exhausted), shown by the unit info UI */
#define OFF_UNIT_MORALE_LEVEL        0x128  /* u32: 0 berserk 1 impetuous 2 high 3 firm 4 shaken 5 wavering 6 routing */
#define OFF_UNIT_SOLDIERS_MAX        0x328  /* u32 (PERCENTAGE_UNIT_KILLED) */
#define OFF_UNIT_SOLDIERS            0x32c  /* u32 alive */
#define OFF_UNIT_MODE_FLAGS          0xd50  /* u32: 0x2 fire at will, 0x4 skirmish */
#define OFF_UNIT_ARMY                0x98   /* army* (army+0x170 is NOT the general's unit - wrong in custom battles) */
#define OFF_UNIT_FORMATION_IMPL      0xd58  /* object*; vfunc[1](obj) = current FORMATION_TYPE (BATTLE_ENEMY_CURRENT_FORMATION) */
#define VT_FORMATION_TYPE_GET        1
#define OFF_UNIT_DESCRIPTION         0xb8   /* UNIT_DESCRIPTION* (unit_category_string_get) - verify in probe */
#define OFF_DESC_NAME                0x0    /* char* type name, e.g. "roman hastati" */
#define OFF_DESC_CATEGORY            0x50   /* u32: 0 infantry 1 cavalry 2 siege 3 non_combatant 4 ship 6 handler */
#define OFF_DESC_CLASS               0x54   /* u32: 0 heavy 1 light 2 skirmish 3 spearmen 4 missile */
#define ADDR_UNIT_POSITION_GET       0x21a8ef0UL /* void (VECTOR_2* out, const UNIT*) - used by unit_order_move_relative */
#define CONTROLLER_AI                1
#define CONTROLLER_SCRIPT            2

/* --- orders (same calls the battle script commands make) --- */
#define ADDR_ORDER_MOVE_DESC_CTOR    0x21fb730UL /* void (ORDER_MOVE_DESCRIPTION*) */
#define ADDR_ORDER_MOVE_DESC_SETDEST 0x21fb7a0UL /* void (desc*, const float xy[2], int mode) */
#define ADDR_ORDER_MOVE_DESC_WIDTH   0x21fb830UL /* void (desc*, u32 width_in_men)  - unit_order_move_to_orientation */
#define ADDR_ORDER_MOVE_DESC_FACING  0x21fb7f0UL /* void (desc*, u16 TAB_ANGLE): 65536 = 360 deg, 0 = north (+y), clockwise */
#define ADDR_UNIT_ORDER_MOVE         0x2204ba0UL /* void UNIT::order_move(origin, bool, desc&, bool run, bool) */
#define ADDR_UNIT_ORDER_ATTACK       0x2202a40UL /* void UNIT::order_attack(origin, bool, UNIT&, ATTACK_TYPE, bool run) */
#define ADDR_UNIT_ORDER_HALT         0x21fec80UL /* void (UNIT*, origin, bool) */
#define ADDR_UNIT_SET_MODE_FLAGS     0x21fffc0UL /* void (UNIT*, origin, bool, u32 flags) */
#define ADDR_UNIT_ORDER_FORMATION    0x2207fb0UL /* void (UNIT*, origin, bool, FORMATION_TYPE) - unit_order_change_formation */
#define ADDR_OPERATOR_DELETE_SIZED   0x39df1b0UL
#define VT_UNIT_ATTACK_SPECIAL       (0x500 / 8) /* vfunc: bool, if true use attack type from next */
#define VT_UNIT_ATTACK_TYPE_Q        (0x548 / 8) /* = ammo left (UNIT_PERCENTAGE_AMMO_LEFT); 0 -> melee attack */
#define VT_UNIT_SIEGE_ENGINE_TYPE    (0x290 / 8) /* SIEGE_ENGINE_TYPE of the carried engine (UNIT_INFANTRY::engine_unassign) */
#define VT_UNIT_SIEGE_ENGINE_NUM     (0x298 / 8) /* siege_engine_num_get(): rams/ladders/towers carried; 0 for most units */
/* UNIT::perform_order_unit_drop_engine (slot 18): UNIT_INFANTRY 0x226cb60 = vfunc 0x288 (detach engine, false);
 * the UNIT base 0x21dccb0 only reports "UNIT order not suported by this unit type". */
#define VT_UNIT_DROP_ENGINE          (0x90 / 8)
/* Special abilities (warcry, druid chant, cantabrian circle, ...; also testudo / phalanx toggles).
 * SCRIPT::UNIT_USE_SPECIAL_ABILITY::exec 0x1fc8cb0 calls ADDR_UNIT_ORDER_SPECIAL_ABILITY(unit, 1, 0). */
#define ADDR_UNIT_SPECIAL_ABILITY_TYPE   0x21fc1c0UL /* int (UNIT*): 0 none, 1 testudo, 2 phalanx, 3 wedge,
   4 drop_engines, 5 flaming_ammo, 6 warcry, 7 chant, 8 curse, 9 beserk, 10 rally, 11 kill_elephants, 12 move_and_shoot,
   13 cantabrian_circle, 14 shield_wall, 15 stealth, 16 feigned_rout, 17 schiltrom (0x1b3b580) */
#define ADDR_UNIT_SPECIAL_ABILITY_ACTIVE 0x21fc4a0UL /* bool (UNIT*) */
#define ADDR_UNIT_SPECIAL_ABILITY_CAN_USE 0x21fc950UL /* bool (UNIT*) */
#define ADDR_UNIT_ORDER_SPECIAL_ABILITY  0x2207fc0UL /* void (UNIT*, origin, bool queue); for chant (7) / curse (8)
                                                 * it only toggles the mode flag (UNIT +0xf7a bit 0) and halts the unit:
                                                 * the unit chants / curses by itself while it stands idle */
/* General's RALLY ability (UI command 0x25 in UNIT_UISHELL::decode_command 0x2311cb0 -> 0x230b8f0): toggled by
 * UNIT::perform_order_unit_rally_ability 0x2204690(unit) when the state 0x21fc7a0(unit) differs from the wanted one.
 * Both do nothing / return 0 for units that are not a general (UNIT+0x4d8 == 0). */
#define ADDR_UNIT_RALLY_TOGGLE           0x2204690UL
#define ADDR_UNIT_RALLY_ACTIVE           0x21fc7a0UL
#define OFF_UNIT_GENERAL                 0x4d8
#define ADDR_UNIT_DROP_ENGINE_BASE   0x21dccb0UL
#define VT_UNIT_HAS_MISSILES         VT_UNIT_ATTACK_SPECIAL
#define VT_UNIT_AMMO_LEFT            VT_UNIT_ATTACK_TYPE_Q
#define VT_UNIT_AMMO_MAX             (0x550 / 8)
#define ORDER_ORIGIN_SCRIPT          1
#define ORDER_MOVE_DESC_SIZE         0x80
#define OFF_DESC_VEC_BEGIN           0x50
#define OFF_DESC_VEC_CAP             0x60
