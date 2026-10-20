/* LVGL 8.3 configuration for the cyd-deck control surface.
 * Shared by the ESP32 firmware and the desktop simulator.
 * Anything not set here uses LVGL's default from lv_conf_internal.h. */
#if 1
#ifndef LV_CONF_H
#define LV_CONF_H

#include <stdint.h>

/* Color: RGB565, no byte swap (LovyanGFX handles the panel byte order) */
#define LV_COLOR_DEPTH 16
#define LV_COLOR_16_SWAP 0
#define LV_COLOR_SCREEN_TRANSP 0

/* Memory: use the system heap (the ESP32 has no PSRAM on this board) */
#define LV_MEM_CUSTOM 1
#define LV_MEM_CUSTOM_INCLUDE <stdlib.h>
#define LV_MEM_CUSTOM_ALLOC malloc
#define LV_MEM_CUSTOM_FREE free
#define LV_MEM_CUSTOM_REALLOC realloc

/* Timing */
#define LV_DISP_DEF_REFR_PERIOD 20
#define LV_INDEV_DEF_READ_PERIOD 20
#define LV_TICK_CUSTOM 1
#define LV_TICK_CUSTOM_INCLUDE "hal.h"
#define LV_TICK_CUSTOM_SYS_TIME_EXPR (hal_millis())

/* Drawing */
#define LV_DRAW_COMPLEX 1
#define LV_SHADOW_CACHE_SIZE 0
#define LV_IMG_CACHE_DEF_SIZE 0
#define LV_GRADIENT_MAX_STOPS 2

/* Logging and debug overlays off */
#define LV_USE_LOG 0
#define LV_USE_ASSERT_NULL 1
#define LV_USE_ASSERT_MALLOC 1
#define LV_USE_PERF_MONITOR 0
#define LV_USE_MEM_MONITOR 0

/* Fonts */
#define LV_FONT_MONTSERRAT_10 1
#define LV_FONT_MONTSERRAT_12 1
#define LV_FONT_MONTSERRAT_14 1
#define LV_FONT_MONTSERRAT_16 1
#define LV_FONT_MONTSERRAT_20 1
#define LV_FONT_MONTSERRAT_28 1
#define LV_FONT_UNSCII_8 1
#define LV_FONT_UNSCII_16 1
#define LV_FONT_DEFAULT &lv_font_montserrat_14

/* Widgets and themes */
#define LV_USE_THEME_DEFAULT 1
#define LV_THEME_DEFAULT_DARK 1
#define LV_THEME_DEFAULT_GROW 0
#define LV_THEME_DEFAULT_TRANSITION_TIME 60
#define LV_USE_THEME_BASIC 0
#define LV_USE_THEME_MONO 0

/* Extra widgets we don't use (saves flash) */
#define LV_USE_ANIMIMG 0
#define LV_USE_CALENDAR 0
#define LV_USE_COLORWHEEL 0
#define LV_USE_IMGBTN 0
#define LV_USE_KEYBOARD 0
#define LV_USE_LED 0
#define LV_USE_LIST 1
#define LV_USE_MENU 0
#define LV_USE_METER 0
#define LV_USE_SPAN 0
#define LV_USE_SPINBOX 0
#define LV_USE_SPINNER 0
#define LV_USE_TILEVIEW 0
#define LV_USE_WIN 0
#define LV_USE_MSGBOX 1
#define LV_USE_TABVIEW 1
#define LV_USE_CHART 1

#define LV_BUILD_EXAMPLES 0

#endif /*LV_CONF_H*/
#endif /*Enable content*/
