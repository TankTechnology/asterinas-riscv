#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
#
# Notice a boot that followed an unclean shutdown.
#
# The root filesystem is ext2: a crash while it is being written can leave
# directory entries whose inodes are gone, and those names cannot even be
# removed until an offline fsck runs. The kernel gives no warning at the next
# boot, so the evidence has to be kept on disk: this unit writes `running` when
# the system comes up and `clean` when it goes down. Finding `running` at boot
# means the previous boot did not shut down, and that is what the console line
# below is for -- the smoke test and anyone reading the log can see it.
#
# The check itself cannot repair the filesystem: fsck only works on an
# unmounted root. What this buys is knowing when a repair is owed.

set -u

state_dir="${ASTERINAS_BOOT_STATE_DIR:-/var/lib/asterinas}"
state_file="$state_dir/boot-state"

awkward() {
    printf '%s\n' "$1" >"$state_file" 2>/dev/null || true
}

case "${1:-start}" in
start)
    previous=""
    [ -r "$state_file" ] && previous="$(tr -d '[:space:]' <"$state_file")"
    case "$previous" in
    clean | "")
        ;;
    *)
        # journald is not available on this image, so this has to reach the
        # console directly.
        printf 'ASTERINAS_UNCLEAN_SHUTDOWN previous_state=%s\n' "$previous" >/dev/console 2>/dev/null ||
            printf 'ASTERINAS_UNCLEAN_SHUTDOWN previous_state=%s\n' "$previous"
        ;;
    esac
    mkdir -p "$state_dir" 2>/dev/null || true
    awkward running
    ;;
stop)
    awkward clean
    ;;
*)
    echo "usage: $0 [start|stop]" >&2
    exit 2
    ;;
esac
