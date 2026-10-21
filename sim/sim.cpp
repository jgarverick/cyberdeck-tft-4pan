// Desktop simulator for the cyd-deck UI. Compiles the real ui.cpp against LVGL and
// renders into an in-memory framebuffer, so screenshots match the device.
//
// Script mode (deterministic, simulated time):
//   ./sim script.txt
//     {"t":...}          feed a protocol line to the UI
//     tick <ms>          advance time
//     touch <x> <y>      tap
//     hold <x> <y> <ms>  long press
//     snap <file.ppm>    save a screenshot
//   Lines the UI sends to deckd are printed to stdout as "TX <json>".
//
// Serial mode (real time, for end-to-end tests with deckd):
//   ./sim --serial /dev/pts/N --run <seconds> [--script timed.txt]
//     timed script lines are "@<ms> <command>" using the commands above.

#include <lvgl.h>
#include "hal.h"
#include "ui.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <string>
#include <vector>
#include <fcntl.h>
#include <unistd.h>
#include <termios.h>
#include <time.h>
#include <malloc.h>

static const int W = 480, H = 320;
static uint16_t fb[W * H];
static uint32_t now_ms = 0;
static bool real_time = false;
static int serial_fd = -1;
static int touch_x = 0, touch_y = 0;
static uint32_t touch_until = 0;

static uint32_t real_ms() {
  static timespec t0 = {0, 0};
  timespec t;
  clock_gettime(CLOCK_MONOTONIC, &t);
  if (t0.tv_sec == 0 && t0.tv_nsec == 0) t0 = t;
  return (uint32_t)((t.tv_sec - t0.tv_sec) * 1000 + (t.tv_nsec - t0.tv_nsec) / 1000000);
}

extern "C" uint32_t hal_millis(void) { return real_time ? real_ms() : now_ms; }

void hal_send(const char *line) {
  printf("TX %s\n", line);
  fflush(stdout);
  if (serial_fd >= 0) {
    std::string s(line);
    s += "\n";
    if (write(serial_fd, s.data(), s.size()) < 0) perror("write");
  }
}
void hal_led(bool r, bool g, bool b) {
  static int last = -1;
  int v = (r << 2) | (g << 1) | (int)b;
  if (v != last) { printf("LED r=%d g=%d b=%d\n", r, g, b); last = v; }
}
void hal_backlight(uint8_t level) { printf("BACKLIGHT %d\n", level); }
uint32_t hal_free_heap() { return 0; }
const char *hal_reset_reason() { return "simulator"; }

static void flush_cb(lv_disp_drv_t *drv, const lv_area_t *a, lv_color_t *px) {
  for (int y = a->y1; y <= a->y2; y++)
    for (int x = a->x1; x <= a->x2; x++) fb[y * W + x] = (px++)->full;
  lv_disp_flush_ready(drv);
}

static void touch_cb(lv_indev_drv_t *drv, lv_indev_data_t *d) {
  bool pressed = hal_millis() < touch_until;
  d->state = pressed ? LV_INDEV_STATE_PR : LV_INDEV_STATE_REL;
  d->point.x = touch_x;
  d->point.y = touch_y;
}

static void snap(const char *path) {
  lv_refr_now(NULL);
  FILE *f = fopen(path, "wb");
  if (!f) { perror(path); return; }
  fprintf(f, "P6\n%d %d\n255\n", W, H);
  for (int i = 0; i < W * H; i++) {
    uint16_t c = fb[i];
    unsigned char rgb[3] = {(unsigned char)(((c >> 11) & 0x1f) * 255 / 31),
                            (unsigned char)(((c >> 5) & 0x3f) * 255 / 63),
                            (unsigned char)((c & 0x1f) * 255 / 31)};
    fwrite(rgb, 1, 3, f);
  }
  fclose(f);
  printf("SNAP %s\n", path);
}

static void step(uint32_t ms) {
  for (uint32_t t = 0; t < ms; t += 5) {
    if (!real_time) now_ms += 5;
    ui_tick();
    lv_timer_handler();
  }
}

static void press(int x, int y, uint32_t ms) {
  touch_x = x;
  touch_y = y;
  touch_until = hal_millis() + ms;
  step(ms + 60);
}

