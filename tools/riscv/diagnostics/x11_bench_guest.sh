#!/bin/sh
# Measure the X round trip and the scroll redraw rate on the Megrez desktop.
#
# DISPLAY must be exported: a bare assignment stays a shell variable and
# x11perf then fails with "Can't open display", which surfaces as NA here.
export DISPLAY=:0
command -v x11perf >/dev/null 2>&1 || { echo M_x_sync_ms NA; echo M_scroll_per_s NA; exit 0; }
sync=$(x11perf -time 1 -aa10text 2>&1 | sed -n 's/.*Sync time adjustment is \([0-9.]*\) msecs.*/\1/p' | head -1)
[ -n "$sync" ] || sync=NA
echo M_x_sync_ms "$sync"
scroll=$(x11perf -time 2 -scroll500 2>&1 | sed -n 's/.*( *\([0-9.]*\)\/sec).*/\1/p' | tail -1)
[ -n "$scroll" ] || scroll=NA
echo M_scroll_per_s "$scroll"
echo ZZX11DONE
