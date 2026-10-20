// cyd-deck UI (LVGL 8.3). Four pages driven by JSON lines from deckd:
//   MIX   audio mixer: output selector, faders, mutes, level meters
//   DECK  Stream Deck-style button grid with banks (layout comes from deck.json on the Pi)
//   SYS   btop-style CPU graph, per-core bars, memory, disk, temp, network, top processes
//   DOCK  Docker containers with start / stop / restart and recent logs
//
// The protocol is documented in deckd/PROTOCOL.md.

#include "ui.h"
#include "hal.h"
#include <lvgl.h>
#include <ArduinoJson.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define SCR_W 480
#define SCR_H 320
#define TAB_H 34

#define MAX_STRIPS 5
#define MAX_BANKS 6
#define MAX_BTNS 12
#define MAX_PROCS 5
#define MAX_CONT 16
#define CPU_POINTS 60
#define LINK_TIMEOUT_MS 6000
#define FADER_SEND_MS 50

// --------------------------------------------------------------------------------- theme

struct Theme {
  char name[16];
  lv_color_t bg, panel, line, fg, dim, accent, accent2, hi, ok, bad;
  char tabs[4][14];
  char title[32];
};
static Theme T;

static lv_color_t hexc(const char *s, lv_color_t def) {
  if (!s || s[0] != '#' || strlen(s) < 7) return def;
  return lv_color_hex((uint32_t)strtoul(s + 1, NULL, 16));
}

static void theme_default() {
  strcpy(T.name, "neon");
  T.bg = lv_color_hex(0x0a0a12);
  T.panel = lv_color_hex(0x141420);
  T.line = lv_color_hex(0x2a2a3a);
  T.fg = lv_color_hex(0xe6e6f0);
  T.dim = lv_color_hex(0x5a5a78);
  T.accent = lv_color_hex(0xff2a6d);
  T.accent2 = lv_color_hex(0x05d9e8);
  T.hi = lv_color_hex(0xf5b700);
  T.ok = lv_color_hex(0x00ff9f);
  T.bad = lv_color_hex(0xf5b700);
  strcpy(T.tabs[0], "MIX");
  strcpy(T.tabs[1], "DECK");
  strcpy(T.tabs[2], "SYS");
  strcpy(T.tabs[3], "DOCK");
  strcpy(T.title, "NEON//DECK");
}

// --------------------------------------------------------------------------------- widgets

static lv_obj_t *tabview;
static int cur_page = 0;

// Mixer
struct Strip {
  lv_obj_t *box, *val, *meter, *slider, *mute, *mute_lbl, *name;
  char id[32];
  uint32_t last_send;
  bool muted;
};
static Strip strips[MAX_STRIPS];
static lv_obj_t *dd_out, *mix_empty;

// Deck
struct DBtn { lv_obj_t *btn, *icon, *lbl, *tag; uint8_t type; bool on; };
enum { BT_EXEC = 0, BT_TOGGLE = 1, BT_CONFIRM = 2, BT_PAGE = 3 };
static DBtn dbtns[MAX_BTNS];
static lv_obj_t *bank_row, *bank_btns[MAX_BANKS], *deck_grid, *deck_empty;
static int bank_count = 0, cur_bank = 0;
static JsonDocument deck_doc;
static uint8_t toggle_state[MAX_BANKS][MAX_BTNS];

// System
static lv_obj_t *cpu_chart, *cpu_title, *cpu_freq;
static lv_chart_series_t *cpu_ser;
static lv_obj_t *core_bar[4], *core_lbl[4];
static lv_obj_t *mem_bar, *mem_lbl, *disk_bar, *disk_lbl, *temp_lbl, *net_lbl, *up_lbl;
static lv_obj_t *proc_table;
static int proc_pid[MAX_PROCS];
static char proc_name[MAX_PROCS][24];

// Docker
struct CRow { lv_obj_t *row, *dot, *name, *status, *stats; char id[16]; char cname[32]; char state[16]; };
static CRow crows[MAX_CONT];
static lv_obj_t *dock_list, *dock_summary, *dock_err;
static lv_obj_t *dock_detail, *dd_name, *dd_status, *dd_logs;
static char dock_sel_id[16] = "";

// Overlays
static lv_obj_t *link_lbl, *toast_lbl;
static uint32_t last_rx = 0, toast_until = 0;
static bool link_up = false, sys_alert = false, dock_alert = false;

// --------------------------------------------------------------------------------- helpers

static void send_doc(JsonDocument &d) {
  char buf[256];
  size_t n = serializeJson(d, buf, sizeof(buf));
  if (n > 0 && n < sizeof(buf)) hal_send(buf);
}

static lv_obj_t *plain(lv_obj_t *parent) {
  lv_obj_t *o = lv_obj_create(parent);
  lv_obj_remove_style_all(o);
  lv_obj_clear_flag(o, LV_OBJ_FLAG_SCROLLABLE);
  return o;
}

static lv_obj_t *panel(lv_obj_t *parent) {
  lv_obj_t *o = plain(parent);
  lv_obj_set_style_bg_color(o, T.panel, 0);
  lv_obj_set_style_bg_opa(o, LV_OPA_COVER, 0);
  lv_obj_set_style_border_color(o, T.line, 0);
  lv_obj_set_style_border_width(o, 1, 0);
  return o;
}

static lv_obj_t *label(lv_obj_t *parent, const lv_font_t *font, lv_color_t color, const char *text) {
  lv_obj_t *l = lv_label_create(parent);
  lv_obj_set_style_text_font(l, font, 0);
  lv_obj_set_style_text_color(l, color, 0);
  lv_label_set_text(l, text);
  return l;
}

static void style_bar(lv_obj_t *b, lv_color_t ind) {
  lv_obj_set_style_radius(b, 0, 0);
  lv_obj_set_style_radius(b, 0, LV_PART_INDICATOR);
  lv_obj_set_style_bg_color(b, T.line, 0);
  lv_obj_set_style_bg_opa(b, LV_OPA_COVER, 0);
  lv_obj_set_style_bg_color(b, ind, LV_PART_INDICATOR);
  lv_obj_set_style_anim_time(b, 150, 0);
}

static lv_color_t load_color(int pct) {
  if (pct >= 85) return T.bad;
  if (pct >= 60) return T.hi;
  return T.ok;
}

static lv_color_t named_color(const char *n, lv_color_t def) {
  if (!n) return def;
  if (n[0] == '#') return hexc(n, def);
  if (!strcmp(n, "accent")) return T.accent;
  if (!strcmp(n, "accent2")) return T.accent2;
  if (!strcmp(n, "hi")) return T.hi;
  if (!strcmp(n, "ok")) return T.ok;
  if (!strcmp(n, "bad")) return T.bad;
  if (!strcmp(n, "dim")) return T.dim;
  return def;
}

