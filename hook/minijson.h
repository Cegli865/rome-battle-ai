/* Tiny parser for flat JSON objects: {"k":"str","n":1.5,"b":true,...}.
 * Enough for single orders; nested values are skipped. */
#pragma once
#include <stdbool.h>
#include <stdlib.h>
#include <string.h>

#define MJ_MAX 16

typedef struct {
    int n;
    struct { char key[24]; char str[64]; double num; int kind; /* 0 str, 1 num, 2 bool, 3 other */ } kv[MJ_MAX];
} mj_obj;

static const char *mj_ws(const char *p) { while (*p == ' ' || *p == '\t' || *p == '\r' || *p == '\n') p++; return p; }

static const char *mj_string(const char *p, char *out, size_t cap) {
    size_t n = 0;
    if (*p != '"') return NULL;
    for (p++; *p && *p != '"'; p++) {
        char c = *p;
        if (c == '\\' && p[1]) c = *++p;
        if (n + 1 < cap) out[n++] = c;
    }
    out[n] = 0;
    return *p == '"' ? p + 1 : NULL;
}

static const char *mj_skip(const char *p) { /* skip a nested value */
    int depth = 0;
    bool in_str = false;
    for (; *p; p++) {
        if (in_str) { if (*p == '\\' && p[1]) p++; else if (*p == '"') in_str = false; continue; }
        if (*p == '"') in_str = true;
        else if (*p == '{' || *p == '[') depth++;
        else if (*p == '}' || *p == ']') { if (depth-- == 0) return p; if (depth == 0) return p + 1; }
        else if (*p == ',' && depth == 0) return p;
    }
    return p;
}

static bool mj_parse_flat(const char *p, mj_obj *o) {
    o->n = 0;
    p = mj_ws(p);
    if (*p++ != '{') return false;
    for (;;) {
        p = mj_ws(p);
        if (*p == '}') return true;
        if (o->n >= MJ_MAX) return true;
        typeof(o->kv[0]) *kv = &o->kv[o->n];
        if (!(p = mj_string(p, kv->key, sizeof kv->key))) return false;
        p = mj_ws(p);
        if (*p++ != ':') return false;
        p = mj_ws(p);
        if (*p == '"') { kv->kind = 0; if (!(p = mj_string(p, kv->str, sizeof kv->str))) return false; }
        else if (!strncmp(p, "true", 4)) { kv->kind = 2; kv->num = 1; p += 4; }
        else if (!strncmp(p, "false", 5)) { kv->kind = 2; kv->num = 0; p += 5; }
        else if (*p == '-' || (*p >= '0' && *p <= '9')) { char *e; kv->kind = 1; kv->num = strtod(p, &e); p = e; }
        else { kv->kind = 3; p = mj_skip(p); }
        o->n++;
        p = mj_ws(p);
        if (*p == ',') p++;
        else if (*p == '}') return true;
        else return false;
    }
}

static int mj_find(const mj_obj *o, const char *k) {
    for (int i = 0; i < o->n; i++) if (!strcmp(o->kv[i].key, k)) return i;
    return -1;
}
static const char *mj_str(const mj_obj *o, const char *k) { int i = mj_find(o, k); return i >= 0 && o->kv[i].kind == 0 ? o->kv[i].str : NULL; }
static double mj_num(const mj_obj *o, const char *k, double d) { int i = mj_find(o, k); return i >= 0 && (o->kv[i].kind == 1 || o->kv[i].kind == 2) ? o->kv[i].num : d; }
static bool mj_bool(const mj_obj *o, const char *k, bool d) { int i = mj_find(o, k); return i >= 0 && o->kv[i].kind == 2 ? o->kv[i].num != 0 : d; }
