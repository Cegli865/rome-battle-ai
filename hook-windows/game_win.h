/* Addresses and layouts in the Windows build of Total War: ROME REMASTERED 2.0.5
 * ("Total War ROME REMASTERED.exe", sha256 09ef4354f5fa37489e8d7bc8d31cfef4042dc3d9f583188c76976ad391e3e70a).
 *
 * The exe is linked at 0x140000000 but uses ASLR, so addresses are kept as RVAs (offsets from the image base)
 * and turned into real addresses with g_img = the base the exe was loaded at.
 * Same source as the Linux build (hook/game.h), compiled with MSVC: functions are at other addresses, a few
 * structures are laid out differently (noted below), vtable slots are shifted.
 * Anything set to 0 is not found yet: the hook skips that feature. */
#pragma once
#include <stdint.h>

extern uintptr_t g_img; /* image base of the exe at run time */
#define RVA(x) ((x) ? g_img + (uintptr_t)(x) : (uintptr_t)0)

/* --- hook point: BATTLE_GAME_HANDLER::vftable 0x14202c400, slot 42 (Linux 43: MSVC has one destructor) --- */
#define RVA_GAME_UPDATE_TICK         0x09ab740
#define RVA_GAME_UPDATE_TICK_SLOT    0x202c550
#define ADDR_GAME_UPDATE_TICK        RVA(RVA_GAME_UPDATE_TICK)
#define ADDR_GAME_UPDATE_TICK_SLOT   RVA(RVA_GAME_UPDATE_TICK_SLOT)

/* --- battle data ---
 * BATTLE (*ADDR_BATTLE_PTR) has one member 8 bytes bigger near its start than on Linux: small getters read +0x94 /
 * +0x46 where Linux reads +0x8c / +0x42, and CHECKED_ARRAY<BATTLE_ALLIANCE,8> is at +0xf8 (Linux +0xf0).
 * Inside an alliance everything is as on Linux (armies at +0xa8, stride 0x98). */
#define ADDR_BATTLE_COUNTS_PTR       RVA(0x5128780) /* the BATTLE: +8 = battle type (3 = siege), +4 = stage */
#define OFF_BATTLE_TYPE              0x8    /* Linux 0x4: the battle-type checks (0x1404a85c0, 0x1405ad760) read +8; +4 is the stage */
#define OFF_ALLIANCE_IN_BATTLE       0x26a0
#define BATTLE_TYPE_SIEGE            3
#define ADDR_BATTLE_PTR              RVA(0x5128770)
/* BATTLE +4 u32 stage (Linux +0): BATTLE::deployment_start 0x1402ee7c0 checks it is 1 or 2,
 * BATTLE::deployment_advance 0x1402ede20 moves 1/2 -> 3 and 3 -> 4; 5 once the battle is started. */
#define BATTLE_STAGE                 0x4
#define ADDR_ALLIANCE_GET            0
#define OFF_BATTLE_ALLIANCES         0xf8   /* Linux 0xf0 */
#define BATTLE_ALLIANCE_STRIDE       0x2810
#define OFF_NUM_ALLIANCES            0x14188 /* Linux 0x14180 + 8 (deployment_start / deployment_advance read it) */
#define OFF_ALLIANCE_NARMIES_A       0x26a0
#define OFF_ALLIANCE_NARMIES_B       0x27d8
#define OFF_ALLIANCE_ARMIES          0xa8
#define ALLIANCE_ARMY_STRIDE         0x98
#define OFF_ARMY_UNITS               0x98   /* Linux 0x90; UNIT** (0x140153260 reads it with the count at +0xa4) */
#define OFF_ARMY_NUNITS              0xa4   /* Linux 0x9c; u32 (0x140153260: free slots = 20 - it) */
#define MAX_ALLIANCES                8
#define MAX_ARMIES                   64

