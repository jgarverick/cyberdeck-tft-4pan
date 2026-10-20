# Builds the desktop simulator from the same ui.cpp the firmware uses.
# Needs the libraries PlatformIO downloads: run `pio run` in ../firmware once first.

LIBDEPS ?= ../firmware/.pio/libdeps/cyd35r
LVGL    := $(LIBDEPS)/lvgl
AJSON   := $(LIBDEPS)/ArduinoJson/src
FW      := ../firmware

CFLAGS   := -O2 -DLV_CONF_INCLUDE_SIMPLE -I$(FW)/include -I$(FW)/src -I$(LVGL) -I$(LIBDEPS)
CXXFLAGS := $(CFLAGS) -std=c++17 -I$(AJSON)

LV_SRC := $(shell find $(LVGL)/src -name '*.c')
LV_OBJ := $(patsubst $(LVGL)/%.c,build/lvgl/%.o,$(LV_SRC))

sim: build/sim.o build/ui.o $(LV_OBJ)
	$(CXX) -o $@ $^

build/lvgl/%.o: $(LVGL)/%.c $(FW)/include/lv_conf.h
	@mkdir -p $(dir $@)
	@$(CC) $(CFLAGS) -c $< -o $@

build/ui.o: $(FW)/src/ui.cpp $(FW)/src/ui.h $(FW)/src/hal.h
	@mkdir -p build
	$(CXX) $(CXXFLAGS) -c $< -o $@

build/sim.o: sim.cpp
	@mkdir -p build
	$(CXX) $(CXXFLAGS) -c $< -o $@

clean:
	rm -rf build sim
