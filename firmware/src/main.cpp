// cyd-deck firmware for the ESP32-3248S035R (3.5" ST7796, XPT2046 resistive touch).
// Hardware layer only: display, touch, calibration, backlight, LED, and the serial link.
// The UI lives in ui.cpp.

#include <Arduino.h>
#include <Preferences.h>
#define LGFX_USE_V1
#include <LovyanGFX.hpp>
#include <lvgl.h>
#include "hal.h"
#include "ui.h"

// ------------------------------------------------------------------ board settings
// Pins from the board's schematic (display and touch share one SPI bus).
#define PIN_SCLK 14
#define PIN_MOSI 13
#define PIN_MISO 12
#define PIN_LCD_CS 15
#define PIN_LCD_DC 2
#define PIN_TOUCH_CS 33
#define PIN_TOUCH_IRQ 36
#define PIN_BACKLIGHT 27
#define PIN_LED_R 4      // RGB LED, active low
#define PIN_LED_G 16
#define PIN_LED_B 17
#define PIN_BOOT 0       // BOOT button: cycle pages; hold at power-up to recalibrate touch

// If the image comes out mirrored or with red and blue swapped, change these.
#define LCD_ROTATION 1   // 1 or 3 = landscape
#define LCD_INVERT false
#define LCD_RGB_ORDER false

#define SERIAL_BAUD 921600
#define SCREEN_W 480
#define SCREEN_H 320

class LGFX : public lgfx::LGFX_Device {
  lgfx::Panel_ST7796 _panel;
  lgfx::Bus_SPI _bus;
  lgfx::Light_PWM _light;
  lgfx::Touch_XPT2046 _touch;

 public:
  LGFX() {
    {
      auto cfg = _bus.config();
      cfg.spi_host = HSPI_HOST;
      cfg.spi_mode = 0;
      cfg.freq_write = 40000000;
      cfg.freq_read = 16000000;
      cfg.spi_3wire = false;
      cfg.use_lock = true;
      cfg.dma_channel = SPI_DMA_CH_AUTO;
      cfg.pin_sclk = PIN_SCLK;
      cfg.pin_mosi = PIN_MOSI;
      cfg.pin_miso = PIN_MISO;
      cfg.pin_dc = PIN_LCD_DC;
      _bus.config(cfg);
      _panel.setBus(&_bus);
    }
    {
      auto cfg = _panel.config();
      cfg.pin_cs = PIN_LCD_CS;
      cfg.pin_rst = -1;
      cfg.pin_busy = -1;
      cfg.panel_width = 320;
      cfg.panel_height = 480;
      cfg.offset_x = 0;
      cfg.offset_y = 0;
      cfg.offset_rotation = 0;
      cfg.readable = true;
      cfg.invert = LCD_INVERT;
      cfg.rgb_order = LCD_RGB_ORDER;
      cfg.dlen_16bit = false;
      cfg.bus_shared = true;  // touch controller is on the same bus
      _panel.config(cfg);
    }
    {
      auto cfg = _light.config();
      cfg.pin_bl = PIN_BACKLIGHT;
      cfg.invert = false;
      cfg.freq = 12000;
      cfg.pwm_channel = 7;
      _light.config(cfg);
      _panel.setLight(&_light);
    }
    {
      auto cfg = _touch.config();
      cfg.x_min = 0;
      cfg.x_max = 319;
      cfg.y_min = 0;
      cfg.y_max = 479;
      cfg.pin_int = PIN_TOUCH_IRQ;
      cfg.bus_shared = true;
      cfg.offset_rotation = 0;
      cfg.spi_host = HSPI_HOST;
      cfg.freq = 1000000;
      cfg.pin_sclk = PIN_SCLK;
      cfg.pin_mosi = PIN_MOSI;
      cfg.pin_miso = PIN_MISO;
      cfg.pin_cs = PIN_TOUCH_CS;
      _touch.config(cfg);
      _panel.setTouch(&_touch);
    }
    setPanel(&_panel);
  }
};

static LGFX tft;
static Preferences prefs;

// ------------------------------------------------------------------ hal

extern "C" uint32_t hal_millis(void) { return millis(); }

void hal_send(const char *line) {
  Serial.print(line);
  Serial.print('\n');
}

void hal_led(bool r, bool g, bool b) {
  digitalWrite(PIN_LED_R, r ? LOW : HIGH);
  digitalWrite(PIN_LED_G, g ? LOW : HIGH);
  digitalWrite(PIN_LED_B, b ? LOW : HIGH);
}

void hal_backlight(uint8_t level) { tft.setBrightness(level); }

uint32_t hal_free_heap() { return ESP.getFreeHeap(); }

const char *hal_reset_reason() {
  switch (esp_reset_reason()) {
    case ESP_RST_POWERON: return "power-on or EN pin";  // includes resets from the serial DTR/RTS lines
    case ESP_RST_EXT: return "external pin";
    case ESP_RST_SW: return "software";
    case ESP_RST_PANIC: return "panic";
    case ESP_RST_INT_WDT: return "interrupt watchdog";
    case ESP_RST_TASK_WDT: return "task watchdog";
    case ESP_RST_WDT: return "watchdog";
    case ESP_RST_BROWNOUT: return "brownout";
    case ESP_RST_DEEPSLEEP: return "deep sleep";
    default: return "unknown";
  }
}

