#!/bin/sh
# SPDX-License-Identifier: MPL-2.0

set -u

systemctl_bounded() {
    /usr/bin/timeout --kill-after=1s 3s /usr/bin/systemctl "$@"
}

die_usage() {
    exit 2
}

is_uint() {
    case "$1" in
        '' | *[!0-9]*) return 1 ;;
        # Linux PIDs are commonly capped below 2^22, but accept a full
        # unsigned 32-bit decimal width so the guest helper does not impose a
        # smaller PID namespace contract than the host.
        *) [ "${#1}" -le 10 ] ;;
    esac
}

is_seconds() {
    case "$1" in
        '' | '.' | *[!0-9.]* | *.*.*) return 1 ;;
        *) [ "${#1}" -le 8 ] ;;
    esac
}

is_nonce() {
    [ "${#1}" -eq 4 ] || return 1
    case "$1" in
        *[!0-9]*) return 1 ;;
        *) return 0 ;;
    esac
}

is_experiment_id() {
    [ "${#1}" -eq 32 ] || return 1
    case "$1" in
        *[!0-9a-f]*) return 1 ;;
        *) return 0 ;;
    esac
}

is_profile_timeout() {
    is_uint "$1" && [ "$1" -ge 1 ] && [ "$1" -le 120 ]
}

input_identity() {
    PYTHONPYCACHEPREFIX=/run/asterinas-python-cache \
        /usr/bin/timeout --kill-after=1s 10s /usr/bin/python3 -c 'import glob,os,runpy;m=runpy.run_path("/run/asterinas-tools/desktop-input-identity");d=[(os.path.basename(p),m["read_identity"](p)) for p in glob.glob("/dev/input/event*")];uk=(3,"usb_boot_keyboard","xhci/input0");um=(3,"usb_boot_mouse","xhci/input1");qk=(6,"QEMU Virtio Keyboard","virtio/input0");qm=(6,"QEMU Virtio Tablet","virtio/input0");ks=[p for p,x in d if x in (uk,qk)];ms=[p for p,x in d if x in (um,qm)];print(sum(x in (uk,um) for _,x in d),int(sum(x==uk for _,x in d)==1),int(sum(x==um for _,x in d)==1),ks[0] if len(ks)==1 else "missing",ms[0] if len(ms)==1 else "missing")' 2>/dev/null || printf '0 0 0 missing missing\n'
}

browser_stage() {
    printf '__ASTERINAS_PHYSICAL_BROWSER_STAGE__ stage=%s\n' "$1"
}