static const char *icon_symbol(const char *n) {
  struct { const char *name; const char *sym; } map[] = {
    {"terminal", LV_SYMBOL_KEYBOARD}, {"keyboard", LV_SYMBOL_KEYBOARD}, {"home", LV_SYMBOL_HOME},
    {"settings", LV_SYMBOL_SETTINGS}, {"power", LV_SYMBOL_POWER}, {"refresh", LV_SYMBOL_REFRESH},
    {"wifi", LV_SYMBOL_WIFI}, {"bluetooth", LV_SYMBOL_BLUETOOTH}, {"audio", LV_SYMBOL_AUDIO},
    {"volume", LV_SYMBOL_VOLUME_MAX}, {"mute", LV_SYMBOL_MUTE}, {"video", LV_SYMBOL_VIDEO},
    {"image", LV_SYMBOL_IMAGE}, {"file", LV_SYMBOL_FILE}, {"folder", LV_SYMBOL_DIRECTORY},
    {"play", LV_SYMBOL_PLAY}, {"pause", LV_SYMBOL_PAUSE}, {"stop", LV_SYMBOL_STOP},
    {"next", LV_SYMBOL_NEXT}, {"prev", LV_SYMBOL_PREV}, {"eye", LV_SYMBOL_EYE_OPEN},
    {"eye_off", LV_SYMBOL_EYE_CLOSE}, {"gps", LV_SYMBOL_GPS}, {"list", LV_SYMBOL_LIST},
    {"bell", LV_SYMBOL_BELL}, {"warning", LV_SYMBOL_WARNING}, {"shuffle", LV_SYMBOL_SHUFFLE},
    {"upload", LV_SYMBOL_UPLOAD}, {"download", LV_SYMBOL_DOWNLOAD}, {"battery", LV_SYMBOL_BATTERY_FULL},
    {"usb", LV_SYMBOL_USB}, {"charge", LV_SYMBOL_CHARGE}, {"edit", LV_SYMBOL_EDIT},
    {"save", LV_SYMBOL_SAVE}, {"copy", LV_SYMBOL_COPY}, {"trash", LV_SYMBOL_TRASH},
    {"loop", LV_SYMBOL_LOOP}, {"call", LV_SYMBOL_CALL}, {"plus", LV_SYMBOL_PLUS},
    {"minus", LV_SYMBOL_MINUS}, {"close", LV_SYMBOL_CLOSE}, {"ok", LV_SYMBOL_OK},
    {"lock", LV_SYMBOL_EYE_CLOSE}, {"drive", LV_SYMBOL_DRIVE}, {"sd", LV_SYMBOL_SD_CARD},
    {"cut", LV_SYMBOL_CUT}, {"paste", LV_SYMBOL_PASTE}, {"up", LV_SYMBOL_UP}, {"down", LV_SYMBOL_DOWN},
    {"left", LV_SYMBOL_LEFT}, {"right", LV_SYMBOL_RIGHT}, {"bars", LV_SYMBOL_BARS},
  };
  if (!n) return "";
  for (auto &m : map)
    if (!strcmp(m.name, n)) return m.sym;
  return n;  // allow a literal short label like "SSH"
}

// LVGL sends CLICKED on release even after LONG_PRESSED; this tells the two apart.
static lv_obj_t *long_pressed_obj = NULL;
static bool is_release_after_hold(lv_event_t *e) {
  lv_event_code_t code = lv_event_get_code(e);
  lv_obj_t *obj = lv_event_get_current_target(e);
  if (code == LV_EVENT_PRESSED) { long_pressed_obj = NULL; return false; }
  if (code == LV_EVENT_LONG_PRESSED) { long_pressed_obj = obj; return false; }
  if (code == LV_EVENT_CLICKED && long_pressed_obj == obj) { long_pressed_obj = NULL; return true; }
  return false;
}

static void toast(const char *msg, lv_color_t color) {
  if (!toast_lbl) return;
  lv_label_set_text(toast_lbl, msg);
  lv_obj_set_style_border_color(toast_lbl, color, 0);
  lv_obj_set_style_text_color(toast_lbl, color, 0);
  lv_obj_clear_flag(toast_lbl, LV_OBJ_FLAG_HIDDEN);
  lv_obj_move_foreground(toast_lbl);
  toast_until = hal_millis() + 2500;
}

// --------------------------------------------------------------------------------- MIX page

static void fader_cb(lv_event_t *e) {
  Strip *s = (Strip *)lv_event_get_user_data(e);
  lv_event_code_t code = lv_event_get_code(e);
  int v = lv_slider_get_value(s->slider);
  lv_label_set_text_fmt(s->val, "%d", v);
  uint32_t now = hal_millis();
  if (code == LV_EVENT_RELEASED || now - s->last_send >= FADER_SEND_MS) {
    s->last_send = now;
    JsonDocument d;
    d["t"] = "vol";
    d["id"] = s->id;
    d["v"] = v;
    send_doc(d);
  }
}

static void mute_cb(lv_event_t *e) {
  Strip *s = (Strip *)lv_event_get_user_data(e);
  JsonDocument d;
  d["t"] = "mute";
  d["id"] = s->id;
  send_doc(d);
}

static void out_cb(lv_event_t *e) {
  JsonDocument d;
  d["t"] = "out";
  d["i"] = lv_dropdown_get_selected(dd_out);
  send_doc(d);
}

static void set_mute_look(Strip *s, bool muted) {
  s->muted = muted;
  lv_obj_set_style_bg_color(s->mute, muted ? T.bad : T.panel, 0);
  lv_obj_set_style_text_color(s->mute_lbl, muted ? T.bg : T.dim, 0);
  lv_label_set_text(s->mute_lbl, muted ? LV_SYMBOL_MUTE " MUTED" : LV_SYMBOL_VOLUME_MAX);
  lv_obj_set_style_bg_color(s->slider, muted ? T.dim : T.accent, LV_PART_INDICATOR);
  lv_obj_set_style_bg_color(s->slider, muted ? T.dim : T.accent, LV_PART_KNOB);
}