// ------------------------------------------------------------------ touch calibration

static void calibrate_touch(bool force) {
  uint16_t cal[8];
  prefs.begin("cyd-deck", false);
  bool have = prefs.getBytesLength("touch") == sizeof(cal);
  if (have && !force) {
    prefs.getBytes("touch", cal, sizeof(cal));
    tft.setTouchCalibrate(cal);
    prefs.end();
    return;
  }
  tft.fillScreen(TFT_BLACK);
  tft.setTextColor(TFT_WHITE, TFT_BLACK);
  tft.setTextDatum(textdatum_t::middle_center);
  tft.setFont(&fonts::Font2);
  tft.drawString("Touch the arrow in each corner", SCREEN_W / 2, SCREEN_H / 2 - 10);
  tft.drawString("(use a stylus or fingernail)", SCREEN_W / 2, SCREEN_H / 2 + 12);
  tft.calibrateTouch(cal, TFT_MAGENTA, TFT_BLACK, 20);
  prefs.putBytes("touch", cal, sizeof(cal));
  prefs.end();
  tft.fillScreen(TFT_BLACK);
}

// ------------------------------------------------------------------ LVGL glue

static const uint32_t BUF_LINES = 40;
static lv_color_t *buf1, *buf2;
static lv_disp_draw_buf_t draw_buf;
static lv_disp_drv_t disp_drv;
static lv_indev_drv_t indev_drv;

static void flush_cb(lv_disp_drv_t *drv, const lv_area_t *area, lv_color_t *px) {
  uint32_t w = area->x2 - area->x1 + 1;
  uint32_t h = area->y2 - area->y1 + 1;
  tft.startWrite();
  tft.setAddrWindow(area->x1, area->y1, w, h);
  tft.writePixels((lgfx::rgb565_t *)&px->full, w * h);
  tft.endWrite();
  lv_disp_flush_ready(drv);
}

static void touch_cb(lv_indev_drv_t *drv, lv_indev_data_t *data) {
  uint16_t x, y;
  if (tft.getTouch(&x, &y)) {
    data->state = LV_INDEV_STATE_PR;
    data->point.x = x;
    data->point.y = y;
  } else {
    data->state = LV_INDEV_STATE_REL;
  }
}

// ------------------------------------------------------------------ serial line reader

static char rx_line[6144];  // deck.json can be a few KB
static size_t rx_len = 0;

static void poll_serial() {
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n') {
      rx_line[rx_len] = 0;
      if (rx_len) ui_handle_line(rx_line);
      rx_len = 0;
    } else if (c != '\r') {
      if (rx_len < sizeof(rx_line) - 1) rx_line[rx_len++] = c;
      else rx_len = 0;  // overlong line: drop it
    }
  }
}

// ------------------------------------------------------------------ BOOT button

static void poll_boot_button() {
  static bool was_down = false;
  static uint32_t down_at = 0;
  bool down = digitalRead(PIN_BOOT) == LOW;
  if (down && !was_down) down_at = millis();
  if (!down && was_down && millis() - down_at > 30) ui_next_page();
  was_down = down;
}

void setup() {
  Serial.setRxBufferSize(8192);
  Serial.begin(SERIAL_BAUD);

  pinMode(PIN_LED_R, OUTPUT);
  pinMode(PIN_LED_G, OUTPUT);
  pinMode(PIN_LED_B, OUTPUT);
  hal_led(false, false, false);
  pinMode(PIN_BOOT, INPUT_PULLUP);

  tft.init();
  tft.setRotation(LCD_ROTATION);
  tft.setBrightness(220);

  bool force_cal = digitalRead(PIN_BOOT) == LOW;  // hold BOOT while powering on
  calibrate_touch(force_cal);

  lv_init();
  buf1 = (lv_color_t *)heap_caps_malloc(SCREEN_W * BUF_LINES * sizeof(lv_color_t), MALLOC_CAP_DMA);
  buf2 = (lv_color_t *)heap_caps_malloc(SCREEN_W * BUF_LINES * sizeof(lv_color_t), MALLOC_CAP_DMA);
  lv_disp_draw_buf_init(&draw_buf, buf1, buf2, SCREEN_W * BUF_LINES);

  lv_disp_drv_init(&disp_drv);
  disp_drv.hor_res = SCREEN_W;
  disp_drv.ver_res = SCREEN_H;
  disp_drv.flush_cb = flush_cb;
  disp_drv.draw_buf = &draw_buf;
  lv_disp_drv_register(&disp_drv);

  lv_indev_drv_init(&indev_drv);
  indev_drv.type = LV_INDEV_TYPE_POINTER;
  indev_drv.read_cb = touch_cb;
  indev_drv.long_press_time = 700;  // hold-to-confirm duration
  lv_indev_drv_register(&indev_drv);

  ui_init();
}

void loop() {
  poll_serial();
  poll_boot_button();
  ui_tick();
  lv_timer_handler();
  delay(5);
}