preflight() {
    input_nodes=0
    for node in /dev/input/event*; do
        [ -c "$node" ] && input_nodes=$((input_nodes + 1))
    done
    framebuffer=0
    [ -c /dev/fb0 ] && framebuffer=1
    set -- $(input_identity)
    usb_inputs=${1:-0}
    usb_keyboard=${2:-0}
    usb_mouse=${3:-0}
    keyboard_node=${4:-missing}
    mouse_node=${5:-missing}
    xorg=0
    xorg_keyboard=0
    xorg_mouse=0
    for xorg_pid in $(pgrep -x Xorg 2>/dev/null); do
        for xorg_fd in /proc/$xorg_pid/fd/*; do
            xorg_target=$(readlink "$xorg_fd" 2>/dev/null || true)
            [ "$xorg_target" = /dev/fb0 ] && [ -S /tmp/.X11-unix/X0 ] && xorg=1
            [ "$xorg_target" = "/dev/input/$keyboard_node" ] && xorg_keyboard=1
            [ "$xorg_target" = "/dev/input/$mouse_node" ] && xorg_mouse=1
        done
    done
    openbox=0
    pgrep -u 1000 -x openbox >/dev/null 2>&1 && openbox=1
    service=$(systemctl is-active asterinas-browser-web.service 2>/dev/null || true)
    pid=$(systemctl show --property MainPID --value asterinas-browser-web.service 2>/dev/null || true)
    restarts=$(systemctl show --property NRestarts --value asterinas-browser-web.service 2>/dev/null || true)
    firefox=0
    case "$pid" in
        '' | *[!0-9]*) ;;
        *) grep -Eq '^firefox(-esr)?$' "/proc/$pid/comm" 2>/dev/null && firefox=1 ;;
    esac
    printf '__ASTERINAS_PHYSICAL_PREFLIGHT__ browser_pid=%s input_nodes=%s framebuffer=%s xorg_fbdev=%s openbox=%s firefox=%s browser_service=%s browser_restarts=%s usb_inputs=%s usb_keyboard=%s usb_mouse=%s keyboard_node=%s mouse_node=%s xorg_keyboard=%s xorg_mouse=%s\n' \
        "$pid" "$input_nodes" "$framebuffer" "$xorg" "$openbox" "$firefox" \
        "$service" "$restarts" "$usb_inputs" "$usb_keyboard" "$usb_mouse" \
        "$keyboard_node" "$mouse_node" "$xorg_keyboard" "$xorg_mouse"
}

start_browser() {
    reset_existing=$1
    runtime_provider_root=/run/asterinas-physical-display-providers
    runtime_config_directory=$runtime_provider_root/fbdev/xorg.conf.d
    runtime_config=$runtime_config_directory/20-asterinas.conf
    source_config=/etc/asterinas/display-providers/fbdev/xorg.conf.d/20-asterinas.conf
    [ -f "$source_config" ] || source_config=/etc/X11/xorg.conf.d/20-asterinas.conf
    status=0
    if [ "$reset_existing" -eq 1 ]; then
        systemctl_bounded stop --no-block asterinas-browser-web.service \
            >/dev/null 2>&1 || status=$?
        systemctl_bounded stop --no-block asterinas-desktop-m5.service \
            >/dev/null 2>&1 || status=$?
        attempt=0
        while systemctl_bounded is-active --quiet \
            asterinas-browser-web.service asterinas-desktop-m5.service \
            2>/dev/null; do
            attempt=$((attempt + 1))
            if [ "$attempt" -ge 30 ]; then
                [ "$status" -ne 0 ] || status=124
                break
            fi
            /usr/bin/sleep 1
        done
    fi

    browser_stage input-wait
    attempt=0
    while [ "$status" -eq 0 ]; do
        set -- $(input_identity)
        if [ "$4" != missing ] && [ "$5" != missing ] && [ "$4" != "$5" ]; then
            keyboard_node=$4
            mouse_node=$5
            break
        fi
        attempt=$((attempt + 1))
        if [ "$attempt" -ge 30 ]; then
            [ "$status" -ne 0 ] || status=125
            break
        fi
        /usr/bin/sleep 1
    done
    [ "$status" -ne 0 ] || browser_stage input-ready

    if [ "$status" -eq 0 ]; then
        [ -f "$source_config" ] || status=126
    fi
    if [ "$status" -eq 0 ]; then
        /usr/bin/mkdir -p "$runtime_config_directory" || status=126
    fi
    if [ "$status" -eq 0 ]; then
        /usr/bin/sed \
            -e '/Identifier "Asterinas keyboard"/,/EndSection/ s#Option "Device" "[^"]*"#Option "Device" "/dev/input/'"$keyboard_node"'"#' \
            -e '/Identifier "Asterinas pointer"/,/EndSection/ s#Option "Device" "[^"]*"#Option "Device" "/dev/input/'"$mouse_node"'"#' \
            "$source_config" >"$runtime_config" || status=126
    fi
    if [ "$status" -eq 0 ]; then
        grep -Fq "Option \"Device\" \"/dev/input/$keyboard_node\"" "$runtime_config" &&
            grep -Fq "Option \"Device\" \"/dev/input/$mouse_node\"" "$runtime_config" || status=126
    fi
    if [ "$status" -eq 0 ]; then
        systemctl_bounded set-environment \
            ASTERINAS_DISPLAY_PROVIDER_ROOT="$runtime_provider_root" \
            >/dev/null 2>&1 || status=126
    fi
    if [ "$status" -eq 0 ]; then
        systemctl_bounded reset-failed asterinas-desktop-m5.service \
            asterinas-browser-web.service >/dev/null 2>&1 || status=$?
    fi
    if [ "$status" -eq 0 ]; then
        browser_stage desktop-start
        systemctl_bounded start --no-block --job-mode=ignore-dependencies \
            asterinas-desktop-m5.service >/dev/null 2>&1 || status=$?
    fi
    attempt=0
    while [ "$status" -eq 0 ] && ! systemctl_bounded is-active --quiet \
        asterinas-desktop-m5.service 2>/dev/null; do
        attempt=$((attempt + 1))
        if [ "$attempt" -ge 30 ]; then
            status=127
            break
        fi
        /usr/bin/sleep 1
    done
    [ "$status" -ne 0 ] || browser_stage desktop-ready

    if [ "$status" -eq 0 ]; then
        systemctl_bounded reset-failed asterinas-browser-web.service \
            >/dev/null 2>&1 || status=$?
    fi
    if [ "$status" -eq 0 ]; then
        browser_stage browser-start
        systemctl_bounded start --no-block --job-mode=ignore-dependencies \
            asterinas-browser-web.service >/dev/null 2>&1 || status=$?
    fi
    printf '__ASTERINAS_PHYSICAL_BROWSER_START__ status=%s\n' "$status"
}

browser_identity() {
    pid=$(systemctl show --property MainPID --value asterinas-browser-web.service 2>/dev/null || true)
    restarts=$(systemctl show --property NRestarts --value asterinas-browser-web.service 2>/dev/null || true)
}

cycle() {
    [ "$#" -eq 7 ] || die_usage
    cycle=$1
    nonce=$2
    timeout=$3
    setup_timeout=$4
    expected_pid=$5
    width=$6
    height=$7
    case "$cycle" in 1 | 2 | 3) ;; *) die_usage ;; esac
    is_nonce "$nonce" || die_usage
    is_seconds "$timeout" || die_usage
    is_seconds "$setup_timeout" || die_usage
    is_uint "$expected_pid" || die_usage
    is_uint "$width" || die_usage
    is_uint "$height" || die_usage

    browser_identity
    original_pid=$pid
    status=125
    case "$original_pid" in
        '' | *[!0-9]*) ;;
        *)
            if [ "$expected_pid" != 0 ] && [ "$original_pid" != "$expected_pid" ]; then
                status=124
            else
                PYTHONPYCACHEPREFIX=/run/asterinas-python-cache \
                    nsenter -t "$original_pid" -n \
                    /run/asterinas-tools/physical-graphics-gate \
                    --nonce "$nonce" --cycle "$cycle" --firefox-pid "$original_pid" \
                    --timeout "$timeout" --setup-timeout "$setup_timeout" \
                    --expected-width "$width" --expected-height "$height"
                status=$?
            fi
            ;;
    esac
    browser_identity
    [ "$pid" = "$original_pid" ] && [ "$restarts" = 0 ] || status=126
    printf '__ASTERINAS_PHYSICAL_COMMAND_STATUS__cycle=%s status=%s\n' \
        "$cycle" "$status"
}

final() {
    [ "$#" -eq 5 ] || die_usage
    cycle=$1
    nonce=$2
    timeout=$3
    setup_timeout=$4
    expected_pid=$5
    case "$cycle" in 1 | 3) ;; *) die_usage ;; esac
    is_nonce "$nonce" || die_usage
    is_seconds "$timeout" || die_usage
    is_seconds "$setup_timeout" || die_usage
    is_uint "$expected_pid" || die_usage

    browser_identity
    original_pid=$pid
    status=124
    if [ "$original_pid" = "$expected_pid" ]; then
        PYTHONPYCACHEPREFIX=/run/asterinas-python-cache \
            nsenter -t "$original_pid" -n \
            /run/asterinas-tools/physical-graphics-gate \
            --nonce "$nonce" --cycle "$cycle" --firefox-pid "$original_pid" \
            --verify-final --timeout "$timeout" --setup-timeout "$setup_timeout"
        status=$?
    fi
    browser_identity
    [ "$pid" = "$original_pid" ] && [ "$restarts" = 0 ] || status=126
    printf '__ASTERINAS_PHYSICAL_FINAL_STATUS__ status=%s\n' "$status"
}

daily_use() {
    [ "$#" -eq 3 ] || die_usage
    experiment_id=$1
    timeout_seconds=$2
    expected_pid=$3
    is_experiment_id "$experiment_id" || die_usage
    is_profile_timeout "$timeout_seconds" || die_usage
    is_uint "$expected_pid" || die_usage
    [ "$expected_pid" -gt 1 ] || die_usage

    fixture_source='http://10.100.19.216:17894/asterinas-network-probe.bin'
    fixture_index='http://10.100.19.216:17894/browser-quality/index.html'
    upload_url="http://10.100.19.216:17894/browser-quality/daily-use-evidence/$experiment_id"
    evidence_dir="/run/asterinas-browser-daily-use-$experiment_id"
    gate_status=125
    upload_status=125
    outcome=fail

    browser_identity
    original_pid=$pid
    case "$original_pid" in
        '' | *[!0-9]*) gate_status=124 ;;
        *)
            if [ "$original_pid" != "$expected_pid" ] || [ "$original_pid" -le 1 ]; then
                gate_status=124
            elif [ -e "$evidence_dir" ] || [ -L "$evidence_dir" ]; then
                gate_status=123
            else
                manager_environment=$(systemctl_bounded show-environment 2>/dev/null || true)
                physical_mode=$(printf '%s\n' "$manager_environment" |
                    sed -n 's/^ASTERINAS_PHYSICAL_DAILY_USE=//p')
                configured_fixture=$(printf '%s\n' "$manager_environment" |
                    sed -n 's/^ASTERINAS_DESKTOP_FIXTURE_URL=//p')
                xorg_pids=$(pgrep -x Xorg 2>/dev/null || true)
                set -- $xorg_pids
                if [ "$#" -ne 1 ] || ! is_uint "$1"; then
                    gate_status=122
                elif [ "$physical_mode" != 1 ] || [ "$configured_fixture" != "$fixture_source" ]; then
                    gate_status=121
                else
                    xorg_pid=$1
                    PYTHONPYCACHEPREFIX=/run/asterinas-python-cache \
                        nsenter -t "$original_pid" -n \
                        /run/asterinas-tools/browser-daily-use-gate \
                        --firefox-pid "$original_pid" --xorg-pid "$xorg_pid" \
                        --fixture-index-url "$fixture_index" \
                        --evidence-dir "$evidence_dir" --mode profile --physical \
                        --timeout-seconds "$timeout_seconds"
                    gate_status=$?
                    [ "$gate_status" -ne 0 ] || outcome=pass
                    PYTHONPYCACHEPREFIX=/run/asterinas-python-cache \
                        nsenter -t "$original_pid" -n \
                        /run/asterinas-tools/browser-daily-use-upload \
                        "$evidence_dir" "$experiment_id" "$outcome" "$upload_url" \
                        --timeout 15
                    upload_status=$?
                fi
            fi
            ;;
    esac

    printf '__ASTERINAS_PHYSICAL_DAILY_USE__ experiment_id=%s outcome=%s gate_status=%s upload_status=%s\n' \
        "$experiment_id" "$outcome" "$gate_status" "$upload_status"
    [ "$outcome" = pass ] && [ "$gate_status" -eq 0 ] && [ "$upload_status" -eq 0 ]
}

action=${1-}
[ "$#" -ge 1 ] || die_usage
shift
case "$action" in
    preflight) [ "$#" -eq 0 ] || die_usage; preflight ;;
    start-browser) [ "$#" -eq 0 ] || die_usage; start_browser 1 ;;
    start-web) [ "$#" -eq 0 ] || die_usage; start_browser 0 ;;
    cycle) cycle "$@" ;;
    final) final "$@" ;;
    daily-use) daily_use "$@" ;;
    *) die_usage ;;
esac
