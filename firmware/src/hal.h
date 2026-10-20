// Hardware hooks the UI calls. main.cpp implements these for the CYD;
// the desktop simulator implements them for screenshots and testing.
#pragma once
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

uint32_t hal_millis(void);

#ifdef __cplusplus
}
#endif

#ifdef __cplusplus
void hal_send(const char *line);        // write one JSON line to deckd
void hal_led(bool r, bool g, bool b);   // onboard RGB LED
void hal_backlight(uint8_t level);      // 0-255
uint32_t hal_free_heap();               // bytes, reported to deckd for diagnostics
const char *hal_reset_reason();         // why the board last restarted
#endif
