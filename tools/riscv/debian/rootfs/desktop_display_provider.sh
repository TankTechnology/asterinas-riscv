#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

set -euo pipefail

provider="${ASTERINAS_DISPLAY_PROVIDER:-}"
readonly provider_root="${ASTERINAS_DISPLAY_PROVIDER_ROOT:-/etc/asterinas/display-providers}"
readonly legacy_fbdev_directory="${ASTERINAS_LEGACY_XORG_CONFIG_DIR:-/etc/X11/xorg.conf.d}"
readonly provider_runtime="${ASTERINAS_DISPLAY_PROVIDER_RUNTIME:-/run/asterinas/display-provider}"

# A successful DRM start may hand the session to the fbdev provider.  Keep
# that choice in /run so later desktop/browser services consume the same
# provider instead of re-reading the original kernel command line.  Ignore a
# missing runtime file; it is created lazily by the desktop owner.  A present
# but malformed file is an invalid handoff and fails closed.
if [[ -z "$provider" && -e "$provider_runtime" ]]; then
    [[ -f "$provider_runtime" && ! -L "$provider_runtime" ]] || exit 65
    runtime_provider=""
    IFS= read -r runtime_provider <"$provider_runtime" || exit 65
    [[ "$runtime_provider" == fbdev || "$runtime_provider" == drm ]] || exit 65
    [[ "$(wc -l <"$provider_runtime")" -eq 1 ]] || exit 65
    provider="$runtime_provider"
fi

if [[ -z "$provider" && -r /proc/cmdline ]]; then
    for word in $(</proc/cmdline); do
        case "$word" in
            asterinas.display_provider=fbdev|asterinas.display_provider=drm)
                provider="${word#*=}"
                ;;
        esac
    done
fi
provider="${provider:-fbdev}"
readonly provider

[[ "${1:-}" == --name && $# == 1 || $# == 0 ]] || exit 64

case "$provider" in
    fbdev | drm) ;;
    *) exit 64 ;;
esac

readonly config_directory="$provider_root/$provider/xorg.conf.d"
has_provider_config() {
    local directory="$1"
    local config="$directory/20-asterinas.conf"
    [[ "$directory" == /* && "$directory" != *$'\n'* ]] || return 1
    [[ -d "$directory" && ! -L "$directory" ]] || return 1
    [[ -f "$config" && ! -L "$config" ]] || return 1
    case "$provider" in
        fbdev)
            grep -Eq '^[[:space:]]*Driver[[:space:]]+"fbdev"[[:space:]]*$' \
                "$config" || return 1
            grep -Eq '^[[:space:]]*Option[[:space:]]+"fbdev"[[:space:]]+"/dev/fb0"[[:space:]]*$' \
                "$config" || return 1
            ;;
        drm)
            grep -Eq '^[[:space:]]*Driver[[:space:]]+"modesetting"[[:space:]]*$' \
                "$config" || return 1
            grep -Eq '^[[:space:]]*Option[[:space:]]+"kmsdev"[[:space:]]+"/dev/dri/card0"[[:space:]]*$' \
                "$config" || return 1
            ;;
        *) return 1 ;;
    esac
}

if has_provider_config "$config_directory"; then
    if [[ "${1:-}" == --name ]]; then
        printf '%s\n' "$provider"
    else
        printf '%s\n' "$config_directory"
    fi
elif [[ "$provider" == fbdev ]] && has_provider_config "$legacy_fbdev_directory"; then
    if [[ "${1:-}" == --name ]]; then
        printf '%s\n' "$provider"
    else
        printf '%s\n' "$legacy_fbdev_directory"
    fi
else
    exit 65
fi