/* --- town battles ---
 * Globals matched through the battle set-up (0x140422680 / Linux 0x15c8350), which opens the same subsystems in
 * the same order: areas 0x900, walls 0x940 (Linux 0x757ca20), platform links 0x920 (0x757ca40), streets 0x930
 * (0x757ca30), siege rams 0x918 (0x757ca48), BATTLE_MAP_BUILDING_OWNERSHIP::open 0x140465330 gets the building
 * manager 0x7c8 (0x757cbf8). Layouts checked against the Windows code: settlement records (0x1405037f0: +0x40e8,
 * stride 0x120, 0xd4 of them, inactive +0x58), buildings (0x140464f50: holder +0x60, list +0x20, count +0x2c;
 * ownership update 0x1404657f0: x +0x4c, y +0x54), gate (ctor 0x1404f9d60: controller at +0x490, +0x4a2/+0x4a4 as
 * on Linux), streets (add_plan_instance 0x14047e9f0: instances +0x100 stride 0x28, x/y/orientation +8/+0xc/+0x10,
 * count +0x1c8 <= 5), walls (add 0x140489320: count +0xc98 <= 400). */
#define ADDR_BUILDING_MANAGER_PTR    RVA(0x51287c8)
#define OFF_BMGR_SETTLEMENTS         0x40e8
#define BMGR_SETTLEMENT_STRIDE       0x120
#define BMGR_SETTLEMENT_COUNT        0xd4
#define OFF_SETT_REC_INACTIVE        0x58
#define OFF_SETT_REC_HOLDER          0x60
#define OFF_HOLDER_BUILDINGS         0x20
#define OFF_HOLDER_COUNT             0x2c
#define OFF_BUILDING_X               0x4c
#define OFF_BUILDING_Y               0x54
#define OFF_BUILDING_DAMAGE          0x68
#define OFF_BUILDING_MAX_HP          0x70
#define OFF_BUILDING_FLAG74          0x74
#define OFF_GATE_OPEN                0x4a0
#define OFF_GATE_DISABLED            0x4a4
#define OFF_GATE_OWNER               0x4a8
#define OFF_GATE_OWNER_ALLIANCE      0xb0
#define ADDR_STREETS_PTR             RVA(0x5128930)
#define ADDR_WALLS_PTR               RVA(0x5128940)
#define WALLS_PLATFORMS              0x18
#define WALLS_COUNT                  0xc98
#define ADDR_PLATFORM_LINKS_PTR      RVA(0x5128920)
/* BATTLE_MAP_AREAS: river crossings (bridges, fords). The global is read by BATTLE_MAP_AREAS::add_link
 * 0x14044f950 and AI_TACTIC_TASK_ASSAULT_CROSSING::assault_crossing_required 0x14027b080 (on Linux 0x757ca60 by
 * the same two); allocate_link 0x140451280. Link layout as on Linux: 0x78 bytes, +4 type, +8 area1*, +0x10 p1,
 * +0x28 area2*, +0x30 p2, +0x50 width; links at +0x18, count at +0x5c (max 100). */
