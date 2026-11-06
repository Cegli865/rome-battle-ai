/* rome_hook.so: LD_PRELOAD bridge between Rome Remastered battles and the
 * external AI process (rome_ai.server).
 *
 * - Hooks BATTLE_GAME_HANDLER::game_update_tick by swapping one vtable slot.
 * - Every tick: executes queued orders (the same engine calls the battle
 *   script commands make) and periodically snapshots unit state.
 * - A background thread talks newline-delimited JSON over TCP to 127.0.0.1:7777.
 *
 * Nothing is patched unless the exe is bin/Rome and the expected bytes match.
 * Env:
 *   ROME_AI_PORT      server port (default 7777)
 *   ROME_AI_ALLIANCE  alliance handed to the external AI (default: auto = the one the game AI controls)
 *   ROME_AI_PROBE=1   dump raw unit memory to probe/ for offset discovery (no AI control)
 */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <math.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#include "game.h"
#include "minijson.h"

/* ------------------------------------------------------------------ */
/* logging                                                             */

static FILE *g_log;
static char g_dir[PATH_MAX]; /* directory of this .so */

static void net_log(const char *msg);

static void logf_(const char *fmt, ...) {
    char line[512];
    va_list ap0;
    va_start(ap0, fmt);
    vsnprintf(line, sizeof line, fmt, ap0);
    va_end(ap0);
    net_log(line);
    if (!g_log) return;
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    fprintf(g_log, "[%ld.%03ld] ", (long)ts.tv_sec, ts.tv_nsec / 1000000);
    va_list ap;
    va_start(ap, fmt);
    vfprintf(g_log, fmt, ap);
    va_end(ap);
    fputc('\n', g_log);
    fflush(g_log);
}

static double now_s(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec + ts.tv_nsec / 1e9;
}

/* ------------------------------------------------------------------ */
/* safe memory reads (never crash the game on a bad pointer)           */

static int g_memfd = -1;

static bool rd(uintptr_t addr, void *out, size_t n) {
    if (addr < 0x10000) return false;
    return pread(g_memfd, out, n, (off_t)addr) == (ssize_t)n;
}

static uintptr_t rd_ptr(uintptr_t addr) {
    uintptr_t v = 0;
    return rd(addr, &v, sizeof v) ? v : 0;
}

static uint32_t rd_u32(uintptr_t addr) {
    uint32_t v = 0;
    rd(addr, &v, sizeof v);
    return v;
}

/* ------------------------------------------------------------------ */
/* battle enumeration                                                  */

typedef struct {
    uintptr_t ptr;
    int alliance, army, index;
    int id; /* (alliance + 1) * 10000 + army * 100 + n, n = index when first seen: stable within a battle */
} unit_ref;

#define MAX_UNITS 512
static unit_ref g_units[MAX_UNITS];
static int g_nunits;

/* The game removes a destroyed unit from its army's array and reorders the rest, so the array index
 * is NOT stable. Ids are kept per unit pointer for the whole battle; new units get the next free n.
 * id = (alliance + 1) * 10000 + army * 100 + n: short, readable numbers, all five digits (the LLM mistyped
 * 65536-style ids, and with alliance 0 as bare #0..#10 next to its own #10000s it "completed" #8 to #10008). */
static struct { uintptr_t ptr; int id; uint32_t men; } g_idmap[MAX_UNITS]; /* men: last seen alive */
static int g_nids;
static int g_known; /* units of the last enumeration that already had an id (0 = a different battle) */
static unsigned char g_next_n[MAX_ALLIANCES][MAX_ARMIES];

static void idmap_reset(void) {
    g_nids = 0;
    memset(g_next_n, 0, sizeof g_next_n);
}

static int unit_id(uintptr_t ptr, int a, int r, int u) {
    for (int i = 0; i < g_nids; i++)
        if (g_idmap[i].ptr == ptr) {
            g_known++;
            return g_idmap[i].id;
        }
    int n = u > g_next_n[a][r] ? u : g_next_n[a][r];
    if (n > 99) n = 99; /* an army has at most ~20 units */
    g_next_n[a][r] = (unsigned char)(n + 1);
    int id = (a + 1) * 10000 + r * 100 + n;
    if (g_nids < MAX_UNITS) g_idmap[g_nids++] = (typeof(g_idmap[0])){ptr, id, 0};
    return id;
}

/* After a pause / menu: is this still the battle we knew? A restarted battle can reuse some of the old unit
   pointers, so "some unit is known" is not enough: most units must be known, and none may have more men than
   when last seen (men only ever go down within a battle). */
static bool same_battle(void) {
    if (g_nunits == 0 || g_known * 2 < g_nunits) return false;
    for (int i = 0; i < g_nunits; i++)
        for (int k = 0; k < g_nids; k++)
            if (g_idmap[k].ptr == g_units[i].ptr) {
                if (g_idmap[k].men && rd_u32(g_units[i].ptr + OFF_UNIT_SOLDIERS) > g_idmap[k].men) return false;
                break;
            }
    return true;
}

static void idmap_note_men(void) {
    for (int i = 0; i < g_nunits; i++)
        for (int k = 0; k < g_nids; k++)
            if (g_idmap[k].ptr == g_units[i].ptr) { g_idmap[k].men = rd_u32(g_units[i].ptr + OFF_UNIT_SOLDIERS); break; }
}

static uintptr_t alliance_ptr(uintptr_t battle, int idx) { return battle + 0xf0 + (uintptr_t)idx * 0x2810; }

static void enumerate_units(void) {
    g_nunits = 0;
    g_known = 0;
    uintptr_t battle = rd_ptr(ADDR_BATTLE_PTR);
    if (!battle) return;
    uint32_t nall = rd_u32(battle + OFF_NUM_ALLIANCES);
    if (nall > MAX_ALLIANCES) return;
    for (uint32_t a = 0; a < nall; a++) {
        uintptr_t al = alliance_ptr(battle, a);
        uint32_t narm = rd_u32(al + OFF_ALLIANCE_NARMIES_A) + rd_u32(al + OFF_ALLIANCE_NARMIES_B);
        if (narm > MAX_ARMIES) continue;
        for (uint32_t r = 0; r < narm; r++) {
            uintptr_t army = rd_ptr(al + OFF_ALLIANCE_ARMIES + r * ALLIANCE_ARMY_STRIDE);
            if (!army) continue;
            uintptr_t units = rd_ptr(army + OFF_ARMY_UNITS);
            uint32_t nu = rd_u32(army + OFF_ARMY_NUNITS);
            if (!units || nu > 64) continue;
            for (uint32_t u = 0; u < nu && g_nunits < MAX_UNITS; u++) {
                uintptr_t up = rd_ptr(units + u * 8);
                if (!up) continue;
                g_units[g_nunits++] = (unit_ref){up, (int)a, (int)r, (int)u, unit_id(up, (int)a, (int)r, (int)u)};
            }
        }
    }
}

static unit_ref *unit_by_id(int id) {
    for (int i = 0; i < g_nunits; i++)
        if (g_units[i].id == id) return &g_units[i];
    return NULL;
}

/* ------------------------------------------------------------------ */
/* orders                                                              */

typedef void (*desc_ctor_fn)(void *desc);
typedef void (*desc_setdest_fn)(void *desc, const float *xy, int mode);
typedef void (*order_move_fn)(void *unit, int origin, int b, void *desc, int run, int b2);
typedef void (*desc_width_fn)(void *desc, uint32_t width_in_men);
typedef void (*desc_facing_fn)(void *desc, uint16_t tab_angle);
typedef void (*order_attack_fn)(void *unit, int origin, int b, void *target, int attack_type, int run);
typedef void (*order_halt_fn)(void *unit, int origin, int b);
typedef void (*set_mode_flags_fn)(void *unit, int origin, int b, uint32_t flags);
typedef void (*op_delete_sized_fn)(void *p, size_t n);
typedef long (*vfn0)(void *self);

/* facing_deg < 0: keep the game's default (face the direction of travel) */
static void do_move(uintptr_t unit, float x, float y, bool run, float facing_deg, int width_men) {
    _Alignas(16) unsigned char desc[ORDER_MOVE_DESC_SIZE];
    float xy[2] = {x, y};
    ((desc_ctor_fn)ADDR_ORDER_MOVE_DESC_CTOR)(desc);
    ((desc_setdest_fn)ADDR_ORDER_MOVE_DESC_SETDEST)(desc, xy, 1);
    if (facing_deg >= 0) {
        if (width_men > 0) ((desc_width_fn)ADDR_ORDER_MOVE_DESC_WIDTH)(desc, (uint32_t)width_men);
        ((desc_facing_fn)ADDR_ORDER_MOVE_DESC_FACING)(desc, (uint16_t)lroundf(fmodf(facing_deg, 360.0f) * 65536.0f / 360.0f));
    }
    ((order_move_fn)ADDR_UNIT_ORDER_MOVE)((void *)unit, ORDER_ORIGIN_SCRIPT, 0, desc, run, 1);
    void *vec = *(void **)(desc + OFF_DESC_VEC_BEGIN);
    char *cap = *(char **)(desc + OFF_DESC_VEC_CAP);
    if (vec) ((op_delete_sized_fn)ADDR_OPERATOR_DELETE_SIZED)(vec, (size_t)(cap - (char *)vec));
}

/* ATTACK_TYPE: 0 = the unit's missiles, 1 = melee. As the game's own UI does: a missile unit shoots while it has
 * ammunition, unless melee is asked for (the player's alt-click attack). */
static void do_attack(uintptr_t unit, uintptr_t target, bool run, bool melee) {
    void **vt = *(void ***)unit;
    int type = 0;
    if (((vfn0)vt[VT_UNIT_ATTACK_SPECIAL])((void *)unit) & 0xff)
        type = melee || ((int)((vfn0)vt[VT_UNIT_ATTACK_TYPE_Q])((void *)unit) == 0);
    ((order_attack_fn)ADDR_UNIT_ORDER_ATTACK)((void *)unit, ORDER_ORIGIN_SCRIPT, 0, (void *)target, type, run);
}

typedef void (*unit_pos_fn)(float *out, void *unit);

static const char *unit_state_name(uint32_t s) {
    static const char *n[] = {"idling", "hiding", "taunting", "celebrating", "ready", "reforming", "moving", "withdrawing",
                              "missiles_firing", "missiles_reloading", "charging", "fighting", "pursuing", "routing",
                              "fighting_backs_to_the_walls", "running_amok", "go_berserk", "rallying", "dead",
                              "leaving_battle", "entering_battle", "left_battle"};
    return s < sizeof n / sizeof n[0] ? n[s] : "unknown";
}