static void run_command(const std::string &line) {
  if (line.empty() || line[0] == '#') return;
  if (line[0] == '{') { ui_handle_line(line.c_str()); step(30); return; }
  char cmd[32] = {0}, arg[256] = {0};
  int a = 0, b = 0, c = 0;
  if (sscanf(line.c_str(), "%31s", cmd) != 1) return;
  if (!strcmp(cmd, "tick") && sscanf(line.c_str(), "%*s %d", &a) == 1) step(a);
  else if (!strcmp(cmd, "touch") && sscanf(line.c_str(), "%*s %d %d", &a, &b) == 2) press(a, b, 80);
  else if (!strcmp(cmd, "hold") && sscanf(line.c_str(), "%*s %d %d %d", &a, &b, &c) == 3) press(a, b, c);
  else if (!strcmp(cmd, "heap")) printf("HEAP %zu bytes in use\n", mallinfo2().uordblks);
  else if (!strcmp(cmd, "snap") && sscanf(line.c_str(), "%*s %255s", arg) == 1) snap(arg);
  else fprintf(stderr, "unknown command: %s\n", line.c_str());
}

static void init_lvgl() {
  lv_init();
  static lv_color_t buf[W * 40];
  static lv_disp_draw_buf_t db;
  lv_disp_draw_buf_init(&db, buf, NULL, W * 40);
  static lv_disp_drv_t dd;
  lv_disp_drv_init(&dd);
  dd.hor_res = W;
  dd.ver_res = H;
  dd.flush_cb = flush_cb;
  dd.draw_buf = &db;
  lv_disp_drv_register(&dd);
  static lv_indev_drv_t id;
  lv_indev_drv_init(&id);
  id.type = LV_INDEV_TYPE_POINTER;
  id.read_cb = touch_cb;
  id.long_press_time = 700;
  lv_indev_drv_register(&id);
}

int main(int argc, char **argv) {
  const char *serial = NULL, *script = NULL;
  int run_s = 10;
  for (int i = 1; i < argc; i++) {
    if (!strcmp(argv[i], "--serial") && i + 1 < argc) serial = argv[++i];
    else if (!strcmp(argv[i], "--run") && i + 1 < argc) run_s = atoi(argv[++i]);
    else if (!strcmp(argv[i], "--script") && i + 1 < argc) script = argv[++i];
    else script = argv[i];
  }
  init_lvgl();

  if (!serial) {
    ui_init();
    step(50);
    FILE *f = script ? fopen(script, "r") : stdin;
    if (!f) { perror(script); return 1; }
    char line[16384];
    while (fgets(line, sizeof(line), f)) {
      std::string s(line);
      while (!s.empty() && (s.back() == '\n' || s.back() == '\r')) s.pop_back();
      run_command(s);
    }
    return 0;
  }

  // Serial mode
  real_time = true;
  serial_fd = open(serial, O_RDWR | O_NOCTTY | O_NONBLOCK);
  if (serial_fd < 0) { perror(serial); return 1; }
  termios tio;
  if (tcgetattr(serial_fd, &tio) == 0) { cfmakeraw(&tio); tcsetattr(serial_fd, TCSANOW, &tio); }
  std::vector<std::pair<uint32_t, std::string>> timed;
  if (script) {
    FILE *f = fopen(script, "r");
    char line[1024];
    while (f && fgets(line, sizeof(line), f)) {
      unsigned at;
      char rest[1000];
      if (sscanf(line, "@%u %999[^\n]", &at, rest) == 2) timed.push_back({at, rest});
    }
    if (f) fclose(f);
  }
  ui_init();
  std::string rx;
  size_t next = 0;
  while (real_ms() < (uint32_t)run_s * 1000) {
    char buf[4096];
    ssize_t n = read(serial_fd, buf, sizeof(buf));
    for (ssize_t i = 0; i < n; i++) {
      if (buf[i] == '\n') {
        printf("RX %u %.90s\n", real_ms(), rx.c_str());
        ui_handle_line(rx.c_str());
        rx.clear();
      }
      else if (buf[i] != '\r') rx += buf[i];
    }
    while (next < timed.size() && real_ms() >= timed[next].first) run_command(timed[next++].second);
    ui_tick();
    lv_timer_handler();
    usleep(5000);
  }
  return 0;
}
