#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
#
# GUI stress: start Xorg, launch ten concurrent GUI clients, run a 2 GiB
# write + sha256 through /dev/shm, sample memory/load/windows for 20 minutes,
# then do three suspend/wake cycles. Every key line goes to /dev/console and
# /tmp/gui-stress.log.

set -u
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

LOG=/tmp/gui-stress.log
: >"$LOG"
export DISPLAY=:0
IMG=/usr/share/pixmaps/debian-logo.png
if [ ! -f "$IMG" ]; then
    IMG=/tmp/gui-probe.png
    printf '%s' 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=' \
        | base64 -d >"$IMG" 2>/dev/null
fi
APP_ENV="MESA_LOADER_DRIVER_OVERRIDE=kms_swrast LIBGL_ALWAYS_SOFTWARE=1 GALLIUM_DRIVER=softpipe GDK_GL=disable"

now() { cut -d' ' -f1 /proc/uptime; }
memavail() { awk '/^MemAvailable:/{print $2}' /proc/meminfo; }
loadavg() { cut -d' ' -f1 /proc/loadavg; }
log() { echo "$*" | tee -a "$LOG" >/dev/console; }

log "gui_stress start $(date -u '+%Y-%m-%dT%H:%M:%SZ')"

# --- package availability (x11-apps is not in the toolchain profile) ------
# QEMU's user-mode network exposes its DNS at 10.0.2.3. This image was built
# with 1.1.1.1, which does not resolve through slirp, so point it at the
# virtual resolver before apt.
printf 'nameserver 10.0.2.3\n' >/etc/resolv.conf 2>/dev/null || true
timeout 120 apt-get update >/tmp/gui-apt-update.log 2>&1
timeout 180 apt-get install -y --no-install-recommends x11-apps xdotool \
    >>/tmp/gui-apt.log 2>&1
have_xapps=no
if command -v xclock >/dev/null 2>&1 && command -v xcalc >/dev/null 2>&1 &&
    command -v xeyes >/dev/null 2>&1; then
    have_xapps=yes
fi
have_xdotool=no
command -v xdotool >/dev/null 2>&1 && have_xdotool=yes
log "gui_tools x11_apps=$have_xapps xdotool=$have_xdotool img=$IMG"
if [ "$have_xapps" = no ] || [ "$have_xdotool" = no ]; then
    log "gui_apt_error update=$(head -2 /tmp/gui-apt-update.log 2>/dev/null | tr '\n' '_' | head -c 160) install=$(head -2 /tmp/gui-apt.log 2>/dev/null | tr '\n' '_' | head -c 160)"
fi

# --- start Xorg on :0 ------------------------------------------------------
mkdir -p /tmp/.X11-unix
env MESA_LOADER_DRIVER_OVERRIDE=kms_swrast LIBGL_ALWAYS_SOFTWARE=1 \
    GALLIUM_DRIVER=softpipe /usr/lib/xorg/Xorg :0 -noreset -nolisten tcp \
    -config /etc/X11/xorg-noudev.conf >/tmp/gui-xorg.log 2>&1 &
waited=0
while [ ! -S /tmp/.X11-unix/X0 ] && [ "$waited" -lt 40 ]; do
    sleep 1
    waited=$((waited + 1))
done
if [ -S /tmp/.X11-unix/X0 ]; then
    if xdpyinfo -queryExtensions 2>/dev/null | grep -q DRI3; then
        log "DAILY_DESKTOP=ok Xorg+DRI3"
    else
        log "DAILY_DESKTOP=fail Xorg-up-no-DRI3"
    fi
else
    log "DAILY_DESKTOP=fail no-X-socket"
fi

# --- ten concurrent clients ------------------------------------------------
launch() {
    label=$1
    shift
    env $APP_ENV "$@" >"/tmp/gui-app-$label.log" 2>&1 &
    sleep 2
}
if [ "$have_xapps" = yes ]; then
    APPS="pcmanfm1 eog1 xterm1 xterm2 xclock xcalc xeyes pcmanfm2 eog2 xterm3"