/* protocol action: idle | moving | running | melee | shooting | routing | dead */
static const char *unit_action(uint32_t s) {
    switch (s) {
    case 6: case 7: return "moving";
    case 10: return "running";
    case 8: case 9: return "shooting";
    case 11: case 12: case 14: case 15: case 16: return "melee";
    case 13: return "routing";
    case 18: case 19: case 21: return "dead";
    default: return "idle";
    }
}

static void unit_type(uintptr_t unit, char *name, size_t cap, const char **category, const char **cls) {
    static const char *CLS[] = {"heavy", "light", "skirmish", "spearmen", "missile"};
    uintptr_t desc = rd_ptr(unit + OFF_UNIT_DESCRIPTION);
    uintptr_t np = desc ? rd_ptr(desc + OFF_DESC_NAME) : 0;
    char tmp[64] = {0};
    size_t n = 0;
    if (np && rd(np, tmp, sizeof tmp - 1))
        for (; tmp[n] && n + 1 < cap; n++) name[n] = (tmp[n] >= 0x20 && tmp[n] < 0x7f && tmp[n] != '"' && tmp[n] != '\\') ? tmp[n] : '_';
    name[n] = 0;
    uint32_t cat = desc ? rd_u32(desc + OFF_DESC_CATEGORY) : 99, cl = desc ? rd_u32(desc + OFF_DESC_CLASS) : 99;
    *cls = cl < 5 ? CLS[cl] : "other";
    if (cat == 0) *category = (cl == 2 || cl == 4) ? "missile" : "infantry";
    else if (cat == 1) *category = "cavalry";
    else if (cat == 2) *category = "siege";
    else *category = "other";
    /* army+0x170 turned out not to be the general's unit (it pointed at an onager in a custom battle).
     * The commanding character is attached at UNIT+0x4d8 - also when it leads an ordinary unit (e.g. barbarian
     * cavalry when the army has no general's bodyguard); a "... general's ..." type is the bodyguard. */
    if (strstr(name, "general") || rd_ptr(unit + OFF_UNIT_GENERAL)) *category = "general";
}

static int unit_engines(uintptr_t unit, int *type) {
    void **vt = *(void ***)unit;
    int n = (int)((vfn0)vt[VT_UNIT_SIEGE_ENGINE_NUM])((void *)unit);
    *type = n > 0 ? (int)((vfn0)vt[VT_UNIT_SIEGE_ENGINE_TYPE])((void *)unit) : -1;
    return n;
}

/* -1 for units without missiles */
static double unit_ammo(uintptr_t unit) {
    void **vt = *(void ***)unit;
    if (!(((vfn0)vt[VT_UNIT_HAS_MISSILES])((void *)unit) & 0xff)) return -1;
    uint32_t left = (uint32_t)((vfn0)vt[VT_UNIT_AMMO_LEFT])((void *)unit), max = (uint32_t)((vfn0)vt[VT_UNIT_AMMO_MAX])((void *)unit);
    return max ? (double)left / max : 0;
}

static const char *unit_formation(uintptr_t unit) {
    static const char *T[] = {"horde", "column", "square", "wedge", "square_hollow", "testudo", "phalanx", "schiltrom", "shield_wall"};
    uintptr_t f = rd_ptr(unit + OFF_UNIT_FORMATION_IMPL);
    uintptr_t vt = f ? rd_ptr(f) : 0;
    if (!vt || !rd_ptr(vt + 8 * VT_FORMATION_TYPE_GET)) return "unknown";
    uint32_t t = (uint32_t)((vfn0)(*(void ***)f)[VT_FORMATION_TYPE_GET])((void *)f);
    return t < sizeof T / sizeof T[0] ? T[t] : "unknown";
}

static const float MORALE_BY_LEVEL[7] = {1.0f, 1.0f, 0.9f, 0.7f, 0.45f, 0.25f, 0.0f};

static bool unit_on_field(uintptr_t unit) {
    uint32_t st = rd_u32(unit + OFF_UNIT_STATE);
    return st != 18 && st != 19 && st != 21 && rd_u32(unit + OFF_UNIT_SOLDIERS) > 0;
}

typedef void (*order_formation_fn)(void *unit, int origin, int b, int type);

static void do_formation(uintptr_t unit, const char *name) {
    static const char *T[] = {"horde", "column", "square", "wedge", "square_hollow", "testudo", "phalanx", "schiltrom", "shield_wall"};
    for (int i = 0; i < (int)(sizeof T / sizeof T[0]); i++)
        if (!strcmp(name, T[i])) {
            ((order_formation_fn)ADDR_UNIT_ORDER_FORMATION)((void *)unit, ORDER_ORIGIN_SCRIPT, 0, i);
            return;
        }
    logf_("unknown formation %s", name);
}

typedef void (*order_attack_building_fn)(void *unit, int origin, int queue, void *building, int run);

/* The town's buildings, as BATTLE_MAP_BUILDING_OWNERSHIP builds its list (pure reads). Returns the count. */
static int battle_buildings(uintptr_t *out, int max) {
    uintptr_t mgr = rd_ptr(ADDR_BUILDING_MANAGER_PTR);
    if (!mgr) return 0;
    for (int i = 0; i < BMGR_SETTLEMENT_COUNT; i++) {
        uintptr_t rec = mgr + OFF_BMGR_SETTLEMENTS + (uintptr_t)i * BMGR_SETTLEMENT_STRIDE;
        unsigned char used = 0;
        if (!rd(rec, &used, 1) || !used || rd_u32(rec + OFF_SETT_REC_INACTIVE)) continue;
        uintptr_t holder = rd_ptr(rec + OFF_SETT_REC_HOLDER), list = holder ? rd_ptr(holder + OFF_HOLDER_BUILDINGS) : 0;
        int n = holder ? (int)rd_u32(holder + OFF_HOLDER_COUNT) : 0;
        if (!list || n <= 0 || n > 4096) return 0;
        int k = 0;
        for (int b = 0; b < n && k < max; b++) out[k++] = rd_ptr(list + 8 * (uintptr_t)b);
        return k;
    }
    return 0;
}

static int building_kind(uintptr_t b) {
    uintptr_t vt = b ? rd_ptr(b) : 0;
    return vt == VTABLE_BUILDING_MAJOR ? 1 : vt == VTABLE_BUILDING_WALL ? 2 : vt == VTABLE_BUILDING_TOWER ? 3
         : vt == VTABLE_BUILDING_GATE ? 4 : vt == VTABLE_BUILDING_WALL_FEATURE ? 0 : -1;
}

/* Siege-engine carriers ordered at a building: watched every second. If the order does not get them moving within
 * RAM_WAIT_S, the unit is handed to the game's own AI (which knows how to use rams / ladders / towers) until its
 * engine is used or dropped; our orders to it are ignored meanwhile. Everything is logged to find the cause. */
#define MAX_RAMS 32
#define RAM_WAIT_S 8.0
#define CLIMB_HOLD_S 45.0 /* ladders / siege tower placed: the game's AI keeps the unit this long so it climbs up */
static struct { uintptr_t unit; int building, type; double t0, last_log, gone; float x, y; bool handed; } g_rams[MAX_RAMS];
static uint32_t g_battle_type; /* BATTLE_TYPE_*, read at battle start */

static int ram_slot(uintptr_t unit) {
    for (int i = 0; i < MAX_RAMS; i++) if (g_rams[i].unit == unit) return i;
    return -1;
}

static bool ram_handed(uintptr_t unit) {
    int i = ram_slot(unit);
    return i >= 0 && g_rams[i].handed;
}

static void ram_watch(uintptr_t unit, int building, int type) {
    int i = ram_slot(unit);
    if (i < 0) for (i = 0; i < MAX_RAMS && g_rams[i].unit; i++) {}
    if (i >= MAX_RAMS) return;
    float xy[2] = {0, 0};
    ((unit_pos_fn)ADDR_UNIT_POSITION_GET)(xy, (void *)unit);
    if (g_rams[i].unit != unit || g_rams[i].building != building)
        g_rams[i] = (typeof(g_rams[0])){unit, building, type, now_s(), 0, 0, xy[0], xy[1], false};
}

static void rams_update(void) {
    double t = now_s();
    for (int i = 0; i < MAX_RAMS; i++) {
        if (!g_rams[i].unit) continue;
        uintptr_t u = g_rams[i].unit;
        bool found = false;
        for (int k = 0; k < g_nunits; k++) if (g_units[k].ptr == u) found = true;
        if (!found) { g_rams[i].unit = 0; continue; }
        int type = -1, engines = unit_engines(u, &type);
        float xy[2] = {0, 0};
        ((unit_pos_fn)ADDR_UNIT_POSITION_GET)(xy, (void *)u);
        float moved = hypotf(xy[0] - g_rams[i].x, xy[1] - g_rams[i].y);
        uint32_t *c = (uint32_t *)(u + OFF_UNIT_CONTROLLER);
        if (t - g_rams[i].last_log >= 2.0) {
            g_rams[i].last_log = t;
            uintptr_t bd = rd_ptr(u + 0x90);
            uint32_t cnt = bd ? rd_u32(bd + 0xf50) : 0, head = bd ? rd_u32(bd + 0xf48) : 0;
            logf_("ram watch %p: %.0fs after order, moved %.0fm, state %u, engines %d, queue %u head-type %u, controller %u%s",
                  (void *)u, t - g_rams[i].t0, moved, rd_u32(u + OFF_UNIT_STATE), engines, cnt,
                  cnt ? rd_u32(bd + 0x148 + (uintptr_t)(head % 16) * 0xe0) : 0, *c, g_rams[i].handed ? " (game AI)" : "");
        }
        /* artillery (type 4, sieges only) comes back when its ammunition runs out or its machines are gone */
        if (engines <= 0 || (g_rams[i].type == 4 && unit_ammo(u) == 0)) {
            /* used or dropped: back to us - but a unit whose ladders / siege tower were just placed stays with the
             * game's AI a while longer, so it climbs onto the wall instead of standing at its foot */
            if (g_rams[i].type == 1 || g_rams[i].type == 2) {
                /* ladders / tower at the wall: the game's AI takes the unit up (our orders would only make it attack
                 * the wall from below, which infantry cannot do) - also when our own order brought it there */
                if (g_rams[i].gone == 0) {
                    g_rams[i].gone = t;
                    g_rams[i].handed = true;
                    *c = CONTROLLER_AI;
                    logf_("ram watch %p: ladders / siege tower placed after %.0fs - the game AI takes it up the wall",
                          (void *)u, t - g_rams[i].t0);
                }
                if (t - g_rams[i].gone < CLIMB_HOLD_S) continue;
            }
            if (g_rams[i].handed) *c = CONTROLLER_SCRIPT;
            logf_("ram watch %p: engine gone after %.0fs%s", (void *)u, t - g_rams[i].t0, g_rams[i].handed ? ", taken back" : "");
            g_rams[i].unit = 0;
        } else if (!g_rams[i].handed && t - g_rams[i].t0 > RAM_WAIT_S && moved < 5) {
            g_rams[i].handed = true;
            *c = CONTROLLER_AI;
            logf_("ram watch %p: did not move in %.0fs - handed to the game AI until its engine is used", (void *)u, RAM_WAIT_S);
        }
    }
}