#define ADDR_AREAS_PTR               RVA(0x5128900)
#define AREAS_LINKS                  0x18
#define AREAS_NUM_LINKS              0x5c
#define AREA_LINK_SIZE               0x78
/* PHYSICAL_PLATFORM::unique_instance_retrieve 0x140b5e2d0: count, then PHYSICAL_PLATFORM*[1000] (Linux +0x20) */
#define ADDR_PLATFORM_COUNT          RVA(0x4f9afe0)
#define ADDR_PLATFORM_TABLE          RVA(0x4f9aff0)
#define STREETS_NUM_INSTANCES        0x1c8
#define STREETS_INSTANCES            0x100
#define STREETS_INSTANCE_STRIDE      0x28
#define INST_PLAN                    0x00
#define INST_X                       0x08
#define INST_Y                       0x0c
#define INST_ORIENT                  0x10
#define PLAN_LINKS                   0x10
#define PLAN_NUM_LINKS               0x18
#define PLAN_NODES                   0x20
#define PLAN_NUM_NODES               0x28
#define NODE_STRIDE                  0x68
#define LINK_STRIDE                  0x20
#define ADDR_SIEGE_RAM_LIST_PTR      RVA(0x5128918)
/* the UNIT::order_* functions writing order type 0xf / 0x10 into the order queue, same arguments as on Linux */
#define ADDR_UNIT_ORDER_ATTACK_BUILDING RVA(0x1350ec0) /* (UNIT*, origin, bool, BUILDING_BATTLE*, bool run) */
#define ADDR_UNIT_ORDER_ATTACK_ENGINE   RVA(0x1350bf0) /* (UNIT*, origin, bool, SIEGE_ENGINE*, ATTACK_TYPE, bool run) */
/* building vtables from the MSVC RTTI (.?AVBUILDING_BATTLE_...@@ -> complete object locator -> vtable) */
#define VTABLE_BUILDING_MAJOR        RVA(0x1eb7128)
#define VTABLE_BUILDING_WALL         RVA(0x1eb9bd8)
#define VTABLE_BUILDING_TOWER        RVA(0x1eb9430)
#define VTABLE_BUILDING_GATE         RVA(0x1eb6b08)
#define VTABLE_BUILDING_WALL_FEATURE RVA(0x1eb4b70)
/* UNIT_TASK_FLEE::set_flee_details 0x1413e5160: plaza = *( *(*0x5128620 + 0x28) + 0x98 ), as Linux 0x757ce00 */
#define ADDR_SETTLEMENT_BATTLE_PTR   RVA(0x5128620)
#define OFF_SBD_SETTLEMENT           0x28
#define OFF_SETTLEMENT_PLAZA         0x98
#define OFF_LOC_IS_CIRCLE            0x28
#define OFF_LOC_CENTRE               0x2c
#define OFF_LOC_RADIUS               0x34
#define OFF_LOC_HALF_A               0x4c
#define OFF_LOC_HALF_B               0x50

/* --- terrain and map ---
 * PHYSICAL_GRID: BATTLE_MAP_AREAS::add_link passes it to 0x140450870(link, grid) where Linux calls
 * 0x13c1810(link, *0x757cdf8); cell layout checked in PHYSICAL_GRID::save_height_map 0x140b53770 (1000 x 1000 cells
 * of 0x20 bytes, height u32 at +0xc, base / min height at +32000000 / +32000004) - as on Linux.
 * BATTLE_MAP: 0x141399d40 calls BATTLE_MAP::clamp_unit_position_to_playable_area 0x140444460 with it, as Linux
 * 0x2253660 calls 0x13aff60 with *0x757cdf0; half extents at +0x14 / +0x18 as on Linux. */
#define ADDR_PHYSICAL_GRID_PTR       RVA(0x5128618)
#define ADDR_BATTLE_MAP_PTR          RVA(0x5128610)
#define OFF_MAP_HALF_X               0x14
#define OFF_MAP_HALF_Y               0x18
#define GRID_NUM_CELLS               1000
#define GRID_CELL_SIZE               2.0f
#define GRID_HALF_EXTENT             1000.0f
#define GRID_CELL_BYTES              0x20
#define OFF_CELL_OBJECT              0x0
#define OFF_CELL_GROUND              0x8
#define OFF_CELL_ZONE                0x9
#define OFF_CELL_HEIGHT              0xc
#define GRID_HEIGHT_SCALE            0.0015625f
#define OFF_GRID_BASE_HEIGHT         32000000
#define OFF_GRID_MIN_HEIGHT          (OFF_GRID_BASE_HEIGHT + 4)
#define OFF_GRID_MAX_HEIGHT          (OFF_GRID_BASE_HEIGHT + 8)