else
    # Keep ten windows when x11-apps cannot be installed: three extra xterms
    # stand in for xclock/xcalc/xeyes and the table records the substitution.
    APPS="pcmanfm1 eog1 xterm1 xterm2 xterm4 xterm5 xterm6 pcmanfm2 eog2 xterm3"
fi
for label in $APPS; do
    case "$label" in
        pcmanfm1) launch "$label" pcmanfm /etc/hostname ;;
        pcmanfm2) launch "$label" pcmanfm /tmp ;;
        eog1 | eog2) launch "$label" eog "$IMG" ;;
        xterm1) launch "$label" xterm -title gui-soak-1 -e sleep 3600 ;;
        xterm2) launch "$label" xterm -title gui-soak-2 -e top -b -d 5 ;;
        xterm3) launch "$label" xterm -title gui-soak-3 -e sh -c 'while :; do ls /usr >/dev/null; done' ;;
        xterm4 | xterm5 | xterm6) launch "$label" xterm -title "gui-soak-$label" -e sleep 3600 ;;
        xclock) launch "$label" xclock ;;
        xcalc) launch "$label" xcalc ;;
        xeyes) launch "$label" xeyes ;;
    esac
done
sleep 5

count_class() {
    cls=$1
    if [ "$have_xdotool" = yes ]; then
        xdotool search --onlyvisible --class "$cls" 2>/dev/null | wc -l
    else
        xwininfo -root -tree 2>/dev/null | grep -icE "$cls"
    fi
}
count_windows() {
    total=0
    for cls in '[Pp]cmanfm' '[Ee]og' '[Xx]term' '[Xx]clock' '[Xx]calc' '[Xx]eyes'; do
        c=$(count_class "$cls")
        total=$((total + c))
    done
    echo "$total"
}

win_total=$(count_windows)
log "gui_windows total=$win_total apps=\"$APPS\""
for cls in '[Pp]cmanfm' '[Ee]og' '[Xx]term' '[Xx]clock' '[Xx]calc' '[Xx]eyes'; do
    log "gui_window_class class=$cls count=$(count_class "$cls")"
done
if [ "$win_total" -lt 10 ]; then
    sleep 5
    win_total=$(count_windows)
    log "gui_windows retry_total=$win_total"
fi

# --- 2 GiB write + sha256 through /dev/shm ---------------------------------
mount -o remount,size=3G /dev/shm 2>/dev/null || true
shm_avail=$(df -k /dev/shm 2>/dev/null | awk 'NR==2{print $4}')
IOFILE=/dev/shm/gui-big
io_note=""
if [ -n "$shm_avail" ] && [ "$shm_avail" -ge 2100000 ]; then
    ( dd if=/dev/zero of="$IOFILE" bs=1M count=2048 2>/tmp/gui-dd.log; echo "rc=$?" >/tmp/gui-dd.rc ) &
else
    IOFILE=/tmp/gui-big
    io_note="shm_avail=${shm_avail:-unknown}_fallback_tmp"
fi
io_note="$io_note io_file=$IOFILE"
log "gui_io start $io_note"
( sha256sum "$IOFILE" >/tmp/gui-sha.log 2>&1; echo "rc=$?" >/tmp/gui-sha.rc ) &

# --- hold + sample (20 minutes) -------------------------------------------
base_ma=$(memavail)
base_up=$(now)
log "gui_baseline memavail=$base_ma uptime=$base_up"
sample=0
while :; do
    up=$(now)
    elapsed=$(awk -v a="$base_up" -v b="$up" 'BEGIN{printf "%.0f", b-a}')
    [ "$elapsed" -ge 1200 ] && break
    sample=$((sample + 1))
    ma=$(memavail)
    la=$(loadavg)
    wins=$(count_windows)
    if [ "$have_xdotool" = yes ] && [ $((sample % 10)) -eq 0 ]; then
        w=$(xdotool search --onlyvisible --class '[Pp]cmanfm|[Ee]og|[Xx]term' 2>/dev/null | head -1)
        [ -n "$w" ] && xdotool windowactivate "$w" 2>/dev/null
    fi
    if [ $((sample % 4)) -eq 0 ]; then
        log "gui_sample t=${elapsed}s memavail=$ma load=$la windows=$wins"
    fi
    sleep 30