static void do_attack_building(uintptr_t unit, int index, bool run) {
    static uintptr_t list[4096];
    int n = battle_buildings(list, 4096);
    if (index < 0 || index >= n || building_kind(list[index]) < 0) { logf_("attack_building: no building %d", index); return; }
    uintptr_t bd = rd_ptr(unit + 0x90); /* UNIT battle data: order queue count at +0xf50 (UNIT::order_* 0x21ff150) */
    uint32_t before = bd ? rd_u32(bd + 0xf50) : 0;
    ((order_attack_building_fn)ADDR_UNIT_ORDER_ATTACK_BUILDING)((void *)unit, ORDER_ORIGIN_SCRIPT, 0, (void *)list[index], run);
    int type = -1, engines = unit_engines(unit, &type);
    if (engines > 0 && type >= 0 && type <= 2) ram_watch(unit, index, type); /* ram, ladders, siege tower - not artillery */
    logf_("attack_building unit %p -> building %d (kind %d, hp %u/%u): order queue %u -> %u, unit state %u, engines %d type %d",
          (void *)unit, index, building_kind(list[index]), rd_u32(list[index] + OFF_BUILDING_DAMAGE),
          rd_u32(list[index] + OFF_BUILDING_MAX_HP), before, bd ? rd_u32(bd + 0xf50) : 0,
          rd_u32(unit + OFF_UNIT_STATE), engines, type);
}

typedef long (*unit_getter_fn)(void *unit);
typedef void (*order_ability_fn)(void *unit, int origin, int queue);

/* special ability: type (0 = none), can it be used now, is it on. 4 = drop_engines is left out (own command);
   5 = flaming_ammo (names -> type in 0x1b3b580). */
static int unit_ability(uintptr_t unit, bool *ready, bool *active) {
    int type = (int)((unit_getter_fn)ADDR_UNIT_SPECIAL_ABILITY_TYPE)((void *)unit);
    if (type <= 0 || type == 4 || type > 0x11) { *ready = *active = false; return 0; }
    *ready = ((unit_getter_fn)ADDR_UNIT_SPECIAL_ABILITY_CAN_USE)((void *)unit) & 0xff;
    *active = ((unit_getter_fn)ADDR_UNIT_SPECIAL_ABILITY_ACTIVE)((void *)unit) & 0xff;
    return type;
}

static void do_special_ability(uintptr_t unit) {
    bool ready, active;
    if (!unit_ability(unit, &ready, &active) || (!ready && !active)) return; /* active: toggle it off */
    ((order_ability_fn)ADDR_UNIT_ORDER_SPECIAL_ABILITY)((void *)unit, ORDER_ORIGIN_SCRIPT, 0);
}

typedef float (*unit_speed_fn)(void *unit);

static float unit_speed(uintptr_t unit, size_t slot) {
    uintptr_t vt = rd_ptr(unit);
    uintptr_t fn = vt ? rd_ptr(vt + slot) : 0;
    if (!fn) return 0;
    float v = ((unit_speed_fn)fn)((void *)unit);
    return (v > 0 && v < 100) ? v : 0;
}

/* -1: not a general; else whether its rally ability is on */
static int unit_rally(uintptr_t unit) {
    if (!rd_ptr(unit + OFF_UNIT_GENERAL)) return -1;
    return (int)(((unit_getter_fn)ADDR_UNIT_RALLY_ACTIVE)((void *)unit) & 1);
}

static void do_rally(uintptr_t unit, bool on) {
    int cur = unit_rally(unit);
    if (cur < 0 || cur == (int)on) return;
    ((unit_getter_fn)ADDR_UNIT_RALLY_TOGGLE)((void *)unit);
    logf_("general %p rally %s", (void *)unit, on ? "on" : "off");
}

typedef char (*can_withdraw_fn)(void *battle, void *unit);
typedef void (*order_withdraw_fn)(void *unit, int origin, int queue);

static void do_withdraw(uintptr_t unit) {
    uintptr_t battle = rd_ptr(ADDR_BATTLE_OBJ_PTR);
    if (!battle || !((can_withdraw_fn)ADDR_BATTLE_CAN_WITHDRAW)((void *)battle, (void *)unit)) {
        logf_("withdraw: unit %p may not withdraw from this battle", (void *)unit);
        return;
    }
    ((order_withdraw_fn)ADDR_UNIT_ORDER_WITHDRAW)((void *)unit, ORDER_ORIGIN_SCRIPT, 0);
    logf_("withdraw: unit %p ordered off the battlefield", (void *)unit);
}

static void do_drop_engine(uintptr_t unit) {
    int type;
    void **vt = *(void ***)unit;
    if (unit_engines(unit, &type) <= 0 || (uintptr_t)vt[VT_UNIT_DROP_ENGINE] == ADDR_UNIT_DROP_ENGINE_BASE) return;
    ((vfn0)vt[VT_UNIT_DROP_ENGINE])((void *)unit);
    logf_("unit %p dropped its siege engine (type %d)", (void *)unit, type);
}

static void do_halt(uintptr_t unit) { ((order_halt_fn)ADDR_UNIT_ORDER_HALT)((void *)unit, ORDER_ORIGIN_SCRIPT, 0); }

static void do_mode(uintptr_t unit, uint32_t bit, bool on) {
    uint32_t flags = *(uint32_t *)(unit + OFF_UNIT_MODE_FLAGS);
    flags = on ? (flags | bit) : (flags & ~bit);
    ((set_mode_flags_fn)ADDR_UNIT_SET_MODE_FLAGS)((void *)unit, ORDER_ORIGIN_SCRIPT, 0, flags);
}

/* ------------------------------------------------------------------ */
/* message queues between game thread and network thread               */

typedef struct msg { struct msg *next; char *line; } msg;
typedef struct { msg *head, *tail; pthread_mutex_t mu; } queue;

static queue g_in = {.mu = PTHREAD_MUTEX_INITIALIZER}, g_out = {.mu = PTHREAD_MUTEX_INITIALIZER};

static void q_push(queue *q, char *line) {
    msg *m = malloc(sizeof *m);
    m->next = NULL;
    m->line = line;
    pthread_mutex_lock(&q->mu);
    if (q->tail) q->tail->next = m; else q->head = m;
    q->tail = m;
    pthread_mutex_unlock(&q->mu);
}

static char *q_pop(queue *q) {
    pthread_mutex_lock(&q->mu);
    msg *m = q->head;
    if (m) {
        q->head = m->next;
        if (!q->head) q->tail = NULL;
    }
    pthread_mutex_unlock(&q->mu);
    if (!m) return NULL;
    char *l = m->line;
    free(m);
    return l;
}

static volatile bool g_connected;

static void net_log(const char *msg) {
    if (!g_connected) return;
    size_t n = strlen(msg);
    char *out = malloc(n * 2 + 32), *o = out;
    o += sprintf(o, "{\"type\":\"log\",\"msg\":\"");
    for (const char *p = msg; *p; p++) {
        if (*p == '"' || *p == '\\') *o++ = '\\';
        *o++ = (*p >= 0x20 && *p < 0x7f) ? *p : ' ';
    }
    strcpy(o, "\"}\n");
    q_push(&g_out, out);
}

static void send_line(const char *fmt, ...) {
    if (!g_connected) return;
    char *s = NULL;
    va_list ap;
    va_start(ap, fmt);
    if (vasprintf(&s, fmt, ap) < 0) s = NULL;
    va_end(ap);
    if (s) q_push(&g_out, s);
}

/* ------------------------------------------------------------------ */
/* per-tick logic                                                      */

static int g_ai_alliance = -1;        /* from env, or auto-detected */
static bool g_probe;
static bool g_in_battle;
static double g_last_tick, g_battle_t0, g_last_state;
static struct { uintptr_t ptr; uint32_t controller; } g_saved[MAX_UNITS]; /* original controller per unit */
static int g_nsaved;
static double g_last_probe;
static volatile bool g_release_pending; /* server gone: hand units back to the game AI on the next tick */
static volatile bool g_terrain_pending; /* set on battle start / (re)connect, cleared when terrain is sent */
static volatile bool g_ticks_stopped; /* set by the network thread; units may be gone, don't touch them */

static void probe_dump_grid(void);

static int detect_ai_alliance(void) {
    int votes[MAX_ALLIANCES] = {0};
    for (int i = 0; i < g_nunits; i++)
        if (rd_u32(g_units[i].ptr + OFF_UNIT_CONTROLLER) == CONTROLLER_AI) votes[g_units[i].alliance]++;
    int best = -1;
    for (int a = 0; a < MAX_ALLIANCES; a++)
        if (votes[a] && (best < 0 || votes[a] > votes[best])) best = a;
    return best;
}

/* Siege equipment (ram, ladders, siege tower) is the game's AI's job: a unit carrying it stays with the game's AI
   from the start until the equipment is used, dropped or destroyed (rams_update gives it back then). In siege
   battles the artillery (type 4) too, until its ammunition runs out. */
static void hand_siege_unit(uintptr_t unit) {
    int type = -1;
    if (unit_engines(unit, &type) <= 0 || type < 0) return;
    if (type > 2 && !(type == 4 && g_battle_type == BATTLE_TYPE_SIEGE && unit_ammo(unit) != 0)) return;
    int i = ram_slot(unit);
    if (i < 0) for (i = 0; i < MAX_RAMS && g_rams[i].unit; i++) {}
    if (i >= MAX_RAMS) return;
    if (g_rams[i].unit != unit) {
        float xy[2] = {0, 0};
        ((unit_pos_fn)ADDR_UNIT_POSITION_GET)(xy, (void *)unit);
        g_rams[i] = (typeof(g_rams[0])){unit, -1, type, now_s(), 0, 0, xy[0], xy[1], true};
        logf_("siege unit %p (engine type %d) left to the game's AI until its equipment is used", (void *)unit, type);
    }
    g_rams[i].handed = true;
    *(uint32_t *)(unit + OFF_UNIT_CONTROLLER) = CONTROLLER_AI;
}

static bool g_taken; /* the alliance is ours (take_control): units the game's AI takes back are retaken */
static uintptr_t g_handed[MAX_UNITS]; /* units handed to the game's AI one by one ("control" with "unit") */
static int g_nhanded;

