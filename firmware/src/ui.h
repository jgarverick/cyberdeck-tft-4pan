// cyd-deck UI: four pages (mixer, deck, system, docker) driven by JSON lines from deckd.
#pragma once

#define CYD_FW_VERSION "1.0.0"

void ui_init();                          // build the UI with the built-in default theme
void ui_handle_line(const char *line);   // process one JSON line from deckd
void ui_tick();                          // call every loop: link watchdog, LED, toasts
void ui_next_page();                     // BOOT button: go to the next page