done

dd_rc="pending"
sha_rc="pending"
[ -f /tmp/gui-dd.rc ] && dd_rc=$(cut -d= -f2 /tmp/gui-dd.rc)
[ -f /tmp/gui-sha.rc ] && sha_rc=$(cut -d= -f2 /tmp/gui-sha.rc)
sha_val=$(cut -d' ' -f1 /tmp/gui-sha.log 2>/dev/null)
log "gui_io done dd_rc=$dd_rc sha_rc=$sha_rc sha=${sha_val:-none}"
rm -f "$IOFILE"

# --- three suspend/wake cycles --------------------------------------------
suspend_ok=0
for i in 1 2 3; do
    before=$(now)
    echo "DAILY_SUSPEND_WAIT" >/dev/console
    timeout 60 systemctl suspend >"/tmp/gui-suspend-$i.log" 2>&1
    rc=$?
    sleep 3
    after=$(now)
    wins=$(count_windows)
    apps=$(pgrep -c -f 'pcmanfm|eog|xterm|top' 2>/dev/null || true)
    probe=""
    if command -v xclock >/dev/null 2>&1; then
        env $APP_ENV xclock >/tmp/gui-probe-$i.log 2>&1 &
        sleep 3
        probe=$(count_class '[Xx]clock')
        pkill -x xclock 2>/dev/null
    else
        env $APP_ENV eog "$IMG" >/tmp/gui-probe-$i.log 2>&1 &
        sleep 3
        probe=$(count_class '[Ee]og')
        pkill -x eog 2>/dev/null
    fi
    if [ "$rc" -eq 0 ] && [ -n "$wins" ] && [ "$wins" -ge 8 ]; then
        suspend_ok=$((suspend_ok + 1))
        result=ok
    else
        result=fail
    fi
    log "gui_suspend round=$i rc=$rc before=$before after=$after windows=$wins apps=$apps probe=$probe result=$result err=$(head -1 /tmp/gui-suspend-$i.log 2>/dev/null | tr ' ' '_' | head -c 60)"
done

last_ma=$(memavail)
end_up=$(now)

{
    echo "=== gui-stress summary ==="
    echo "duration_s=$(awk -v a="$base_up" -v b="$end_up" 'BEGIN{printf "%.1f", b-a}')"
    echo "memavail_baseline=$base_ma memavail_last=$last_ma"
    echo "windows=$win_total tools_x11_apps=$have_xapps xdotool=$have_xdotool"
    echo "io dd_rc=$dd_rc sha_rc=$sha_rc $io_note"
    echo "suspend_ok=$suspend_ok/3"
    for cls in '[Pp]cmanfm' '[Ee]og' '[Xx]term' '[Xx]clock' '[Xx]calc' '[Xx]eyes'; do
        echo "window_class class=$cls count=$(count_class "$cls")"
    done
    echo "=== app first errors ==="
    for label in $APPS; do
        if [ -s "/tmp/gui-app-$label.log" ]; then
            echo "-- $label: $(head -1 "/tmp/gui-app-$label.log" | tr '\n' ' ' | head -c 120)"
        fi
    done
    echo "=== dmesg scan ==="
    for pat in "Uncaught panic" "oom-killer" "Out of memory" "allocation failed"; do
        c=$(dmesg | grep -c -- "$pat" 2>/dev/null || true)
        echo "dmesg_count[$pat]=$c"
    done
    echo "=== gui-stress log ==="
    cat "$LOG"
} >/dev/console 2>&1

pkill -f 'pcmanfm|eog|xterm|xclock|xcalc|xeyes' 2>/dev/null
echo DAILY_DONE=ok >/dev/console