static void take_control(bool take) {
    g_taken = take;
    for (int i = 0; i < g_nunits; i++) {
        unit_ref *u = &g_units[i];
        if (u->alliance != g_ai_alliance) continue;
        if (take) hand_siege_unit(u->ptr);
        if (take && ram_handed(u->ptr)) continue; /* stays with the game's AI until its siege engine is used */
        uint32_t *c = (uint32_t *)(u->ptr + OFF_UNIT_CONTROLLER);
        int k = 0;
        while (k < g_nsaved && g_saved[k].ptr != u->ptr) k++;
        if (take) {
            if (k == g_nsaved && g_nsaved < MAX_UNITS && *c != CONTROLLER_SCRIPT) /* first take keeps the original */
                g_saved[g_nsaved++] = (typeof(g_saved[0])){u->ptr, *c};
            *c = CONTROLLER_SCRIPT;
        } else if (*c == CONTROLLER_SCRIPT) {
            *c = k < g_nsaved ? g_saved[k].controller : CONTROLLER_AI;
        }
    }
    logf_("%s control of alliance %d", take ? "took" : "released", g_ai_alliance);
}

static void battle_begin(void) {
    g_in_battle = true;
    g_nsaved = 0;
    g_nhanded = 0;
    g_taken = false;
    g_battle_t0 = now_s();
    const char *env = getenv("ROME_AI_ALLIANCE");
    g_ai_alliance = env ? atoi(env) : detect_ai_alliance();
    uintptr_t counts0 = rd_ptr(ADDR_BATTLE_COUNTS_PTR);
    g_battle_type = counts0 ? rd_u32(counts0 + OFF_BATTLE_TYPE) : 0; /* before take_control: sieges hand artillery over */
    logf_("battle begin: %d units, external AI gets alliance %d", g_nunits, g_ai_alliance);
    for (int i = 0; i < g_nunits; i++)
        logf_("  unit id=%d ptr=%p al=%d army=%d idx=%d controller=%u flags=%#x", g_units[i].id, (void *)g_units[i].ptr,
              g_units[i].alliance, g_units[i].army, g_units[i].index, rd_u32(g_units[i].ptr + OFF_UNIT_CONTROLLER),
              rd_u32(g_units[i].ptr + OFF_UNIT_MODE_FLAGS));
    if (!g_probe && g_ai_alliance >= 0 && g_connected) take_control(true);
    if (g_probe) probe_dump_grid();
    /* Who attacks: byte 0 of each alliance is "defends" (custom battles 2026-10-29: AI attacking -> 0, AI defending
     * -> 1). The game itself (battle_state_inspection, 0x1436de0) takes as the attacking army the last alliance
     * with that byte 0 and armies in it (+0x26a0) - do the same. */
    uintptr_t counts_obj = rd_ptr(ADDR_BATTLE_COUNTS_PTR);
    int attacker = -1;
    uint32_t nall = counts_obj ? rd_u32(counts_obj + OFF_NUM_ALLIANCES) : 0;
    char flags_s[64] = "";
    for (uint32_t a = 0; a < nall && a < MAX_ALLIANCES; a++) {
        unsigned char defends = 1;
        uint32_t armies = 0;
        rd(alliance_ptr(counts_obj, a), &defends, 1);
        rd(alliance_ptr(counts_obj, a) + OFF_ALLIANCE_IN_BATTLE, &armies, 4);
        size_t l = strlen(flags_s);
        snprintf(flags_s + l, sizeof flags_s - l, "%s%u:%u/%u", a ? " " : "", a, defends, armies);
        if (!defends && armies) attacker = (int)a;
    }
    unsigned char ai_attacks = attacker >= 0 && attacker == g_ai_alliance;
    logf_("alliance defends/armies: %s -> attacker alliance %d, AI attacks: %d", flags_s, attacker, ai_attacks);
    uintptr_t counts = rd_ptr(ADDR_BATTLE_COUNTS_PTR);
    uint32_t btype = counts ? rd_u32(counts + OFF_BATTLE_TYPE) : 0;
    logf_("battle type %u", btype);
    char plaza[160] = "null";
    uintptr_t sbd = rd_ptr(ADDR_SETTLEMENT_BATTLE_PTR), sett = sbd ? rd_ptr(sbd + OFF_SBD_SETTLEMENT) : 0;
    uintptr_t loc = sett ? rd_ptr(sett + OFF_SETTLEMENT_PLAZA) : 0;
    if (loc) {
        float c[2] = {0, 0}, r = 0, ha = 0, hb = 0;
        unsigned char circle = 0;
        rd(loc + OFF_LOC_CENTRE, c, sizeof c);
        rd(loc + OFF_LOC_IS_CIRCLE, &circle, 1);
        rd(loc + OFF_LOC_RADIUS, &r, 4);
        rd(loc + OFF_LOC_HALF_A, &ha, 4);
        rd(loc + OFF_LOC_HALF_B, &hb, 4);
        float size = circle ? r : (ha > hb ? ha : hb);
        if (isfinite(c[0]) && isfinite(c[1]) && fabsf(c[0]) < 1100 && fabsf(c[1]) < 1100 && isfinite(size) && size >= 0 && size < 500)
            snprintf(plaza, sizeof plaza, "[%.1f,%.1f,%.1f]", c[0], c[1], size);
        logf_("plaza: centre (%.1f, %.1f) %s %.1f / %.1f x %.1f", c[0], c[1], circle ? "radius" : "rect", r, ha, hb);
    }
    /* may the AI's army withdraw from this battle at all (the game's own check behind the withdraw order) */
    int can_withdraw = -1;
    uintptr_t battle_obj = rd_ptr(ADDR_BATTLE_OBJ_PTR);
    for (int i = 0; i < g_nunits && battle_obj; i++)
        if (g_units[i].alliance == g_ai_alliance) {
            can_withdraw = ((can_withdraw_fn)ADDR_BATTLE_CAN_WITHDRAW)((void *)battle_obj, (void *)g_units[i].ptr) ? 1 : 0;
            break;
        }
    float half[2] = {0, 0};
    uintptr_t bmap = rd_ptr(ADDR_BATTLE_MAP_PTR);
    if (bmap) { rd(bmap + OFF_MAP_HALF_X, &half[0], 4); rd(bmap + OFF_MAP_HALF_Y, &half[1], 4); }
    send_line("{\"type\":\"battle_start\",\"ai_alliance\":%d,\"player_is_attacker\":%s,\"battle_type\":%u,"
              "\"can_withdraw\":%s,\"map\":{\"settlement\":%s,\"plaza\":%s,\"playable\":[%.1f,%.1f]}}\n",
              g_ai_alliance, ai_attacks ? "false" : "true", btype, can_withdraw < 0 ? "null" : can_withdraw ? "true" : "false",
              btype == BATTLE_TYPE_SIEGE ? "true" : "false", plaza, half[0], half[1]);
    g_terrain_pending = true; /* sent from the next tick, after battle_start is queued */
}

/* Coarse terrain sample, sent once per battle: heights (decimetres) + raw cell bytes for offline decoding. */
#define TERRAIN_N 80
static void send_terrain_from(uintptr_t grid);
static void send_masks_from(uintptr_t grid);
static void send_terrain(void) {
    uintptr_t grid = rd_ptr(ADDR_PHYSICAL_GRID_PTR);
    if (!grid) { logf_("no physical grid"); return; }
    send_terrain_from(grid);
}

static void send_terrain_from(uintptr_t grid) {
    float base = 0, hmin = 0, hmax = 0;
    rd(grid + OFF_GRID_BASE_HEIGHT, &base, 4);
    rd(grid + OFF_GRID_MIN_HEIGHT, &hmin, 4);
    rd(grid + OFF_GRID_MAX_HEIGHT, &hmax, 4);
    const float step = 2 * GRID_HALF_EXTENT / TERRAIN_N;
    size_t cap = 512 + TERRAIN_N * TERRAIN_N * (8 + 2 * GRID_CELL_BYTES), len = 0;
    char *buf = malloc(cap);
    len += snprintf(buf + len, cap - len,
                    "{\"type\":\"terrain\",\"n\":%d,\"step\":%.1f,\"origin\":%.1f,\"base\":%.2f,\"min\":%.2f,\"max\":%.2f,\"heights_dm\":[",
                    TERRAIN_N, step, -GRID_HALF_EXTENT, base, hmin, hmax);
    static unsigned char cells[TERRAIN_N * TERRAIN_N][GRID_CELL_BYTES];
    for (int j = 0; j < TERRAIN_N; j++) /* row j = y */
        for (int i = 0; i < TERRAIN_N; i++) {
            int cx = (int)(((i + 0.5f) * step) / GRID_CELL_SIZE), cz = (int)(((j + 0.5f) * step) / GRID_CELL_SIZE);
            unsigned char *c = cells[j * TERRAIN_N + i];
            memset(c, 0, GRID_CELL_BYTES);
            rd(grid + ((uintptr_t)cz * GRID_NUM_CELLS + (uintptr_t)cx) * GRID_CELL_BYTES, c, GRID_CELL_BYTES);
            uint32_t q;
            memcpy(&q, c + OFF_CELL_HEIGHT, 4);
            len += snprintf(buf + len, cap - len, "%s%d", (i || j) ? "," : "", (int)lroundf((q * GRID_HEIGHT_SCALE + base) * 10));
        }
    len += snprintf(buf + len, cap - len, "],\"cells_hex\":\"");
    static const char HX[] = "0123456789abcdef";
    for (int k = 0; k < TERRAIN_N * TERRAIN_N; k++)
        for (int b = 0; b < GRID_CELL_BYTES; b++) {
            buf[len++] = HX[cells[k][b] >> 4];
            buf[len++] = HX[cells[k][b] & 15];
        }
    snprintf(buf + len, cap - len, "\"}\n");
    logf_("terrain sent: base %.1f m, range %.1f..%.1f m", base, hmin, hmax);
    if (g_connected) q_push(&g_out, buf); else free(buf);
    send_masks_from(grid);
}