static void build_mix(lv_obj_t *page) {
  lv_obj_t *top = plain(page);
  lv_obj_set_size(top, lv_pct(100), 34);
  lv_obj_align(top, LV_ALIGN_TOP_MID, 0, 0);

  label(top, &lv_font_montserrat_12, T.dim, "OUTPUT");
  lv_obj_t *l = lv_obj_get_child(top, 0);
  lv_obj_align(l, LV_ALIGN_LEFT_MID, 4, 0);

  dd_out = lv_dropdown_create(top);
  lv_obj_set_size(dd_out, 300, 32);
  lv_obj_align(dd_out, LV_ALIGN_LEFT_MID, 62, 0);
  lv_dropdown_set_options(dd_out, "waiting for deckd...");
  lv_obj_set_style_bg_color(dd_out, T.panel, 0);
  lv_obj_set_style_border_color(dd_out, T.accent2, 0);
  lv_obj_set_style_text_color(dd_out, T.fg, 0);
  lv_obj_set_style_radius(dd_out, 0, 0);
  lv_obj_t *list = lv_dropdown_get_list(dd_out);
  lv_obj_set_style_bg_color(list, T.panel, 0);
  lv_obj_set_style_text_color(list, T.fg, 0);
  lv_obj_set_style_border_color(list, T.accent2, 0);
  lv_obj_set_style_radius(list, 0, 0);
  lv_obj_set_style_bg_color(list, T.accent, LV_PART_SELECTED | LV_STATE_CHECKED);
  lv_obj_add_event_cb(dd_out, out_cb, LV_EVENT_VALUE_CHANGED, NULL);

  const int sw = 92, sh = 236, gap = 2;
  for (int i = 0; i < MAX_STRIPS; i++) {
    Strip *s = &strips[i];
    memset(s->id, 0, sizeof(s->id));
    s->last_send = 0;
    s->box = panel(page);
    lv_obj_set_size(s->box, sw, sh);
    lv_obj_set_pos(s->box, i * (sw + gap), 40);

    s->val = label(s->box, &lv_font_montserrat_16, T.fg, "--");
    lv_obj_align(s->val, LV_ALIGN_TOP_MID, 0, 4);

    s->meter = lv_bar_create(s->box);
    lv_obj_set_size(s->meter, 8, 136);
    lv_obj_align(s->meter, LV_ALIGN_TOP_MID, 30, 28);
    lv_bar_set_range(s->meter, 0, 100);
    style_bar(s->meter, T.ok);
    lv_obj_set_style_bg_grad_color(s->meter, T.bad, LV_PART_INDICATOR);
    lv_obj_set_style_bg_grad_dir(s->meter, LV_GRAD_DIR_VER, LV_PART_INDICATOR);
    lv_obj_set_style_bg_main_stop(s->meter, 255, LV_PART_INDICATOR);
    lv_obj_set_style_bg_grad_stop(s->meter, 0, LV_PART_INDICATOR);

    s->slider = lv_slider_create(s->box);
    lv_obj_set_size(s->slider, 14, 128);
    lv_obj_align(s->slider, LV_ALIGN_TOP_MID, -6, 32);
    lv_slider_set_range(s->slider, 0, 100);
    lv_obj_set_style_radius(s->slider, 0, 0);
    lv_obj_set_style_radius(s->slider, 0, LV_PART_INDICATOR);
    lv_obj_set_style_radius(s->slider, 2, LV_PART_KNOB);
    lv_obj_set_style_bg_color(s->slider, T.line, 0);
    lv_obj_set_style_bg_opa(s->slider, LV_OPA_COVER, 0);
    lv_obj_set_style_bg_color(s->slider, T.accent, LV_PART_INDICATOR);
    lv_obj_set_style_bg_color(s->slider, T.accent, LV_PART_KNOB);
    lv_obj_set_style_pad_hor(s->slider, 14, LV_PART_KNOB);   // wide knob for resistive touch
    lv_obj_set_style_pad_ver(s->slider, 5, LV_PART_KNOB);
    lv_obj_set_style_border_color(s->slider, T.fg, LV_PART_KNOB);
    lv_obj_set_style_border_width(s->slider, 2, LV_PART_KNOB);
    lv_obj_set_ext_click_area(s->slider, 22);
    lv_obj_add_event_cb(s->slider, fader_cb, LV_EVENT_VALUE_CHANGED, s);
    lv_obj_add_event_cb(s->slider, fader_cb, LV_EVENT_RELEASED, s);

    s->mute = lv_btn_create(s->box);
    lv_obj_set_size(s->mute, sw - 12, 34);
    lv_obj_align(s->mute, LV_ALIGN_TOP_MID, 0, 172);
    lv_obj_set_style_radius(s->mute, 0, 0);
    lv_obj_set_style_shadow_width(s->mute, 0, 0);
    lv_obj_set_style_border_color(s->mute, T.line, 0);
    lv_obj_set_style_border_width(s->mute, 1, 0);
    s->mute_lbl = label(s->mute, &lv_font_montserrat_12, T.dim, LV_SYMBOL_VOLUME_MAX);
    lv_obj_center(s->mute_lbl);
    lv_obj_add_event_cb(s->mute, mute_cb, LV_EVENT_CLICKED, s);
    set_mute_look(s, false);

    s->name = label(s->box, &lv_font_montserrat_12, T.hi, "");
    lv_label_set_long_mode(s->name, LV_LABEL_LONG_DOT);
    lv_obj_set_width(s->name, sw - 8);
    lv_obj_set_style_text_align(s->name, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_align(s->name, LV_ALIGN_BOTTOM_MID, 0, -6);

    lv_obj_add_flag(s->box, LV_OBJ_FLAG_HIDDEN);
  }
  mix_empty = label(page, &lv_font_montserrat_14, T.dim, "No audio devices reported yet.");
  lv_obj_align(mix_empty, LV_ALIGN_CENTER, 0, 10);
}

static void on_mix(JsonDocument &d) {
  JsonArray outs = d["out"].as<JsonArray>();
  if (!outs.isNull()) {
    char opts[512] = "";
    size_t used = 0;
    for (JsonVariant o : outs) {
      const char *n = o.as<const char *>();
      if (!n) continue;
      size_t need = strlen(n) + 1;
      if (used + need >= sizeof(opts)) break;
      if (used) { opts[used++] = '\n'; opts[used] = 0; }
      strcat(opts, n);
      used += strlen(n);
    }
    if (!lv_dropdown_is_open(dd_out)) {
      char cur[512];
      strncpy(cur, lv_dropdown_get_options(dd_out), sizeof(cur) - 1);
      cur[sizeof(cur) - 1] = 0;
      if (strcmp(cur, opts) != 0) lv_dropdown_set_options(dd_out, opts[0] ? opts : "(none)");
      lv_dropdown_set_selected(dd_out, d["out_sel"] | 0);
    }
  }
  JsonArray arr = d["strips"].as<JsonArray>();
  int i = 0;
  for (JsonObject st : arr) {
    if (i >= MAX_STRIPS) break;
    Strip *s = &strips[i++];
    strncpy(s->id, st["id"] | "", sizeof(s->id) - 1);
    lv_label_set_text(s->name, st["name"] | "?");
    lv_obj_clear_flag(s->box, LV_OBJ_FLAG_HIDDEN);
    if (!lv_obj_has_state(s->slider, LV_STATE_PRESSED)) {
      int v = st["vol"] | 0;
      lv_slider_set_value(s->slider, v, LV_ANIM_OFF);
      lv_label_set_text_fmt(s->val, "%d", v);
    }
    set_mute_look(s, st["mute"] | false);
  }
  for (; i < MAX_STRIPS; i++) {
    lv_obj_add_flag(strips[i].box, LV_OBJ_FLAG_HIDDEN);
    strips[i].id[0] = 0;
  }
  if (arr.size() > 0) lv_obj_add_flag(mix_empty, LV_OBJ_FLAG_HIDDEN);
  else lv_obj_clear_flag(mix_empty, LV_OBJ_FLAG_HIDDEN);
}

static void on_levels(JsonDocument &d) {
  JsonArray v = d["v"].as<JsonArray>();
  int i = 0;
  for (JsonVariant x : v) {
    if (i >= MAX_STRIPS) break;
    lv_bar_set_value(strips[i++].meter, x.as<int>(), LV_ANIM_ON);
  }
}

// --------------------------------------------------------------------------------- DECK page

static void deck_show_bank(int bank);

static void deck_btn_cb(lv_event_t *e) {
  int idx = (int)(intptr_t)lv_event_get_user_data(e);
  lv_event_code_t code = lv_event_get_code(e);
  DBtn *b = &dbtns[idx];
  if (is_release_after_hold(e) || code == LV_EVENT_PRESSED) return;
  if (b->type == BT_CONFIRM && code != LV_EVENT_LONG_PRESSED) {
    if (code == LV_EVENT_CLICKED) toast("Hold to confirm", T.hi);
    return;
  }
  if (b->type != BT_CONFIRM && code != LV_EVENT_CLICKED) return;

  if (b->type == BT_PAGE) {
    JsonObject btn = deck_doc["banks"][cur_bank]["buttons"][idx];
    if (btn["page"].is<int>()) {
      lv_tabview_set_act(tabview, btn["page"].as<int>(), LV_ANIM_OFF);
      lv_event_send(tabview, LV_EVENT_VALUE_CHANGED, NULL);
    } else if (btn["bank"].is<int>()) {
      deck_show_bank(btn["bank"].as<int>());
      JsonDocument d;
      d["t"] = "bank";
      d["b"] = cur_bank;
      send_doc(d);
    }
    return;
  }
  JsonDocument d;
  d["t"] = "btn";
  d["bank"] = cur_bank;
  d["i"] = idx;
  send_doc(d);
  if (b->type == BT_CONFIRM) toast("Confirmed", T.bad);
}

static void bank_cb(lv_event_t *e) {
  int bank = (int)(intptr_t)lv_event_get_user_data(e);
  deck_show_bank(bank);
  JsonDocument d;
  d["t"] = "bank";
  d["b"] = bank;
  send_doc(d);
}

static void paint_deck_btn(DBtn *b, lv_color_t color) {
  bool on = b->type == BT_TOGGLE && b->on;
  lv_obj_set_style_bg_color(b->btn, on ? color : T.panel, 0);
  lv_obj_set_style_border_color(b->btn, color, 0);
  lv_obj_set_style_text_color(b->icon, on ? T.bg : color, 0);
  lv_obj_set_style_text_color(b->lbl, on ? T.bg : T.fg, 0);
}

static void deck_show_bank(int bank) {
  JsonArray banks = deck_doc["banks"].as<JsonArray>();
  if (banks.isNull() || banks.size() == 0) return;
  if (bank < 0 || bank >= (int)banks.size()) bank = 0;
  cur_bank = bank;
  for (int i = 0; i < bank_count; i++) {
    bool act = i == cur_bank;
    lv_obj_set_style_bg_color(bank_btns[i], act ? T.accent : T.panel, 0);
    lv_obj_set_style_text_color(lv_obj_get_child(bank_btns[i], 0), act ? T.bg : T.dim, 0);
  }
  JsonArray btns = banks[bank]["buttons"].as<JsonArray>();
  for (int i = 0; i < MAX_BTNS; i++) {
    DBtn *b = &dbtns[i];
    JsonObject bo = i < (int)btns.size() ? btns[i].as<JsonObject>() : JsonObject();
    if (bo.isNull() || bo["label"].isNull()) {
      lv_obj_add_flag(b->btn, LV_OBJ_FLAG_HIDDEN);
      continue;
    }
    lv_obj_clear_flag(b->btn, LV_OBJ_FLAG_HIDDEN);
    const char *type = bo["type"] | "exec";
    b->type = !strcmp(type, "toggle") ? BT_TOGGLE : !strcmp(type, "confirm") ? BT_CONFIRM
            : !strcmp(type, "page") ? BT_PAGE : BT_EXEC;
    b->on = toggle_state[bank][i];
    lv_label_set_text(b->icon, icon_symbol(bo["icon"] | ""));
    lv_label_set_text(b->lbl, bo["label"] | "");
    if (b->type == BT_CONFIRM) {
      lv_label_set_text(b->tag, "HOLD");
      lv_obj_clear_flag(b->tag, LV_OBJ_FLAG_HIDDEN);
    } else if (b->type == BT_TOGGLE) {
      lv_label_set_text(b->tag, b->on ? "ON" : "OFF");
      lv_obj_clear_flag(b->tag, LV_OBJ_FLAG_HIDDEN);
    } else {
      lv_obj_add_flag(b->tag, LV_OBJ_FLAG_HIDDEN);
    }
    lv_color_t def = b->type == BT_CONFIRM ? T.bad : b->type == BT_TOGGLE ? T.accent2 : T.accent;
    lv_color_t c = named_color(bo["color"] | (const char *)NULL, def);
    lv_obj_set_style_text_color(b->tag, b->on ? T.bg : c, 0);
    paint_deck_btn(b, c);
  }
}

static void build_deck(lv_obj_t *page) {
  bank_row = plain(page);
  lv_obj_set_size(bank_row, lv_pct(100), 30);
  lv_obj_align(bank_row, LV_ALIGN_TOP_LEFT, 0, 0);
  lv_obj_set_flex_flow(bank_row, LV_FLEX_FLOW_ROW);
  lv_obj_set_style_pad_column(bank_row, 4, 0);
  for (int i = 0; i < MAX_BANKS; i++) {
    lv_obj_t *b = lv_btn_create(bank_row);
    lv_obj_set_height(b, 28);
    lv_obj_set_style_pad_hor(b, 12, 0);
    lv_obj_set_style_radius(b, 0, 0);
    lv_obj_set_style_shadow_width(b, 0, 0);
    lv_obj_set_style_bg_color(b, T.panel, 0);
    lv_obj_set_style_border_color(b, T.line, 0);
    lv_obj_set_style_border_width(b, 1, 0);
    label(b, &lv_font_montserrat_12, T.dim, "");
    lv_obj_center(lv_obj_get_child(b, 0));
    lv_obj_add_event_cb(b, bank_cb, LV_EVENT_CLICKED, (void *)(intptr_t)i);
    lv_obj_add_flag(b, LV_OBJ_FLAG_HIDDEN);
    bank_btns[i] = b;
  }

  deck_grid = plain(page);
  lv_obj_set_size(deck_grid, lv_pct(100), 240);
  lv_obj_align(deck_grid, LV_ALIGN_TOP_LEFT, 0, 36);
  const int cols = 4, bw = 114, bh = 76, gx = 6, gy = 6;
  for (int i = 0; i < MAX_BTNS; i++) {
    DBtn *b = &dbtns[i];
    b->btn = lv_btn_create(deck_grid);
    lv_obj_set_size(b->btn, bw, bh);
    lv_obj_set_pos(b->btn, (i % cols) * (bw + gx), (i / cols) * (bh + gy));
    lv_obj_set_style_radius(b->btn, 0, 0);
    lv_obj_set_style_shadow_width(b->btn, 0, 0);
    lv_obj_set_style_border_width(b->btn, 2, 0);
    lv_obj_set_style_bg_color(b->btn, T.panel, 0);
    lv_obj_set_style_bg_color(b->btn, T.accent2, LV_STATE_PRESSED);
    b->icon = label(b->btn, &lv_font_montserrat_20, T.accent, "");
    lv_obj_align(b->icon, LV_ALIGN_TOP_LEFT, 0, 0);
    b->lbl = label(b->btn, &lv_font_montserrat_14, T.fg, "");
    lv_label_set_long_mode(b->lbl, LV_LABEL_LONG_WRAP);
    lv_obj_set_width(b->lbl, bw - 16);
    lv_obj_align(b->lbl, LV_ALIGN_BOTTOM_LEFT, 0, 0);
    b->tag = label(b->btn, &lv_font_montserrat_10, T.dim, "");
    lv_obj_align(b->tag, LV_ALIGN_TOP_RIGHT, 0, 0);
    lv_obj_add_event_cb(b->btn, deck_btn_cb, LV_EVENT_CLICKED, (void *)(intptr_t)i);
    lv_obj_add_event_cb(b->btn, deck_btn_cb, LV_EVENT_LONG_PRESSED, (void *)(intptr_t)i);
    lv_obj_add_event_cb(b->btn, deck_btn_cb, LV_EVENT_PRESSED, (void *)(intptr_t)i);
    lv_obj_add_flag(b->btn, LV_OBJ_FLAG_HIDDEN);
  }
  deck_empty = label(page, &lv_font_montserrat_14, T.dim, "Waiting for deck.json from deckd...");
  lv_obj_align(deck_empty, LV_ALIGN_CENTER, 0, 0);
}

static void on_deck(JsonDocument &d) {
  deck_doc.clear();
  deck_doc.set(d);
  memset(toggle_state, 0, sizeof(toggle_state));
  JsonArray banks = deck_doc["banks"].as<JsonArray>();
  bank_count = 0;
  for (JsonObject bk : banks) {
    if (bank_count >= MAX_BANKS) break;
    lv_obj_t *b = bank_btns[bank_count];
    lv_label_set_text(lv_obj_get_child(b, 0), bk["name"] | "BANK");
    lv_obj_clear_flag(b, LV_OBJ_FLAG_HIDDEN);
    bank_count++;
  }
  for (int i = bank_count; i < MAX_BANKS; i++) lv_obj_add_flag(bank_btns[i], LV_OBJ_FLAG_HIDDEN);
  if (bank_count) lv_obj_add_flag(deck_empty, LV_OBJ_FLAG_HIDDEN);
  deck_show_bank(cur_bank < bank_count ? cur_bank : 0);
}

static void on_dstate(JsonDocument &d) {
  int bank = d["bank"] | 0;
  if (bank < 0 || bank >= MAX_BANKS) return;
  int i = 0;
  for (JsonVariant v : d["on"].as<JsonArray>()) {
    if (i >= MAX_BTNS) break;
    toggle_state[bank][i++] = v.as<bool>() ? 1 : 0;
  }
  if (bank == cur_bank) deck_show_bank(cur_bank);
}

// --------------------------------------------------------------------------------- SYS page

static void chart_fill_cb(lv_event_t *e) {
  lv_obj_t *obj = lv_event_get_target(e);
  lv_obj_draw_part_dsc_t *dsc = lv_event_get_draw_part_dsc(e);
  if (dsc->part != LV_PART_ITEMS || !dsc->p1 || !dsc->p2) return;
  lv_draw_mask_line_param_t line_mask;
  lv_draw_mask_line_points_init(&line_mask, dsc->p1->x, dsc->p1->y, dsc->p2->x, dsc->p2->y,
                                LV_DRAW_MASK_LINE_SIDE_BOTTOM);
  int16_t line_id = lv_draw_mask_add(&line_mask, NULL);
  lv_draw_rect_dsc_t rd;
  lv_draw_rect_dsc_init(&rd);
  rd.bg_opa = LV_OPA_70;
  rd.bg_grad.dir = LV_GRAD_DIR_VER;
  rd.bg_grad.stops_count = 2;
  rd.bg_grad.stops[0].color = T.bad;
  rd.bg_grad.stops[0].frac = 0;
  rd.bg_grad.stops[1].color = T.ok;
  rd.bg_grad.stops[1].frac = 200;
  lv_area_t a;
  a.x1 = dsc->p1->x;
  a.x2 = dsc->p2->x - 1;
  a.y1 = obj->coords.y1;   // gradient spans the whole chart so color tracks load
  a.y2 = obj->coords.y2;
  lv_draw_rect(dsc->draw_ctx, &rd, &a);
  lv_draw_mask_free_param(&line_mask);
  lv_draw_mask_remove_id(line_id);
}

static void proc_cb(lv_event_t *e);

static void build_sys(lv_obj_t *page) {
  // CPU graph
  lv_obj_t *cpu = panel(page);
  lv_obj_set_size(cpu, 300, 132);
  lv_obj_set_pos(cpu, 0, 0);
  cpu_title = label(cpu, &lv_font_montserrat_14, T.accent2, "CPU --%");
  lv_obj_align(cpu_title, LV_ALIGN_TOP_LEFT, 6, 3);
  cpu_freq = label(cpu, &lv_font_montserrat_12, T.dim, "");
  lv_obj_align(cpu_freq, LV_ALIGN_TOP_RIGHT, -6, 4);

  cpu_chart = lv_chart_create(cpu);
  lv_obj_set_size(cpu_chart, 290, 104);
  lv_obj_align(cpu_chart, LV_ALIGN_BOTTOM_MID, 0, -3);
  lv_chart_set_type(cpu_chart, LV_CHART_TYPE_LINE);
  lv_chart_set_point_count(cpu_chart, CPU_POINTS);
  lv_chart_set_range(cpu_chart, LV_CHART_AXIS_PRIMARY_Y, 0, 100);
  lv_chart_set_update_mode(cpu_chart, LV_CHART_UPDATE_MODE_SHIFT);
  lv_chart_set_div_line_count(cpu_chart, 3, 0);
  lv_obj_set_style_bg_opa(cpu_chart, LV_OPA_TRANSP, 0);
  lv_obj_set_style_border_width(cpu_chart, 0, 0);
  lv_obj_set_style_pad_all(cpu_chart, 0, 0);
  lv_obj_set_style_line_color(cpu_chart, T.line, LV_PART_MAIN);
  lv_obj_set_style_size(cpu_chart, 0, LV_PART_INDICATOR);
  lv_obj_set_style_line_width(cpu_chart, 2, LV_PART_ITEMS);
  cpu_ser = lv_chart_add_series(cpu_chart, T.ok, LV_CHART_AXIS_PRIMARY_Y);
  lv_chart_set_all_value(cpu_chart, cpu_ser, 0);
  lv_obj_add_event_cb(cpu_chart, chart_fill_cb, LV_EVENT_DRAW_PART_BEGIN, NULL);

  // Cores
  lv_obj_t *cores = panel(page);
  lv_obj_set_size(cores, 170, 132);
  lv_obj_set_pos(cores, 304, 0);
  label(cores, &lv_font_montserrat_12, T.dim, "CORES");
  lv_obj_align(lv_obj_get_child(cores, 0), LV_ALIGN_TOP_LEFT, 6, 4);
  for (int i = 0; i < 4; i++) {
    char n[4];
    snprintf(n, sizeof(n), "C%d", i);
    lv_obj_t *cl = label(cores, &lv_font_unscii_8, T.fg, n);
    lv_obj_set_pos(cl, 6, 28 + i * 24);
    core_bar[i] = lv_bar_create(cores);
    lv_obj_set_size(core_bar[i], 96, 10);
    lv_obj_set_pos(core_bar[i], 28, 27 + i * 24);
    lv_bar_set_range(core_bar[i], 0, 100);
    style_bar(core_bar[i], T.ok);
    core_lbl[i] = label(cores, &lv_font_unscii_8, T.fg, "--%");
    lv_obj_set_pos(core_lbl[i], 130, 28 + i * 24);
  }

  // Memory, disk, temp, network
  lv_obj_t *mid = panel(page);
  lv_obj_set_size(mid, 474, 50);
  lv_obj_set_pos(mid, 0, 136);
  mem_lbl = label(mid, &lv_font_montserrat_12, T.fg, "MEM --");
  lv_obj_set_pos(mem_lbl, 6, 4);
  mem_bar = lv_bar_create(mid);
  lv_obj_set_size(mem_bar, 140, 10);
  lv_obj_set_pos(mem_bar, 6, 26);
  lv_bar_set_range(mem_bar, 0, 100);
  style_bar(mem_bar, T.accent);

  disk_lbl = label(mid, &lv_font_montserrat_12, T.fg, "DISK --");
  lv_obj_set_pos(disk_lbl, 160, 4);
  disk_bar = lv_bar_create(mid);
  lv_obj_set_size(disk_bar, 120, 10);
  lv_obj_set_pos(disk_bar, 160, 26);
  lv_bar_set_range(disk_bar, 0, 100);
  style_bar(disk_bar, T.accent2);

  temp_lbl = label(mid, &lv_font_montserrat_20, T.hi, "--°C");
  lv_obj_set_pos(temp_lbl, 296, 12);
  net_lbl = label(mid, &lv_font_unscii_8, T.fg, LV_SYMBOL_UP " --\n" LV_SYMBOL_DOWN " --");
  lv_obj_set_style_text_font(net_lbl, &lv_font_montserrat_12, 0);
  lv_obj_set_pos(net_lbl, 374, 4);

  // Processes
  proc_table = lv_table_create(page);
  lv_obj_set_size(proc_table, 474, 92);
  lv_obj_set_pos(proc_table, 0, 190);
  lv_table_set_col_cnt(proc_table, 4);
  lv_table_set_row_cnt(proc_table, MAX_PROCS + 1);
  lv_table_set_col_width(proc_table, 0, 70);
  lv_table_set_col_width(proc_table, 1, 236);
  lv_table_set_col_width(proc_table, 2, 84);
  lv_table_set_col_width(proc_table, 3, 82);
  lv_obj_set_style_bg_color(proc_table, T.panel, 0);
  lv_obj_set_style_border_color(proc_table, T.line, 0);
  lv_obj_set_style_radius(proc_table, 0, 0);
  lv_obj_set_style_pad_all(proc_table, 0, 0);
  lv_obj_set_style_bg_opa(proc_table, LV_OPA_TRANSP, LV_PART_ITEMS);
  lv_obj_set_style_border_width(proc_table, 0, LV_PART_ITEMS);
  lv_obj_set_style_text_color(proc_table, T.fg, LV_PART_ITEMS);
  lv_obj_set_style_text_font(proc_table, &lv_font_unscii_8, LV_PART_ITEMS);
  lv_obj_set_style_pad_ver(proc_table, 2, LV_PART_ITEMS);
  lv_obj_set_scrollbar_mode(proc_table, LV_SCROLLBAR_MODE_OFF);
  lv_obj_clear_flag(proc_table, LV_OBJ_FLAG_SCROLLABLE);
  lv_obj_set_style_pad_hor(proc_table, 6, LV_PART_ITEMS);
  lv_obj_set_style_bg_color(proc_table, T.accent, LV_PART_ITEMS | LV_STATE_PRESSED);
  lv_obj_set_style_bg_opa(proc_table, LV_OPA_COVER, LV_PART_ITEMS | LV_STATE_PRESSED);
  lv_table_set_cell_value(proc_table, 0, 0, "PID");
  lv_table_set_cell_value(proc_table, 0, 1, "PROGRAM");
  lv_table_set_cell_value(proc_table, 0, 2, "CPU%");
  lv_table_set_cell_value(proc_table, 0, 3, "MEM%");
  lv_obj_add_event_cb(proc_table, proc_cb, LV_EVENT_VALUE_CHANGED, NULL);
  for (int i = 0; i < MAX_PROCS; i++) proc_pid[i] = 0;
}

static void kill_box_cb(lv_event_t *e) {
  lv_obj_t *mbox = lv_event_get_current_target(e);
  const char *txt = lv_msgbox_get_active_btn_text(mbox);
  int pid = (int)(intptr_t)lv_event_get_user_data(e);
  if (txt && !strcmp(txt, "KILL")) {
    JsonDocument d;
    d["t"] = "kill";
    d["pid"] = pid;
    send_doc(d);
  }
  lv_msgbox_close(mbox);
}

static void proc_cb(lv_event_t *e) {
  uint16_t row, col;
  lv_table_get_selected_cell(proc_table, &row, &col);
  if (row == 0 || row > MAX_PROCS || proc_pid[row - 1] == 0) return;
  static const char *btns[] = {"KILL", "CANCEL", ""};
  char msg[64];
  snprintf(msg, sizeof(msg), "Send SIGTERM to %s (%d)?", proc_name[row - 1], proc_pid[row - 1]);
  lv_obj_t *m = lv_msgbox_create(NULL, "END PROCESS", msg, btns, false);
  lv_obj_set_style_bg_color(m, T.panel, 0);
  lv_obj_set_style_border_color(m, T.bad, 0);
  lv_obj_set_style_border_width(m, 2, 0);
  lv_obj_set_style_radius(m, 0, 0);
  lv_obj_set_style_text_color(m, T.fg, 0);
  lv_obj_t *bm = lv_msgbox_get_btns(m);
  lv_obj_set_style_bg_color(bm, T.bad, LV_PART_ITEMS);
  lv_obj_set_style_text_color(bm, T.bg, LV_PART_ITEMS);
  lv_obj_set_style_radius(bm, 0, LV_PART_ITEMS);
  lv_obj_set_size(bm, 260, 40);
  lv_obj_center(m);
  lv_obj_add_event_cb(m, kill_box_cb, LV_EVENT_VALUE_CHANGED, (void *)(intptr_t)proc_pid[row - 1]);
}

static void on_sys(JsonDocument &d) {
  int cpu = d["cpu"] | 0;
  lv_chart_set_next_value(cpu_chart, cpu_ser, cpu);
  lv_label_set_text_fmt(cpu_title, "CPU %d%%", cpu);
  lv_obj_set_style_text_color(cpu_title, load_color(cpu), 0);
  lv_label_set_text_fmt(cpu_freq, "%d MHz  up %s", d["freq"] | 0, d["uptime"] | "--");
  int i = 0;
  for (JsonVariant c : d["cores"].as<JsonArray>()) {
    if (i >= 4) break;
    int v = c.as<int>();
    lv_bar_set_value(core_bar[i], v, LV_ANIM_ON);
    lv_obj_set_style_bg_color(core_bar[i], load_color(v), LV_PART_INDICATOR);
    lv_label_set_text_fmt(core_lbl[i], "%3d%%", v);
    i++;
  }
  int mem = d["mem"] | 0, disk = d["disk"] | 0;
  lv_bar_set_value(mem_bar, mem, LV_ANIM_ON);
  lv_label_set_text_fmt(mem_lbl, "MEM %d%%  %s/%s", mem, d["mem_used"] | "?", d["mem_total"] | "?");
  lv_bar_set_value(disk_bar, disk, LV_ANIM_ON);
  lv_label_set_text_fmt(disk_lbl, "DISK %d%%", disk);
  float temp = d["temp"] | 0.0f;
  bool thr = d["throttled"] | false;
  lv_label_set_text_fmt(temp_lbl, "%d°C%s", (int)(temp + 0.5f), thr ? " " LV_SYMBOL_WARNING : "");
  lv_obj_set_style_text_color(temp_lbl, (temp >= 75 || thr) ? T.bad : temp >= 60 ? T.hi : T.ok, 0);
  lv_label_set_text_fmt(net_lbl, LV_SYMBOL_UP " %s/s\n" LV_SYMBOL_DOWN " %s/s", d["up"] | "--", d["down"] | "--");
  sys_alert = (temp >= 75 || thr);
}

static void on_procs(JsonDocument &d) {
  int i = 0;
  char buf[16];
  for (JsonObject p : d["p"].as<JsonArray>()) {
    if (i >= MAX_PROCS) break;
    proc_pid[i] = p["pid"] | 0;
    strncpy(proc_name[i], p["name"] | "?", sizeof(proc_name[i]) - 1);
    proc_name[i][sizeof(proc_name[i]) - 1] = 0;
    snprintf(buf, sizeof(buf), "%d", proc_pid[i]);
    lv_table_set_cell_value(proc_table, i + 1, 0, buf);
    lv_table_set_cell_value(proc_table, i + 1, 1, proc_name[i]);
    snprintf(buf, sizeof(buf), "%5.1f", (double)(p["cpu"] | 0.0f));
    lv_table_set_cell_value(proc_table, i + 1, 2, buf);
    snprintf(buf, sizeof(buf), "%5.1f", (double)(p["mem"] | 0.0f));
    lv_table_set_cell_value(proc_table, i + 1, 3, buf);
    i++;
  }
  for (; i < MAX_PROCS; i++) {
    proc_pid[i] = 0;
    for (int c = 0; c < 4; c++) lv_table_set_cell_value(proc_table, i + 1, c, "");
  }
}

// --------------------------------------------------------------------------------- DOCK page

static lv_color_t state_color(const char *state, const char *health) {
  if (health && !strcmp(health, "unhealthy")) return T.bad;
  if (!strcmp(state, "restarting") || !strcmp(state, "dead")) return T.bad;
  if (!strcmp(state, "running")) return T.ok;
  if (!strcmp(state, "paused")) return T.hi;
  return T.dim;  // exited or created: stopped on purpose, not an alarm
}

static void dock_action(const char *action) {
  if (!dock_sel_id[0]) return;
  JsonDocument d;
  d["t"] = "dact";
  d["id"] = dock_sel_id;
  d["a"] = action;
  send_doc(d);
}

static void dact_cb(lv_event_t *e) {
  const char *a = (const char *)lv_event_get_user_data(e);
  lv_event_code_t code = lv_event_get_code(e);
  if (is_release_after_hold(e) || code == LV_EVENT_PRESSED) return;
  bool needs_hold = strcmp(a, "start") != 0;
  if (needs_hold && code == LV_EVENT_CLICKED) { toast("Hold to confirm", T.hi); return; }
  if (!needs_hold && code != LV_EVENT_CLICKED) return;
  dock_action(a);
  char m[48];
  snprintf(m, sizeof(m), "%s sent", a);
  toast(m, T.accent2);
}

static void dock_back_cb(lv_event_t *e) {
  lv_obj_add_flag(dock_detail, LV_OBJ_FLAG_HIDDEN);
  dock_sel_id[0] = 0;
  JsonDocument d;
  d["t"] = "dsel";
  d["id"] = "";
  send_doc(d);
}

static void crow_cb(lv_event_t *e) {
  CRow *r = (CRow *)lv_event_get_user_data(e);
  strncpy(dock_sel_id, r->id, sizeof(dock_sel_id) - 1);
  lv_label_set_text(dd_name, r->cname);
  lv_label_set_text(dd_status, lv_label_get_text(r->status));
  lv_label_set_text(dd_logs, "loading logs...");
  lv_obj_clear_flag(dock_detail, LV_OBJ_FLAG_HIDDEN);
  JsonDocument d;
  d["t"] = "dsel";
  d["id"] = r->id;
  send_doc(d);
}

static lv_obj_t *action_btn(lv_obj_t *parent, const char *text, lv_color_t c, const char *act) {
  lv_obj_t *b = lv_btn_create(parent);
  lv_obj_set_size(b, 100, 36);
  lv_obj_set_style_radius(b, 0, 0);
  lv_obj_set_style_shadow_width(b, 0, 0);
  lv_obj_set_style_bg_color(b, T.panel, 0);
  lv_obj_set_style_bg_color(b, c, LV_STATE_PRESSED);
  lv_obj_set_style_border_color(b, c, 0);
  lv_obj_set_style_border_width(b, 2, 0);
  lv_obj_t *l = label(b, &lv_font_montserrat_12, c, text);
  lv_obj_center(l);
  if (act) {
    lv_obj_add_event_cb(b, dact_cb, LV_EVENT_CLICKED, (void *)act);
    lv_obj_add_event_cb(b, dact_cb, LV_EVENT_LONG_PRESSED, (void *)act);
    lv_obj_add_event_cb(b, dact_cb, LV_EVENT_PRESSED, (void *)act);
  }
  return b;
}

static void build_dock(lv_obj_t *page) {
  dock_summary = label(page, &lv_font_montserrat_14, T.accent2, "DOCKER  --");
  lv_obj_set_pos(dock_summary, 4, 4);

  dock_list = plain(page);
  lv_obj_set_size(dock_list, 474, 252);
  lv_obj_set_pos(dock_list, 0, 28);
  lv_obj_add_flag(dock_list, LV_OBJ_FLAG_SCROLLABLE);
  lv_obj_set_flex_flow(dock_list, LV_FLEX_FLOW_COLUMN);
  lv_obj_set_style_pad_row(dock_list, 4, 0);
  lv_obj_set_scrollbar_mode(dock_list, LV_SCROLLBAR_MODE_ACTIVE);

  for (int i = 0; i < MAX_CONT; i++) {
    CRow *r = &crows[i];
    r->id[0] = 0;
    r->row = panel(dock_list);
    lv_obj_set_size(r->row, 468, 44);
    lv_obj_add_flag(r->row, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_set_style_bg_color(r->row, T.line, LV_STATE_PRESSED);
    r->dot = plain(r->row);
    lv_obj_set_size(r->dot, 12, 12);
    lv_obj_set_style_radius(r->dot, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_bg_opa(r->dot, LV_OPA_COVER, 0);
    lv_obj_align(r->dot, LV_ALIGN_LEFT_MID, 10, 0);
    r->name = label(r->row, &lv_font_montserrat_14, T.fg, "");
    lv_label_set_long_mode(r->name, LV_LABEL_LONG_DOT);
    lv_obj_set_width(r->name, 190);
    lv_obj_align(r->name, LV_ALIGN_TOP_LEFT, 30, 4);
    r->status = label(r->row, &lv_font_montserrat_10, T.dim, "");
    lv_label_set_long_mode(r->status, LV_LABEL_LONG_DOT);
    lv_obj_set_width(r->status, 190);
    lv_obj_align(r->status, LV_ALIGN_BOTTOM_LEFT, 30, -4);
    r->stats = label(r->row, &lv_font_unscii_8, T.fg, "");
    lv_obj_set_style_text_align(r->stats, LV_TEXT_ALIGN_RIGHT, 0);
    lv_obj_align(r->stats, LV_ALIGN_RIGHT_MID, -10, 0);
    lv_obj_add_event_cb(r->row, crow_cb, LV_EVENT_CLICKED, r);
    lv_obj_add_flag(r->row, LV_OBJ_FLAG_HIDDEN);
  }
  dock_err = label(page, &lv_font_montserrat_14, T.dim, "Waiting for container list...");
  lv_label_set_long_mode(dock_err, LV_LABEL_LONG_WRAP);
  lv_obj_set_width(dock_err, 440);
  lv_obj_set_style_text_align(dock_err, LV_TEXT_ALIGN_CENTER, 0);
  lv_obj_align(dock_err, LV_ALIGN_CENTER, 0, 10);

  // Detail view (covers the list)
  dock_detail = panel(page);
  lv_obj_set_size(dock_detail, 474, 282);
  lv_obj_set_pos(dock_detail, 0, 0);
  lv_obj_set_style_bg_color(dock_detail, T.bg, 0);
  dd_name = label(dock_detail, &lv_font_montserrat_20, T.accent2, "");
  lv_obj_set_pos(dd_name, 8, 6);
  dd_status = label(dock_detail, &lv_font_montserrat_12, T.dim, "");
  lv_obj_set_pos(dd_status, 8, 34);

  lv_obj_t *back = action_btn(dock_detail, LV_SYMBOL_LEFT " BACK", T.dim, NULL);
  lv_obj_set_size(back, 90, 32);
  lv_obj_align(back, LV_ALIGN_TOP_RIGHT, -6, 6);
  lv_obj_add_event_cb(back, dock_back_cb, LV_EVENT_CLICKED, NULL);

  lv_obj_t *b1 = action_btn(dock_detail, LV_SYMBOL_PLAY " START", T.ok, "start");
  lv_obj_set_pos(b1, 8, 56);
  lv_obj_t *b2 = action_btn(dock_detail, LV_SYMBOL_STOP " STOP", T.bad, "stop");
  lv_obj_set_pos(b2, 114, 56);
  lv_obj_t *b3 = action_btn(dock_detail, LV_SYMBOL_REFRESH " RESTART", T.hi, "restart");
  lv_obj_set_pos(b3, 220, 56);
  lv_obj_t *hint = label(dock_detail, &lv_font_montserrat_10, T.dim, "Stop and restart:\nhold to confirm");
  lv_obj_set_pos(hint, 330, 60);

  lv_obj_t *logbox = panel(dock_detail);
  lv_obj_set_size(logbox, 458, 176);
  lv_obj_set_pos(logbox, 8, 100);
  lv_obj_add_flag(logbox, LV_OBJ_FLAG_SCROLLABLE);
  dd_logs = label(logbox, &lv_font_unscii_8, T.fg, "");
  lv_label_set_long_mode(dd_logs, LV_LABEL_LONG_WRAP);
  lv_obj_set_width(dd_logs, 446);
  lv_obj_set_pos(dd_logs, 6, 6);
  lv_obj_add_flag(dock_detail, LV_OBJ_FLAG_HIDDEN);
}

static void on_dock(JsonDocument &d) {
  bool ok = d["ok"] | false;
  JsonArray c = d["c"].as<JsonArray>();
  int running = 0, stopped = 0, i = 0;
  bool bad = false;
  for (JsonObject o : c) {
    if (i >= MAX_CONT) break;
    CRow *r = &crows[i++];
    strncpy(r->id, o["id"] | "", sizeof(r->id) - 1);
    strncpy(r->cname, o["name"] | "?", sizeof(r->cname) - 1);
    strncpy(r->state, o["state"] | "?", sizeof(r->state) - 1);
    const char *health = o["health"] | "";
    lv_label_set_text(r->name, r->cname);
    lv_label_set_text(r->status, o["status"] | "");
    lv_color_t sc = state_color(r->state, health);
    lv_obj_set_style_bg_color(r->dot, sc, 0);
    if (!strcmp(r->state, "running")) {
      running++;
      char st[32];
      snprintf(st, sizeof(st), "%5.1f%%  %s", (double)(o["cpu"] | 0.0f), o["mem"] | "--");
      lv_label_set_text(r->stats, st);
    } else {
      stopped++;
      lv_label_set_text(r->stats, r->state);
    }
    lv_obj_set_style_text_color(r->stats, !strcmp(r->state, "running") ? T.fg : sc, 0);
    if (!strcmp(health, "unhealthy") || !strcmp(r->state, "restarting")) bad = true;
    lv_obj_clear_flag(r->row, LV_OBJ_FLAG_HIDDEN);
    if (!strcmp(r->id, dock_sel_id)) lv_label_set_text(dd_status, o["status"] | "");
  }
  for (; i < MAX_CONT; i++) {
    crows[i].id[0] = 0;
    lv_obj_add_flag(crows[i].row, LV_OBJ_FLAG_HIDDEN);
  }
  if (!ok) {
    lv_label_set_text_fmt(dock_err, LV_SYMBOL_WARNING " Docker not reachable\n%s", d["err"] | "");
    lv_obj_clear_flag(dock_err, LV_OBJ_FLAG_HIDDEN);
    lv_label_set_text(dock_summary, "DOCKER  offline");
  } else if (c.size() == 0) {
    lv_label_set_text(dock_err, "No containers.");
    lv_obj_clear_flag(dock_err, LV_OBJ_FLAG_HIDDEN);
    lv_label_set_text(dock_summary, "DOCKER  0 containers");
  } else {
    lv_obj_add_flag(dock_err, LV_OBJ_FLAG_HIDDEN);
    lv_label_set_text_fmt(dock_summary, "DOCKER  %d running  " LV_SYMBOL_BULLET "  %d stopped", running, stopped);
  }
  dock_alert = bad;
}

static void on_logs(JsonDocument &d) {
  if (strcmp(d["id"] | "", dock_sel_id) != 0) return;
  lv_label_set_text(dd_logs, d["lines"] | "");
}

// --------------------------------------------------------------------------------- shell

static void tab_cb(lv_event_t *e) {
  int p = lv_tabview_get_tab_act(tabview);
  if (p == cur_page) return;
  cur_page = p;
  JsonDocument d;
  d["t"] = "page";
  d["p"] = cur_page;
  send_doc(d);
}

static void build_ui() {
  lv_obj_t *scr = lv_scr_act();
  lv_obj_clean(scr);
  lv_obj_clean(lv_layer_top());
  lv_obj_set_style_bg_color(scr, T.bg, 0);
  lv_obj_set_style_bg_opa(scr, LV_OPA_COVER, 0);
  lv_disp_t *disp = lv_disp_get_default();
  lv_theme_t *th = lv_theme_default_init(disp, T.accent, T.accent2, true, &lv_font_montserrat_14);
  lv_disp_set_theme(disp, th);

  tabview = lv_tabview_create(scr, LV_DIR_BOTTOM, TAB_H);
  lv_obj_set_style_bg_color(tabview, T.bg, 0);
  lv_obj_t *content = lv_tabview_get_content(tabview);
  lv_obj_clear_flag(content, LV_OBJ_FLAG_SCROLLABLE);  // tabs only; swiping is unreliable on resistive panels

  lv_obj_t *tb = lv_tabview_get_tab_btns(tabview);
  lv_obj_set_style_bg_color(tb, T.panel, 0);
  lv_obj_set_style_border_color(tb, T.accent, 0);
  lv_obj_set_style_border_side(tb, LV_BORDER_SIDE_TOP, 0);
  lv_obj_set_style_border_width(tb, 2, 0);
  lv_obj_set_style_text_font(tb, &lv_font_montserrat_14, 0);
  lv_obj_set_style_text_color(tb, T.dim, LV_PART_ITEMS);
  lv_obj_set_style_text_color(tb, T.bg, LV_PART_ITEMS | LV_STATE_CHECKED);
  lv_obj_set_style_bg_color(tb, T.accent, LV_PART_ITEMS | LV_STATE_CHECKED);
  lv_obj_set_style_bg_opa(tb, LV_OPA_COVER, LV_PART_ITEMS | LV_STATE_CHECKED);
  lv_obj_set_style_border_width(tb, 0, LV_PART_ITEMS | LV_STATE_CHECKED);

  const lv_font_t *f = &lv_font_montserrat_14;
  (void)f;
  lv_obj_t *pages[4];
  for (int i = 0; i < 4; i++) {
    pages[i] = lv_tabview_add_tab(tabview, T.tabs[i]);
    lv_obj_set_style_pad_all(pages[i], 3, 0);
    lv_obj_clear_flag(pages[i], LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_set_style_bg_color(pages[i], T.bg, 0);
    lv_obj_set_style_bg_opa(pages[i], LV_OPA_COVER, 0);
  }
  build_mix(pages[0]);
  build_deck(pages[1]);
  build_sys(pages[2]);
  build_dock(pages[3]);
  lv_obj_add_event_cb(tabview, tab_cb, LV_EVENT_VALUE_CHANGED, NULL);
  if (cur_page) lv_tabview_set_act(tabview, cur_page, LV_ANIM_OFF);

  // Link status in the tab bar's right corner and a toast area above it
  link_lbl = label(lv_layer_top(), &lv_font_montserrat_10, T.bad, LV_SYMBOL_USB " NO LINK");
  lv_obj_align(link_lbl, LV_ALIGN_TOP_RIGHT, -4, 2);
  lv_obj_add_flag(link_lbl, LV_OBJ_FLAG_HIDDEN);

  toast_lbl = label(lv_layer_top(), &lv_font_montserrat_14, T.fg, "");
  lv_obj_set_style_bg_color(toast_lbl, T.panel, 0);
  lv_obj_set_style_bg_opa(toast_lbl, LV_OPA_COVER, 0);
  lv_obj_set_style_border_width(toast_lbl, 2, 0);
  lv_obj_set_style_pad_hor(toast_lbl, 14, 0);
  lv_obj_set_style_pad_ver(toast_lbl, 8, 0);
  lv_obj_align(toast_lbl, LV_ALIGN_BOTTOM_MID, 0, -(TAB_H + 8));
  lv_obj_add_flag(toast_lbl, LV_OBJ_FLAG_HIDDEN);
}

static void send_ready(const char *kind) {
  JsonDocument d;
  d["t"] = kind;
  d["fw"] = CYD_FW_VERSION;
  d["w"] = SCR_W;
  d["h"] = SCR_H;
  d["page"] = cur_page;
  d["heap"] = hal_free_heap();
  send_doc(d);
}

static void on_theme(JsonDocument &d) {
  Theme old = T;
  const char *name = d["name"] | "custom";
  strncpy(T.name, name, sizeof(T.name) - 1);
  T.bg = hexc(d["bg"], T.bg);
  T.panel = hexc(d["panel"], T.panel);
  T.line = hexc(d["line"], T.line);
  T.fg = hexc(d["fg"], T.fg);
  T.dim = hexc(d["dim"], T.dim);
  T.accent = hexc(d["accent"], T.accent);
  T.accent2 = hexc(d["accent2"], T.accent2);
  T.hi = hexc(d["hi"], T.hi);
  T.ok = hexc(d["ok"], T.ok);
  T.bad = hexc(d["bad"], T.bad);
  JsonArray tabs = d["tabs"].as<JsonArray>();
  for (int i = 0; i < 4 && i < (int)tabs.size(); i++) {
    strncpy(T.tabs[i], tabs[i] | "", sizeof(T.tabs[i]) - 1);
    T.tabs[i][sizeof(T.tabs[i]) - 1] = 0;
  }
  strncpy(T.title, d["title"] | T.title, sizeof(T.title) - 1);
  if (memcmp(&old, &T, sizeof(T)) == 0) return;
  build_ui();
  send_ready("resync");  // deckd answers with everything except the theme
}

void ui_init() {
  theme_default();
  build_ui();
  send_ready("ready");
}

void ui_handle_line(const char *line) {
  if (!line || line[0] != '{') return;
  JsonDocument d;
  if (deserializeJson(d, line)) return;
  const char *t = d["t"] | "";
  last_rx = hal_millis();
  if (!link_up) {
    link_up = true;
    lv_obj_add_flag(link_lbl, LV_OBJ_FLAG_HIDDEN);
  }
  if (!strcmp(t, "hello")) send_ready("ready");
  else if (!strcmp(t, "theme")) on_theme(d);
  else if (!strcmp(t, "mix")) on_mix(d);
  else if (!strcmp(t, "lvl")) on_levels(d);
  else if (!strcmp(t, "deck")) on_deck(d);
  else if (!strcmp(t, "dstate")) on_dstate(d);
  else if (!strcmp(t, "sys")) on_sys(d);
  else if (!strcmp(t, "procs")) on_procs(d);
  else if (!strcmp(t, "dock")) on_dock(d);
  else if (!strcmp(t, "logs")) on_logs(d);
  else if (!strcmp(t, "page")) {
    int p = d["p"] | 0;
    if (p >= 0 && p < 4) {
      lv_tabview_set_act(tabview, p, LV_ANIM_OFF);
      cur_page = p;
    }
  } else if (!strcmp(t, "toast")) {
    const char *lvl = d["level"] | "info";
    lv_color_t c = !strcmp(lvl, "ok") ? T.ok : !strcmp(lvl, "bad") ? T.bad : T.accent2;
    toast(d["msg"] | "", c);
  } else if (!strcmp(t, "bl")) {
    hal_backlight((uint8_t)(d["v"] | 255));
  }
}

void ui_next_page() {
  int p = (lv_tabview_get_tab_act(tabview) + 1) % 4;
  lv_tabview_set_act(tabview, p, LV_ANIM_OFF);
  lv_event_send(tabview, LV_EVENT_VALUE_CHANGED, NULL);  // tab_cb tells deckd
}

void ui_tick() {
  uint32_t now = hal_millis();
  if (link_up && now - last_rx > LINK_TIMEOUT_MS) {
    link_up = false;
    lv_obj_clear_flag(link_lbl, LV_OBJ_FLAG_HIDDEN);
  }
  if (toast_until && now > toast_until) {
    toast_until = 0;
    lv_obj_add_flag(toast_lbl, LV_OBJ_FLAG_HIDDEN);
  }
  // LED: red on alerts, slow blue blink while waiting for deckd, otherwise off
  if (!link_up) {
    hal_led(false, false, (now / 700) % 2 == 0);
    static uint32_t last_hello = 0;
    if (now - last_hello > 2000) {  // keep announcing until deckd answers
      last_hello = now;
      send_ready("ready");
    }
  } else {
    hal_led(sys_alert || dock_alert, false, false);
  }
}