/* --- withdraw: the order; whether the army may withdraw is read directly (OFF_ARMY_DATA_CAN_WITHDRAW below) --- */
#define ADDR_UNIT_ORDER_WITHDRAW     RVA(0x1357fc0) /* own __FUNCSIG__, (UNIT*, origin, bool) */
#define ADDR_BATTLE_CAN_WITHDRAW     0
#define ADDR_BATTLE_OBJ_PTR          0
/* instead of the game's check (Linux 0x1358000 reads BATTLE + alliance * 0x2810 + army * 0x98 + 0x1ad, i.e. byte
 * +0x15 of the army's record in its alliance): read that byte directly */
#define OFF_ARMY_DATA_CAN_WITHDRAW   0x15

/* --- order queue: same layout as Linux (UNIT::order_move 0x141355080) --- */
#define OFF_UNIT_BATTLE_DATA         0x90
#define OFF_BD_ORDERS                0x148
#define OFF_BD_ORDER_HEAD            0xf48
#define OFF_BD_ORDER_COUNT           0xf50
#define ORDER_STRIDE                 0xe0
#define ORDER_TYPE_ATTACK_UNIT       0xe
#define ORDER_TYPE_MOVE              0xb
#define OFF_ORDER_MOVE_DEST          0x20
#define OFF_ORDER_RUN                0x98
#define OFF_ORDER_TARGET_HANDLE      0x8
#define UNIT_HANDLE_LEN              0x14

/* --- UNIT vfuncs (byte offsets in the vtable): Linux slot - 5 around here (checked function by function) --- */
#define VF_UNIT_WALK_SPEED           0x430  /* Linux 0x458 */
#define VF_UNIT_RUN_SPEED            0x438  /* Linux 0x460 */
#define VF_UNIT_CHARGE_SPEED         0x440  /* Linux 0x468 */
#define OFF_UNIT_HANDLE              0x78

/* --- UNIT fields: as on Linux up to +0xd60 (checked in a battle: positions, men, morale, speeds, ammo, abilities) except the controller (later ones shift: siege engine count +0x1064 vs
 *     +0x105c) --- */
#define OFF_UNIT_CONTROLLER          0x38   /* Linux 0x34; u32 1 = AI, 0 = player, 2 = script (seen in a battle: 1 on every AI unit) */
#define OFF_UNIT_FORMATION_OBJ       0x90
#define OFF_FORMATION_HEADING        0x70
#define OFF_UNIT_FORMATION_DATA      0xd60
#define OFF_FDATA_WIDTH_MEN          0x26
#define OFF_UNIT_STATE               0xcc
#define OFF_UNIT_FATIGUE_LEVEL       0x118
#define OFF_UNIT_MORALE_LEVEL        0x128
#define OFF_UNIT_SOLDIERS_MAX        0x328
#define OFF_UNIT_SOLDIERS            0x32c
#define OFF_UNIT_MODE_FLAGS          0xd50
#define OFF_UNIT_ARMY                0x98
#define OFF_UNIT_FORMATION_IMPL      0xd58
#define VT_FORMATION_TYPE_GET        1      /* as on Linux: slot 1 of every FORMATION_* class returns its type (checked in
                                             * the code: horde 0, column 1, square 2, wedge 3, testudo 5, phalanx 6,
                                             * schiltrom 7, shield wall 8, wall 9, moving through 10) */
/* Vtables of the FORMATION_* classes (MSVC RTTI): slot 1 is called only on an object with one of these, so a wrong
 * object at OFF_UNIT_FORMATION_IMPL can never make the hook run some other game function. */
#define FORMATION_VTABLES            {0x2214750, 0x2214b50, 0x1dfb730, 0x2214d50, 0x2215550, 0x2214f50, 0x2215150, \
                                      0x2214950, 0x2215350, 0x2237a18, 0x22257a0, 0x22259a0, 0x2225ba0}