/* Full-resolution (2 m) masks, 1 bit per cell, base64: obstacles and settlement area. Sent once per battle. */
static char *b64(const unsigned char *d, size_t n, size_t *outlen) {
    static const char T[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    char *o = malloc((n + 2) / 3 * 4 + 1), *p = o;
    for (size_t i = 0; i < n; i += 3) {
        uint32_t v = (uint32_t)d[i] << 16 | (i + 1 < n ? (uint32_t)d[i + 1] << 8 : 0) | (i + 2 < n ? d[i + 2] : 0);
        *p++ = T[v >> 18 & 63];
        *p++ = T[v >> 12 & 63];
        *p++ = i + 1 < n ? T[v >> 6 & 63] : '=';
        *p++ = i + 2 < n ? T[v & 63] : '=';
    }
    *p = 0;
    *outlen = (size_t)(p - o);
    return o;
}

static void send_masks_from(uintptr_t grid) {
    const size_t nbits = (size_t)GRID_NUM_CELLS * GRID_NUM_CELLS, nbytes = nbits / 8;
    unsigned char *obst = calloc(1, nbytes), *zone = calloc(1, nbytes);
    static unsigned char row[GRID_NUM_CELLS * GRID_CELL_BYTES];
    for (int cz = 0; cz < GRID_NUM_CELLS; cz++) {
        memset(row, 0, sizeof row);
        rd(grid + (uintptr_t)cz * sizeof row, row, sizeof row);
        for (int cx = 0; cx < GRID_NUM_CELLS; cx++) {
            const unsigned char *c = row + (size_t)cx * GRID_CELL_BYTES;
            uint64_t obj;
            memcpy(&obj, c + OFF_CELL_OBJECT, 8);
            size_t k = (size_t)cz * GRID_NUM_CELLS + cx;
            if (obj) obst[k >> 3] |= (unsigned char)(1 << (k & 7));
            if (c[OFF_CELL_ZONE] == 0) zone[k >> 3] |= (unsigned char)(1 << (k & 7));
        }
    }
    size_t l1, l2;
    char *a = b64(obst, nbytes, &l1), *z = b64(zone, nbytes, &l2);
    free(obst);
    free(zone);
    char *buf = malloc(l1 + l2 + 200);
    sprintf(buf, "{\"type\":\"terrain_masks\",\"cells\":%d,\"cell_size\":%.1f,\"origin\":%.1f,\"obstacles_b64\":\"%s\",\"settlement_b64\":\"%s\"}\n",
            GRID_NUM_CELLS, GRID_CELL_SIZE, -GRID_HALF_EXTENT, a, z);
    free(a);
    free(z);
    if (g_connected) q_push(&g_out, buf); else free(buf);
}

/* Full 32 MB grid copy for offline analysis (probe mode only). */
static void probe_dump_grid(void) {
    uintptr_t grid = rd_ptr(ADDR_PHYSICAL_GRID_PTR);
    if (!grid) return;
    char path[PATH_MAX];
    snprintf(path, sizeof path, "%s/probe", g_dir);
    mkdir(path, 0755);
    snprintf(path, sizeof path, "%s/probe/physical_grid.bin", g_dir);
    FILE *f = fopen(path, "wb");
    if (!f) return;
    static unsigned char chunk[GRID_NUM_CELLS * GRID_CELL_BYTES];
    for (int row = 0; row < GRID_NUM_CELLS; row++) {
        memset(chunk, 0, sizeof chunk);
        rd(grid + (uintptr_t)row * sizeof chunk, chunk, sizeof chunk);
        fwrite(chunk, sizeof chunk, 1, f);
    }
    float tail[3] = {0};
    rd(grid + OFF_GRID_BASE_HEIGHT, tail, sizeof tail);
    fwrite(tail, sizeof tail, 1, f);
    fclose(f);
    logf_("physical grid dumped to %s", path);
}

static void battle_end(void) {
    memset(g_rams, 0, sizeof g_rams);
    if (!g_probe && g_ai_alliance >= 0) take_control(false);
    g_in_battle = false;
    idmap_reset();
    logf_("battle end");
    send_line("{\"type\":\"battle_end\"}\n");
}

/* Once per battle: the town's buildings (index = id for attack_building), kind, position, damage. */
/* Once per battle: the street graph of the town (nodes in world coordinates, links with width and length). */
static void send_streets(void) {
    uintptr_t s = rd_ptr(ADDR_STREETS_PTR);
    int ninst = s ? (int)rd_u32(s + STREETS_NUM_INSTANCES) : 0;
    if (ninst <= 0 || ninst > 5) { logf_("no street plan (instances %d)", ninst); return; }
    size_t cap = 256, len = 0;
    char *buf = malloc(cap);
#define GROW(extra) do { if (len + (extra) >= cap) { cap = (cap + (extra)) * 2; buf = realloc(buf, cap); } } while (0)
    len += snprintf(buf + len, cap - len, "{\"type\":\"streets\",\"instances\":[");
    int total_nodes = 0, total_links = 0;
    for (int i = 0; i < ninst; i++) {
        uintptr_t inst = s + STREETS_INSTANCES + (uintptr_t)i * STREETS_INSTANCE_STRIDE;
        uintptr_t plan = rd_ptr(inst + INST_PLAN);
        float ix = 0, iy = 0;
        rd(inst + INST_X, &ix, 4);
        rd(inst + INST_Y, &iy, 4);
        int orient = (int)rd_u32(inst + INST_ORIENT);
        uintptr_t nodes = rd_ptr(plan + PLAN_NODES), links = rd_ptr(plan + PLAN_LINKS);
        int nn = (int)rd_u32(plan + PLAN_NUM_NODES), nl = (int)rd_u32(plan + PLAN_NUM_LINKS);
        if (!plan || !nodes || nn <= 0 || nn > 4096 || nl < 0 || nl > 8192) continue;
        GROW(128);
        len += snprintf(buf + len, cap - len, "%s{\"x\":%.2f,\"y\":%.2f,\"orient\":%d,\"nodes\":[",
                        i ? "," : "", ix, iy, orient);
        for (int k = 0; k < nn; k++) {
            float p[2] = {0, 0}, wx, wy;
            rd(nodes + (uintptr_t)k * NODE_STRIDE, p, 8);
            switch (orient) { /* as BATTLE_MAP_STREETS 0x1403360 */
            case 1: wx = p[1]; wy = -p[0]; break;
            case 2: wx = -p[0]; wy = -p[1]; break;
            case 3: wx = -p[1]; wy = p[0]; break;
            default: wx = p[0]; wy = p[1];
            }
            GROW(40);
            len += snprintf(buf + len, cap - len, "%s[%.1f,%.1f]", k ? "," : "", wx + ix, wy + iy);
        }
        GROW(16);
        len += snprintf(buf + len, cap - len, "],\"links\":[");
        for (int k = 0; k < nl; k++) {
            uintptr_t l = links + (uintptr_t)k * LINK_STRIDE;
            float wl[2] = {0, 0};
            rd(l, wl, 8);
            uintptr_t a = rd_ptr(l + 8), b = rd_ptr(l + 0x10);
            long ia = a >= nodes ? (long)((a - nodes) / NODE_STRIDE) : -1;
            long ib = b >= nodes ? (long)((b - nodes) / NODE_STRIDE) : -1;
            if (ia >= nn) ia = -1;
            if (ib >= nn) ib = -1;
            GROW(64);
            len += snprintf(buf + len, cap - len, "%s[%ld,%ld,%.1f,%.1f]", k ? "," : "", ia, ib, wl[0], wl[1]);
        }
        GROW(8);
        len += snprintf(buf + len, cap - len, "]}");
        total_nodes += nn;
        total_links += nl;
    }
    GROW(8);
    snprintf(buf + len, cap - len, "]}\n");
#undef GROW
    logf_("streets sent: %d plan instance(s), %d nodes, %d links", ninst, total_nodes, total_links);
    if (g_connected) q_push(&g_out, buf); else free(buf);
}

static int hexdump(char *out, size_t cap, uintptr_t addr, size_t n) {
    static const char hx[] = "0123456789abcdef";
    unsigned char b[0x200];
    if (n > sizeof b || cap < n * 2 + 1 || !rd(addr, b, n)) { if (cap) out[0] = 0; return 0; }
    for (size_t i = 0; i < n; i++) { out[2 * i] = hx[b[i] >> 4]; out[2 * i + 1] = hx[b[i] & 15]; }
    out[2 * n] = 0;
    return (int)(2 * n);
}

/* Once per battle, for decoding: raw bytes of every wall platform and of the platform links in its list. */
static void send_walls_raw(void) {
    uintptr_t w = rd_ptr(ADDR_WALLS_PTR);
    int n = w ? (int)rd_u32(w + WALLS_COUNT) : 0;
    if (n <= 0 || n > 400) { logf_("no wall platforms (%d)", n); return; }
    size_t cap = 64 + (size_t)n * (0x110 * 2 + 64 + 12 * (0x40 * 2 + 40)), len = 0;
    char *buf = malloc(cap), hex[0x200 * 2 + 1];
    len += snprintf(buf + len, cap - len, "{\"type\":\"walls_raw\",\"platforms\":[");
    int sent = 0;
    for (int i = 0; i < n; i++) {
        uintptr_t p = rd_ptr(w + WALLS_PLATFORMS + (uintptr_t)i * 8);
        if (!p) continue;
        hexdump(hex, sizeof hex, p, 0x110);
        len += snprintf(buf + len, cap - len, "%s{\"i\":%d,\"ptr\":%lu,\"raw\":\"%s\",\"links\":[",
                        sent++ ? "," : "", i, (unsigned long)p, hex);
        uintptr_t l = rd_ptr(p + 0x70);
        for (int k = 0; l && k < 12; k++, l = rd_ptr(l + 0x10)) {
            hexdump(hex, sizeof hex, l, 0x40);
            len += snprintf(buf + len, cap - len, "%s{\"ptr\":%lu,\"raw\":\"%s\"}", k ? "," : "", (unsigned long)l, hex);
        }
        len += snprintf(buf + len, cap - len, "]}");
    }
    snprintf(buf + len, cap - len, "]}\n");
    logf_("wall platforms dumped: %d", sent);
    if (g_connected) q_push(&g_out, buf); else free(buf);
}

/* Once per battle, for decoding: every PHYSICAL_PLATFORM that is not a wall platform (type 1). */
static void send_platforms_raw(void) {
    int n = (int)rd_u32(ADDR_PLATFORM_COUNT);
    if (n <= 0 || n > 4000) { logf_("platform table: %d", n); return; }
    size_t cap = 64 + (size_t)n * (0x110 * 2 + 64 + 12 * (0x40 * 2 + 40)), len = 0;
    char *buf = malloc(cap), hex[0x200 * 2 + 1];
    len += snprintf(buf + len, cap - len, "{\"type\":\"platforms_raw\",\"count\":%d,\"platforms\":[", n);
    int sent = 0;
    for (int i = 0; i < n; i++) {
        uintptr_t p = rd_ptr(ADDR_PLATFORM_TABLE + (uintptr_t)i * 8);
        if (!p || rd_u32(p + 0xc) == 1) continue;
        hexdump(hex, sizeof hex, p, 0x110);
        len += snprintf(buf + len, cap - len, "%s{\"i\":%d,\"vt\":%lu,\"raw\":\"%s\",\"links\":[",
                        sent++ ? "," : "", i, (unsigned long)rd_ptr(p), hex);
        uintptr_t l = rd_ptr(p + 0x70);
        for (int k = 0; l && k < 12; k++, l = rd_ptr(l + 0x10)) {
            hexdump(hex, sizeof hex, l, 0x40);
            len += snprintf(buf + len, cap - len, "%s{\"raw\":\"%s\"}", k ? "," : "", hex);
        }
        len += snprintf(buf + len, cap - len, "]}");
    }
    snprintf(buf + len, cap - len, "]}\n");
    logf_("other platforms dumped: %d of %d", sent, n);
    if (g_connected) q_push(&g_out, buf); else free(buf);
}

/* Once per battle: the links between map areas - river crossings (bridges, fords) and others. */
static void send_crossings(void) {
    uintptr_t a = rd_ptr(ADDR_AREAS_PTR);
    int n = a ? (int)rd_u32(a + AREAS_NUM_LINKS) : 0;
    uintptr_t links = a ? rd_ptr(a + AREAS_LINKS) : 0;
    if (n <= 0 || n > 100 || !links) { logf_("no area links (%d)", n); return; }
    size_t cap = 64 + (size_t)n * (AREA_LINK_SIZE * 2 + 200), len = 0;
    char *buf = malloc(cap), hex[AREA_LINK_SIZE * 2 + 1];
    len += snprintf(buf + len, cap - len, "{\"type\":\"crossings\",\"links\":[");
    for (int i = 0; i < n; i++) {
        uintptr_t l = links + (uintptr_t)i * AREA_LINK_SIZE;
        float p[4] = {0}, w = 0;
        rd(l + 0x10, p, 8);
        rd(l + 0x30, p + 2, 8);
        rd(l + 0x50, &w, 4);
        uintptr_t a1 = rd_ptr(l + 0x8), a2 = rd_ptr(l + 0x28);
        hexdump(hex, sizeof hex, l, AREA_LINK_SIZE);
        len += snprintf(buf + len, cap - len,
                        "%s{\"kind\":%d,\"p1\":[%.1f,%.1f],\"p2\":[%.1f,%.1f],\"width\":%.1f,"
                        "\"area1\":%d,\"area2\":%d,\"raw\":\"%s\"}",
                        i ? "," : "", (int)rd_u32(l + 4), p[0], p[1], p[2], p[3], w,
                        a1 ? (int)rd_u32(a1) : -1, a2 ? (int)rd_u32(a2) : -1, hex);
        logf_("area link %d: type %d (%.0f,%.0f)-(%.0f,%.0f) width %.1f areas %d/%d", i, (int)rd_u32(l + 4),
              p[0], p[1], p[2], p[3], w, a1 ? (int)rd_u32(a1) : -1, a2 ? (int)rd_u32(a2) : -1);
    }
    snprintf(buf + len, cap - len, "]}\n");
    if (g_connected) q_push(&g_out, buf); else free(buf);
}

static void send_buildings(void) {
    static uintptr_t list[4096];
    int n = battle_buildings(list, 4096);
    if (n <= 0) { logf_("no town buildings"); return; }
    size_t cap = 64 + (size_t)n * 120, len = 0;
    char *buf = malloc(cap);
    len += snprintf(buf + len, cap - len, "{\"type\":\"buildings\",\"items\":[");
    int sent = 0, unknown = 0;
    for (int i = 0; i < n; i++) {
        int kind = building_kind(list[i]);
        if (kind < 0) { unknown++; continue; }
        float x = 0, y = 0;
        rd(list[i] + OFF_BUILDING_X, &x, 4);
        rd(list[i] + OFF_BUILDING_Y, &y, 4);
        unsigned char flag = 0;
        rd(list[i] + OFF_BUILDING_FLAG74, &flag, 1);
        len += snprintf(buf + len, cap - len, "%s{\"id\":%d,\"kind\":%d,\"x\":%.1f,\"y\":%.1f,\"damage\":%u,\"max_hp\":%u,\"flag74\":%u}",
                        sent++ ? "," : "", i, kind, x, y, rd_u32(list[i] + OFF_BUILDING_DAMAGE), rd_u32(list[i] + OFF_BUILDING_MAX_HP), flag);
    }
    snprintf(buf + len, cap - len, "]}\n");
    logf_("buildings sent: %d (%d of other classes skipped)", sent, unknown);
    if (g_connected) q_push(&g_out, buf); else free(buf);
}

/* Every few seconds: [id, damage, max hp, flag] for walls/gates/towers (breaches, broken gates, fallen towers). */
static void send_building_status(void) {
    static uintptr_t list[4096];
    int n = battle_buildings(list, 4096);
    if (n <= 0) return;
    size_t cap = 64 + (size_t)n * 64, len = 0;
    char *buf = malloc(cap);
    len += snprintf(buf + len, cap - len, "{\"type\":\"building_status\",\"items\":[");
    int k = 0;
    for (int i = 0; i < n; i++) {
        if (building_kind(list[i]) < 2) continue;
        unsigned char flag = 0;
        rd(list[i] + OFF_BUILDING_FLAG74, &flag, 1);
        int open = -1, owner = -1; /* gates: open now, alliance controlling it */
        if (building_kind(list[i]) == 4) {
            unsigned char o = 0, dis = 0;
            rd(list[i] + OFF_GATE_OPEN, &o, 1);
            rd(list[i] + OFF_GATE_DISABLED, &dis, 1);
            open = o;
            uintptr_t ow = rd_ptr(list[i] + OFF_GATE_OWNER);
            if (ow && !dis) owner = (int)rd_u32(ow + OFF_GATE_OWNER_ALLIANCE);
        }
        len += snprintf(buf + len, cap - len, "%s[%d,%u,%u,%u,%d,%d]", k++ ? "," : "", i, rd_u32(list[i] + OFF_BUILDING_DAMAGE),
                        rd_u32(list[i] + OFF_BUILDING_MAX_HP), flag, open, owner);
    }
    snprintf(buf + len, cap - len, "]}\n");
    if (g_connected) q_push(&g_out, buf); else free(buf);
}

/* Reinforcements: the game gives a unit entering the field back to its own AI (2026-11-05: army 2 of a sally
 * battle came on ~30 s in and then followed the game AI's orders instead of ours). Take back every unit of the
 * alliance that is on the field and not ours, unless it was handed over on purpose (siege equipment, "control"
 * for one unit), and tell the server so it sends that unit its order again. */
static void retake_units(void) {
    if (!g_taken || g_ai_alliance < 0) return;
    for (int i = 0; i < g_nunits; i++) {
        unit_ref *u = &g_units[i];
        if (u->alliance != g_ai_alliance || !unit_on_field(u->ptr) || ram_handed(u->ptr)) continue;
        uint32_t *c = (uint32_t *)(u->ptr + OFF_UNIT_CONTROLLER);
        if (*c == CONTROLLER_SCRIPT) continue;
        int k = 0;
        while (k < g_nhanded && g_handed[k] != u->ptr) k++;
        if (k < g_nhanded) continue;
        logf_("unit %d was under the game's AI again (controller %u) - taken back", u->id, *c);
        *c = CONTROLLER_SCRIPT;
        send_line("{\"type\":\"retaken\",\"unit\":%d}\n", u->id);
    }
}

static void handle_order(const char *line) {
    mj_obj o;
    if (!mj_parse_flat(line, &o)) return;
    const char *type = mj_str(&o, "type");
    if (type && strcmp(type, "control") == 0) { /* {"type":"control","take":true[,"unit":id]} */
        int uid = (int)mj_num(&o, "unit", -1);
        if (uid < 0) {
            take_control(mj_bool(&o, "take", true));
        } else if (unit_by_id(uid)) { /* one unit: hand it to the game's AI or take it back */
            uintptr_t ptr = unit_by_id(uid)->ptr;
            uint32_t *c = (uint32_t *)(ptr + OFF_UNIT_CONTROLLER);
            *c = mj_bool(&o, "take", true) ? CONTROLLER_SCRIPT : CONTROLLER_AI;
            int k = 0;
            while (k < g_nhanded && g_handed[k] != ptr) k++;
            if (*c == CONTROLLER_AI && k == g_nhanded && g_nhanded < MAX_UNITS) g_handed[g_nhanded++] = ptr;
            if (*c == CONTROLLER_SCRIPT && k < g_nhanded) g_handed[k] = g_handed[--g_nhanded];
            logf_("unit %d %s", uid, *c == CONTROLLER_SCRIPT ? "taken" : "handed to the game AI");
        }
        return;
    }
    const char *cmd = mj_str(&o, "cmd");
    unit_ref *u = unit_by_id((int)mj_num(&o, "unit", -1));
    if (!cmd || !u || !unit_on_field(u->ptr)) return;
    if (ram_handed(u->ptr)) return; /* the game's AI has it until its siege equipment is used */
    if (!strcmp(cmd, "move"))
        do_move(u->ptr, (float)mj_num(&o, "x", 0), (float)mj_num(&o, "y", 0), mj_bool(&o, "run", false),
                (float)mj_num(&o, "facing", -1), (int)mj_num(&o, "width", 0));
    else if (!strcmp(cmd, "attack")) {
        unit_ref *t = unit_by_id((int)mj_num(&o, "target", -1));
        if (t && unit_on_field(t->ptr)) do_attack(u->ptr, t->ptr, mj_bool(&o, "run", true), mj_bool(&o, "melee", false));
    } else if (!strcmp(cmd, "halt")) do_halt(u->ptr);
    else if (!strcmp(cmd, "attack_building")) do_attack_building(u->ptr, (int)mj_num(&o, "building", -1), mj_bool(&o, "run", false));
    else if (!strcmp(cmd, "drop_engine")) do_drop_engine(u->ptr);
    else if (!strcmp(cmd, "special_ability")) do_special_ability(u->ptr);
    else if (!strcmp(cmd, "rally")) do_rally(u->ptr, mj_bool(&o, "on", true));
    else if (!strcmp(cmd, "withdraw")) do_withdraw(u->ptr);
    else if (!strcmp(cmd, "fire_at_will")) do_mode(u->ptr, 0x2, mj_bool(&o, "on", true));
    else if (!strcmp(cmd, "skirmish")) do_mode(u->ptr, 0x4, mj_bool(&o, "on", true));
    else if (!strcmp(cmd, "formation") && mj_str(&o, "formation")) do_formation(u->ptr, mj_str(&o, "formation"));
    else logf_("unsupported cmd %s", cmd);
}

/* Orders arrive as {"type":"orders","orders":[{...},{...}]}; the network
 * thread splits them into one flat object per line before queueing. */
static void drain_orders(void) {
    char *l;
    int n = 0;
    while ((l = q_pop(&g_in))) {
        handle_order(l);
        free(l);
        n++;
    }
    if (n) logf_("executed %d orders", n);
}

static float g_last_xy[MAX_UNITS][2];

static void send_state(void) {
    /* Each unit entry stays well under 768 bytes. */
    size_t cap = 256 + (size_t)g_nunits * 768, len = 0;
    char *buf = malloc(cap);
    uintptr_t battle_p = rd_ptr(ADDR_BATTLE_PTR);
    int stage = battle_p ? (int)rd_u32(battle_p + BATTLE_STAGE) : -1;
    static int last_stage = -2;
    if (stage != last_stage) {
        logf_("battle stage %d -> %d", last_stage, stage);
        last_stage = stage;
    }
    len += snprintf(buf + len, cap - len, "{\"type\":\"state\",\"t\":%.2f,\"stage\":%d,\"units\":[",
                    now_s() - g_battle_t0, stage);
    /* who attacks whom: the handle each unit is known by, then each unit's current attack order */
    /* the handle is 0x14 bytes (UNIT::order_attack copies +0x78..+0x8b): a pointer that may be the same for
       every unit, then the unit's own id - compare all of it */
    static uint8_t handle[MAX_UNITS][UNIT_HANDLE_LEN];
    for (int i = 0; i < g_nunits; i++) {
        memset(handle[i], 0, UNIT_HANDLE_LEN);
        rd(g_units[i].ptr + OFF_UNIT_HANDLE, handle[i], UNIT_HANDLE_LEN);
    }
    for (int i = 0; i < g_nunits && len + 768 < cap; i++) {
        unit_ref *u = &g_units[i];
        uint32_t st = rd_u32(u->ptr + OFF_UNIT_STATE), ml = rd_u32(u->ptr + OFF_UNIT_MORALE_LEVEL);
        bool on_field = unit_on_field(u->ptr);
        double ammo = on_field ? unit_ammo(u->ptr) : -1;
        uint32_t fat = rd_u32(u->ptr + OFF_UNIT_FATIGUE_LEVEL);
        int eng_type = -1, engines = on_field ? unit_engines(u->ptr, &eng_type) : 0;
        bool ab_ready = false, ab_on = false;
        int ability = on_field ? unit_ability(u->ptr, &ab_ready, &ab_on) : 0;
        int target = -1;
        char tgt_s[16] = "null";
        uint16_t width_men = 0;
        float walk_speed = on_field ? unit_speed(u->ptr, VF_UNIT_WALK_SPEED) : 0;
        float run_speed = on_field ? unit_speed(u->ptr, VF_UNIT_RUN_SPEED) : 0;
        float charge_speed = on_field ? unit_speed(u->ptr, VF_UNIT_CHARGE_SPEED) : 0;
        if (on_field) rd(u->ptr + OFF_UNIT_FORMATION_DATA + OFF_FDATA_WIDTH_MEN, &width_men, 2);
        uintptr_t bd = rd_ptr(u->ptr + OFF_UNIT_BATTLE_DATA);
        /* what the unit was ordered to do (by its player or AI): the final move destination in its order queue
           and whether the current order is at a run */
        char dest_s[48] = "null";
        int order_run = 0;
        uint32_t n_orders = bd ? rd_u32(bd + OFF_BD_ORDER_COUNT) : 0;
        if (n_orders > 0 && n_orders <= 16) {
            uint32_t head = rd_u32(bd + OFF_BD_ORDER_HEAD);
            uint8_t run_b = 0;
            rd(bd + OFF_BD_ORDERS + (uintptr_t)(head % 16) * ORDER_STRIDE + OFF_ORDER_RUN, &run_b, 1);
            order_run = run_b ? 1 : 0;
            for (uint32_t k = 0; k < n_orders; k++) {
                uintptr_t e = bd + OFF_BD_ORDERS + (uintptr_t)((head + k) % 16) * ORDER_STRIDE;
                float d[2];
                if (rd_u32(e) == ORDER_TYPE_MOVE && rd(e + OFF_ORDER_MOVE_DEST, d, sizeof d) &&
                    isfinite(d[0]) && isfinite(d[1]) && fabsf(d[0]) < 1e5f && fabsf(d[1]) < 1e5f)
                    snprintf(dest_s, sizeof dest_s, "[%.1f,%.1f]", d[0], d[1]);
            }
        }
        if (bd && rd_u32(bd + OFF_BD_ORDER_COUNT) > 0) {
            uintptr_t e = bd + OFF_BD_ORDERS + (uintptr_t)(rd_u32(bd + OFF_BD_ORDER_HEAD) % 16) * ORDER_STRIDE;
            uint8_t h[UNIT_HANDLE_LEN] = {0}, zero[UNIT_HANDLE_LEN] = {0};
            if (rd_u32(e) == ORDER_TYPE_ATTACK_UNIT && rd(e + OFF_ORDER_TARGET_HANDLE, h, UNIT_HANDLE_LEN) &&
                memcmp(h, zero, UNIT_HANDLE_LEN))
                for (int k = 0; k < g_nunits; k++)
                    if (k != i && !memcmp(handle[k], h, UNIT_HANDLE_LEN)) { target = g_units[k].id; break; }
        }
        if (target >= 0) snprintf(tgt_s, sizeof tgt_s, "%d", target);
        int rally = on_field ? unit_rally(u->ptr) : -1;
        char ammo_s[16] = "null";
        if (ammo >= 0) snprintf(ammo_s, sizeof ammo_s, "%.2f", ammo);
        const char *form_s = on_field ? unit_formation(u->ptr) : "unknown";
        if (on_field) ((unit_pos_fn)ADDR_UNIT_POSITION_GET)(g_last_xy[i], (void *)u->ptr);
        char name[48];
        const char *cat, *cls;
        unit_type(u->ptr, name, sizeof name, &cat, &cls);
        if (!name[0]) snprintf(name, sizeof name, "unit %02x:%02x:%02x", u->alliance, u->army, u->index);
        uint16_t ang = 0;
        uintptr_t form = rd_ptr(u->ptr + OFF_UNIT_FORMATION_OBJ);
        if (form) rd(form + OFF_FORMATION_HEADING, &ang, sizeof ang);
        len += snprintf(buf + len, cap - len,
                        "%s{\"id\":%d,\"alliance\":%d,\"name\":\"%s\",\"category\":\"%s\",\"class\":\"%s\","
                        "\"x\":%.1f,\"y\":%.1f,\"heading\":%.1f,\"soldiers\":%u,\"max_soldiers\":%u,"
                        "\"morale\":%.2f,\"fatigue\":%.2f,\"ammo\":%s,\"formation\":\"%s\",\"action\":\"%s\",\"raw\":{\"state\":\"%s\",\"morale_level\":%u,"
                        "\"controller\":%u,\"flags\":%u,\"army\":%d,\"index\":%d},\"engines\":%d,\"engine_type\":%d,"
                        "\"ability\":%d,\"ability_ready\":%s,\"ability_on\":%s,\"rally\":%d,\"target\":%s,\"width_men\":%u,\"walk_speed\":%.2f,\"run_speed\":%.2f,\"charge_speed\":%.2f,\"dest\":%s,\"order_run\":%s,\"game_ai\":%s,\"on_field\":%s}",
                        i ? "," : "", u->id, u->alliance, name, cat, cls, g_last_xy[i][0], g_last_xy[i][1],
                        ang * 360.0 / 65536.0, rd_u32(u->ptr + OFF_UNIT_SOLDIERS), rd_u32(u->ptr + OFF_UNIT_SOLDIERS_MAX),
                        ml < 7 ? MORALE_BY_LEVEL[ml] : 0.5, fat <= 5 ? fat / 5.0 : 0.0, ammo_s, form_s, unit_action(st), unit_state_name(st), ml,
                        rd_u32(u->ptr + OFF_UNIT_CONTROLLER), rd_u32(u->ptr + OFF_UNIT_MODE_FLAGS), u->army, u->index, engines, eng_type,
                        ability, ab_ready ? "true" : "false", ab_on ? "true" : "false", rally, tgt_s, width_men, walk_speed, run_speed, charge_speed, dest_s,
                        order_run ? "true" : "false", ram_handed(u->ptr) ? "true" : "false", on_field ? "true" : "false");
    }
    snprintf(buf + len, cap - len, "]}\n");
    if (g_connected) q_push(&g_out, buf); else free(buf);
}

/* Raw dumps for offline offset discovery. */
#define PROBE_UNIT_BYTES 0x1800
#define PROBE_SUB_BYTES 0x1100
static void probe_dump(void) {
    char path[PATH_MAX];
    snprintf(path, sizeof path, "%s/probe", g_dir);
    mkdir(path, 0755);
    snprintf(path, sizeof path, "%s/probe/%013.3f.bin", g_dir, now_s() - g_battle_t0 + 1e6);
    FILE *f = fopen(path, "wb");
    if (!f) return;
    static unsigned char buf[PROBE_UNIT_BYTES];
    uint32_t hdr[2] = {0x31424f52 /* "ROB1" */, (uint32_t)g_nunits};
    fwrite(hdr, sizeof hdr, 1, f);
    for (int i = 0; i < g_nunits; i++) {
        unit_ref *u = &g_units[i];
        int32_t meta[4] = {u->id, u->alliance, u->army, u->index};
        uint64_t ptrs[2] = {u->ptr, rd_ptr(u->ptr + 0x90)};
        fwrite(meta, sizeof meta, 1, f);
        fwrite(ptrs, sizeof ptrs, 1, f);
        memset(buf, 0, sizeof buf);
        rd(u->ptr, buf, PROBE_UNIT_BYTES);
        fwrite(buf, PROBE_UNIT_BYTES, 1, f);
        memset(buf, 0, PROBE_SUB_BYTES);
        if (ptrs[1]) rd(ptrs[1], buf, PROBE_SUB_BYTES);
        fwrite(buf, PROBE_SUB_BYTES, 1, f);
        /* strings reachable from the first 0x400 bytes (1 and 2 levels) -> unit type name etc. */
        for (int off = 0; off < 0x400; off += 8) {
            uintptr_t p = rd_ptr(u->ptr + off);
            for (int lvl = 0; lvl < 2 && p; lvl++) {
                char s[48] = {0};
                if (rd(p, s, sizeof s - 1)) {
                    int k = 0;
                    while (s[k] >= 0x20 && s[k] < 0x7f) k++;
                    if (k >= 4 && s[k] == 0) {
                        int32_t rec[2] = {off, lvl};
                        fwrite(rec, sizeof rec, 1, f);
                        fwrite(s, sizeof s, 1, f);
                    }
                }
                p = rd_ptr(p);
            }
        }
        int32_t end[2] = {-1, -1};
        fwrite(end, sizeof end, 1, f);
    }
    fclose(f);
}

typedef void (*tick_fn)(void *self);
static tick_fn g_orig_tick;

static void hooked_tick(void *self) {
    g_orig_tick(self);

    double t = now_s(), last = g_last_tick;
    g_last_tick = t;
    if (g_ticks_stopped) { /* came back after a pause/menu or a new battle */
        g_ticks_stopped = false;
        enumerate_units();
        if (same_battle()) {
            /* battle time stands still while the game is paused: shift its start by the pause (2026-11-06: a 30 s
             * pause counted as battle time made units look stuck for 50 s) */
            g_battle_t0 += t - last;
            send_line("{\"type\":\"resume\"}\n"); /* same battle resumed */
        } else {
            g_in_battle = false; /* old battle is gone: don't restore controllers on freed units */
            idmap_reset();
            send_line("{\"type\":\"battle_end\"}\n");
        }
    }
    enumerate_units();
    if (g_nunits == 0) {
        if (g_in_battle) battle_end();
        return;
    }
    if (!g_in_battle || g_known == 0) { /* none of these units was in the last battle: a new one */
        if (g_in_battle) battle_end();
        idmap_reset();
        enumerate_units(); /* fresh ids: index order */
        battle_begin();
    }
    if (g_probe) {
        if (t - g_last_probe >= 1.0) {
            g_last_probe = t;
            probe_dump();
        }
        return;
    }
    if (g_release_pending) {
        g_release_pending = false;
        if (!g_probe && g_ai_alliance >= 0) take_control(false);
        logf_("AI server lost - the game's own AI commands alliance %d again", g_ai_alliance);
    }
    if (g_terrain_pending && g_connected) {
        g_terrain_pending = false;
        send_terrain();
        send_buildings();
        send_streets();
        send_walls_raw();
        send_platforms_raw();
        send_crossings();
    }
    drain_orders();
    if (g_in_battle) rams_update();
    if (t - g_last_state >= 0.5) {
        g_last_state = t;
        retake_units();
        send_state();
        idmap_note_men();
        static int n_states;
        if (++n_states % 10 == 0) send_building_status(); /* every ~5 s */
    }
}

/* ------------------------------------------------------------------ */
/* network thread                                                      */

static void split_orders_into_queue(const char *line) {
    /* line: {"type":"orders","orders":[{..},{..}]} or {"type":"control",...} */
    const char *arr = strstr(line, "\"orders\"");
    if (!arr) {
        q_push(&g_in, strdup(line));
        return;
    }
    const char *p = strchr(arr, '[');
    while (p && (p = strchr(p, '{'))) {
        const char *e = strchr(p, '}');
        if (!e) break;
        q_push(&g_in, strndup(p, (size_t)(e - p + 1)));
        p = e + 1;
    }
}

static void *net_thread(void *arg) {
    (void)arg;
    int port = getenv("ROME_AI_PORT") ? atoi(getenv("ROME_AI_PORT")) : 7777;
    for (;;) {
        int fd = socket(AF_INET, SOCK_STREAM, 0);
        struct sockaddr_in sa = {.sin_family = AF_INET, .sin_port = htons((uint16_t)port)};
        sa.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
        if (connect(fd, (struct sockaddr *)&sa, sizeof sa) != 0) {
            close(fd);
            sleep(2);
            continue;
        }
        int one = 1;
        setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof one);
        fcntl(fd, F_SETFL, O_NONBLOCK);
        logf_("connected to AI server on port %d", port);
        g_connected = true;
        const char *hello = "{\"type\":\"hello\",\"version\":1,\"game\":\"rome-remastered\"}\n";
        send(fd, hello, strlen(hello), MSG_NOSIGNAL);
        if (g_in_battle) {
            send_line("{\"type\":\"battle_start\",\"ai_alliance\":%d}\n", g_ai_alliance);
            g_terrain_pending = true;
        }

        char rbuf[1 << 16];
        size_t rlen = 0;
        bool alive = true;
        while (alive) {
            char *out;
            while ((out = q_pop(&g_out))) {
                size_t n = strlen(out), off = 0;
                while (off < n) {
                    ssize_t w = send(fd, out + off, n - off, MSG_NOSIGNAL);
                    if (w > 0) off += (size_t)w;
                    else if (w < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) usleep(1000);
                    else { alive = false; break; }
                }
                free(out);
            }
            ssize_t r = recv(fd, rbuf + rlen, sizeof rbuf - 1 - rlen, 0);
            if (r == 0) alive = false;
            else if (r > 0) {
                rlen += (size_t)r;
                rbuf[rlen] = 0;
                char *start = rbuf, *nl;
                while ((nl = strchr(start, '\n'))) {
                    *nl = 0;
                    if (nl > start) split_orders_into_queue(start);
                    start = nl + 1;
                }
                rlen -= (size_t)(start - rbuf);
                memmove(rbuf, start, rlen);
                if (rlen >= sizeof rbuf - 1) rlen = 0; /* oversized line: drop */
            } else if (errno != EAGAIN && errno != EWOULDBLOCK) alive = false;
            else usleep(5000);

            /* battle ended without a tick telling us (back to campaign map / menu) */
            if (g_in_battle && !g_ticks_stopped && now_s() - g_last_tick > 3.0) {
                g_ticks_stopped = true;
                logf_("no battle ticks for 3s - paused (or left the battle)");
                send_line("{\"type\":\"pause\"}\n");
            }
        }
        g_connected = false;
        close(fd);
        logf_("disconnected from AI server");
        if (g_in_battle) g_release_pending = true;
        sleep(1);
    }
    return NULL;
}

