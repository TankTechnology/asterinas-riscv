#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
#
# Small hardware probe for the matrix evidence: audio device enumeration and
# the first aplay error, plus static USB enumeration.
#
# Markers: hwdiag_* and DAILY_DONE=ok.

set -u
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

LOG=/tmp/matrix-hw-diag.log
: >"$LOG"
log() { printf '%s\n' "$*" | tee -a "$LOG" >/dev/console; }

log "matrix_hw_diag start $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
log "hwdiag_cards=$(cat /proc/asound/cards 2>&1 | tr '\n' '|' | head -c 400)"
log "hwdiag_pcm=$(cat /proc/asound/pcm 2>&1 | tr '\n' '|' | head -c 400)"
log "hwdiag_snd_nodes=$(ls /dev/snd 2>&1 | tr '\n' ' ' | head -c 300)"
log "hwdiag_aplay_list=$(aplay -l 2>&1 | tr '\n' '|' | head -c 800)"

python3 - /tmp/diag-tone.wav <<'PY'
import struct, sys, wave
samples = []
for frame in range(24000):
    value = 8000 if (frame // 54) % 2 == 0 else -8000
    samples += [value, value]
with wave.open(sys.argv[1], "wb") as wav:
    wav.setnchannels(2); wav.setsampwidth(2); wav.setframerate(48000)
    wav.writeframes(struct.pack("<%dh" % len(samples), *samples))
PY

timeout 20 aplay -D hw:0,0 /tmp/diag-tone.wav >/tmp/diag-aplay.log 2>&1
rc=$?
log "hwdiag_aplay rc=$rc err=$(head -2 /tmp/diag-aplay.log | tr '\n' '|' | head -c 300)"

log "hwdiag_lsusb=$(lsusb 2>&1 | tr '\n' '|' | head -c 400)"
log "hwdiag_lsusb_tree=$(lsusb -t 2>&1 | tr '\n' '|' | head -c 400)"
log "hwdiag_usb_sys=$(ls /sys/bus/usb/devices 2>&1 | tr '\n' ' ' | head -c 300)"
log "hwdiag_pci_audio=$(lspci 2>/dev/null | grep -i audio | head -2 | tr '\n' '|' | head -c 300)"

log "matrix_hw_diag_done=$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo DAILY_DONE=ok >/dev/console