#define OFF_UNIT_DESCRIPTION         0xb8
#define OFF_DESC_NAME                0x0
#define OFF_DESC_CATEGORY            0x50
#define OFF_DESC_CLASS               0x54
#define ADDR_UNIT_POSITION_GET       RVA(0x131acf0) /* MSVC: (const UNIT*, float out[2]) - arguments swapped */
#define CONTROLLER_AI                1
#define CONTROLLER_SCRIPT            2

/* --- orders --- */
#define ADDR_ORDER_MOVE_DESC_CTOR    0      /* reimplemented in C (desc_init) */
#define ADDR_ORDER_MOVE_DESC_SETDEST RVA(0x13583c0)
#define ADDR_ORDER_MOVE_DESC_WIDTH   0      /* reimplemented in C */
#define ADDR_ORDER_MOVE_DESC_FACING  RVA(0x13586f0)
#define ADDR_UNIT_ORDER_MOVE         RVA(0x1355080) /* (UNIT*, origin, bool, desc*, bool run, bool) */
#define ADDR_UNIT_ORDER_ATTACK       RVA(0x1350790) /* (UNIT*, origin, bool, UNIT*, attack type, bool run) */
#define ADDR_UNIT_ORDER_HALT         RVA(0x1354a10) /* (UNIT*, origin, bool) */
#define ADDR_UNIT_SET_MODE_FLAGS     RVA(0x1352e00) /* (UNIT*, origin, bool, u32 flags) */
#define ADDR_UNIT_ORDER_FORMATION    RVA(0x13516e0) /* (UNIT*, origin, bool, FORMATION_TYPE) */
#define ADDR_OPERATOR_DELETE_SIZED   RVA(0x0a05f40) /* operator delete -> free() of the exe's CRT */
#define VT_UNIT_ATTACK_SPECIAL       (0x4d8 / 8) /* Linux 0x500 */
#define VT_UNIT_ATTACK_TYPE_Q        (0x520 / 8) /* Linux 0x548: ammo left */
#define VT_UNIT_SIEGE_ENGINE_TYPE    (0x268 / 8) /* Linux 0x290 */
#define VT_UNIT_SIEGE_ENGINE_NUM     (0x270 / 8) /* Linux 0x298: returns UNIT +0x1064 */
#define VT_UNIT_DROP_ENGINE          (0x88 / 8)  /* Linux 0x90 */
#define ADDR_UNIT_SPECIAL_ABILITY_TYPE    RVA(0x1358dd0)
#define ADDR_UNIT_SPECIAL_ABILITY_ACTIVE  RVA(0x1358790)
#define ADDR_UNIT_SPECIAL_ABILITY_CAN_USE RVA(0x13589b0)
#define ADDR_UNIT_ORDER_SPECIAL_ABILITY   RVA(0x1357b20)
/* UNIT::perform_order_unit_rally_ability 0x1413579d0 (own __FUNCSIG__): switches the general's RALLY on (sets bit 0
 * of UNIT +0xf8c, Linux +0xf88); the state is read straight from that bit (OFF_UNIT_RALLY_FLAGS). */
#define ADDR_UNIT_RALLY_TOGGLE           RVA(0x13579d0)
#define ADDR_UNIT_RALLY_ACTIVE           0
#define OFF_UNIT_RALLY_FLAGS             0xf8c
#define OFF_UNIT_GENERAL                 0x4d8
#define ADDR_UNIT_DROP_ENGINE_BASE   RVA(0x1326c50)
#define VT_UNIT_HAS_MISSILES         VT_UNIT_ATTACK_SPECIAL
#define VT_UNIT_AMMO_LEFT            VT_UNIT_ATTACK_TYPE_Q
#define VT_UNIT_AMMO_MAX             (0x528 / 8) /* Linux 0x550 */
#define ORDER_ORIGIN_SCRIPT          1
#define ORDER_MOVE_DESC_SIZE         0x80
#define OFF_DESC_VEC_BEGIN           0x50
#define OFF_DESC_VEC_CAP             0x60