/* ------------------------------------------------------------------ */
/* install                                                             */

static bool verify_bytes(void) {
    static const struct { uintptr_t addr; unsigned char b[8]; } exp[] = {
        {ADDR_GAME_UPDATE_TICK, {0x55, 0xbe, 0xc8, 0xc9, 0xf9, 0x03, 0x48, 0x89}},
        {ADDR_ORDER_MOVE_DESC_CTOR, {0x31, 0xc0, 0x66, 0x0f, 0xef, 0xc0, 0xba, 0x01}},
        {ADDR_ORDER_MOVE_DESC_SETDEST, {0xf3, 0x0f, 0x10, 0x06, 0xf3, 0x0f, 0x11, 0x07}},
        {ADDR_UNIT_ORDER_MOVE, {0x55, 0x8d, 0x46, 0xfa, 0x48, 0x89, 0xe5, 0x41}},
        {ADDR_UNIT_ORDER_ATTACK, {0x41, 0x57, 0x41, 0x56, 0x41, 0x55, 0x49, 0x89}},
        {ADDR_UNIT_ORDER_HALT, {0x80, 0x3d, 0x99, 0xe1, 0x37, 0x05, 0x00, 0x41}},
        {ADDR_UNIT_SET_MODE_FLAGS, {0x41, 0x56, 0x41, 0x89, 0xce, 0x41, 0x55, 0x41}},
        {ADDR_OPERATOR_DELETE_SIZED, {0xf3, 0x0f, 0x1e, 0xfa, 0xe9, 0xd7, 0xff, 0xff}},
        {ADDR_UNIT_POSITION_GET, {0x41, 0x54, 0x49, 0x89, 0xfc, 0x55, 0x48, 0x89}},
        {ADDR_UNIT_ORDER_FORMATION, {0x44, 0x0f, 0xbf, 0x87, 0x86, 0x0d, 0x00, 0x00}},
        {ADDR_ORDER_MOVE_DESC_FACING, {0xc6, 0x47, 0x41, 0x01, 0x66, 0x89, 0x77, 0x1c}},
        {ADDR_ORDER_MOVE_DESC_WIDTH, {0xc6, 0x47, 0x43, 0x01, 0x89, 0x77, 0x2c, 0xc3}},
        {ADDR_UNIT_ORDER_ATTACK_BUILDING, {0x41, 0x57, 0x49, 0x89, 0xff, 0x41, 0x56, 0x41}}, /* 0x21ff150 */
    };
    for (size_t i = 0; i < sizeof exp / sizeof exp[0]; i++) {
        unsigned char got[8];
        if (!rd(exp[i].addr, got, 8) || memcmp(got, exp[i].b, 8) != 0) {
            logf_("byte check failed at %#lx - different game version? hook disabled", (unsigned long)exp[i].addr);
            return false;
        }
    }
    if (rd_ptr(ADDR_GAME_UPDATE_TICK_SLOT) != ADDR_GAME_UPDATE_TICK) {
        logf_("vtable slot mismatch - hook disabled");
        return false;
    }
    return true;
}

static bool patch_slot(uintptr_t slot, void *fn) {
    long pg = sysconf(_SC_PAGESIZE);
    void *page = (void *)(slot & ~(uintptr_t)(pg - 1));
    if (mprotect(page, (size_t)pg, PROT_READ | PROT_WRITE) != 0) return false;
    *(void **)slot = fn;
    mprotect(page, (size_t)pg, PROT_READ);
    return true;
}

__attribute__((constructor)) static void rome_hook_init(void) {
    char exe[PATH_MAX];
    ssize_t n = readlink("/proc/self/exe", exe, sizeof exe - 1);
    if (n <= 0) return;
    exe[n] = 0;
    size_t el = strlen(exe);
    if (el < 9 || strcmp(exe + el - 9, "/bin/Rome") != 0) return; /* CEF helpers, launcher tools, ... */

    Dl_info di;
    if (getenv("ROME_AI_DIR")) {
        snprintf(g_dir, sizeof g_dir, "%s", getenv("ROME_AI_DIR"));
    } else if (dladdr((void *)rome_hook_init, &di) && di.dli_fname && strchr(di.dli_fname, '/')) {
        snprintf(g_dir, sizeof g_dir, "%s", di.dli_fname);
        char *slash = strrchr(g_dir, '/');
        if (slash) *slash = 0;
    } else {
        snprintf(g_dir, sizeof g_dir, "/tmp");
    }
    /* Steam's pressure-vessel container mounts the project read-only: fall back to ~/.local/share/rome-ai */
    char logpath[PATH_MAX];
    snprintf(logpath, sizeof logpath, "%s/hook.log", g_dir);
    g_log = fopen(logpath, "a");
    if (!g_log && getenv("HOME")) {
        snprintf(g_dir, sizeof g_dir, "%s/.local/share/rome-ai", getenv("HOME"));
        mkdir(g_dir, 0755);
        snprintf(logpath, sizeof logpath, "%s/hook.log", g_dir);
        g_log = fopen(logpath, "a");
    }
    if (!g_log) fprintf(stderr, "rome_hook: cannot open %s: %s\n", logpath, strerror(errno));
    logf_("rome_hook loaded into %s (pid %d)", exe, getpid());

    g_memfd = open("/proc/self/mem", O_RDONLY | O_CLOEXEC);
    if (g_memfd < 0 || !verify_bytes()) return;

    g_probe = getenv("ROME_AI_PROBE") && atoi(getenv("ROME_AI_PROBE"));
    g_orig_tick = (tick_fn)ADDR_GAME_UPDATE_TICK;
    if (!patch_slot(ADDR_GAME_UPDATE_TICK_SLOT, (void *)hooked_tick)) {
        logf_("mprotect failed: %s", strerror(errno));
        return;
    }
    logf_("hooked game_update_tick%s", g_probe ? " (PROBE mode)" : "");

    pthread_t th;
    pthread_create(&th, NULL, net_thread, NULL);
    pthread_detach(th);
}
