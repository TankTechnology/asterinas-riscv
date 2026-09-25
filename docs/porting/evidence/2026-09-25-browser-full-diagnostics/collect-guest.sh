#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
set -u
out=/run/asterinas-wave3
mkdir -p "$out"
{
  printf 'boot_id='; cat /proc/sys/kernel/random/boot_id
  printf 'uid='; id -u
  printf 'uptime='; cat /proc/uptime
  printf 'cmdline='; cat /proc/cmdline
  printf '\n-- units --\n'
  systemctl --failed --no-pager 2>&1
  printf '\n-- dbus service --\n'
  systemctl status dbus.service dbus.socket --no-pager -l 2>&1
  printf '\n-- service properties --\n'
  systemctl show dbus.service dbus.socket asterinas-browser-web.service asterinas-desktop-m5.service -p LoadState -p ActiveState -p SubState -p Result -p MainPID -p ExecMainStatus -p Requires -p Wants 2>&1
  printf '\n-- sockets --\n'
  ls -ld /run/dbus /run/dbus/system_bus_socket /run/user/1000/bus 2>&1
  printf '\n-- browser processes --\n'
  ps -eo pid,ppid,comm,%cpu,%mem --sort=-%cpu 2>&1 | head -n 40
} >"$out/system-status.txt" 2>&1
/usr/bin/timeout 30 /usr/bin/journalctl -b --no-pager -o short-monotonic >"$out/journal.log" 2>&1
printf '%s\n' "$?" >"$out/journal.exit"
/usr/bin/timeout 15 /usr/bin/dmesg >"$out/dmesg.log" 2>&1
printf '%s\n' "$?" >"$out/dmesg.exit"
/usr/bin/timeout 15 /usr/bin/journalctl -b --no-pager -o short-monotonic -u dbus.service -u dbus.socket -u asterinas-browser-web.service -u asterinas-desktop-m5.service >"$out/units.log" 2>&1
printf '%s\n' "$?" >"$out/units.exit"
for name in firefox-web-stderr.log firefox-web-mozilla.log Xorg.0.log desktop-m5-session.log browser-web-timeline.log; do
  if [ -f "/home/asterinas/$name" ]; then
    cp "/home/asterinas/$name" "$out/$name" || true
  fi
done
printf 'COLLECT boot='; cat /proc/sys/kernel/random/boot_id
printf 'COLLECT files:\n'; wc -c "$out"/* 2>/dev/null | tail -n 15
/usr/bin/tar -C /run -czf /run/asterinas-wave3.tgz asterinas-wave3
printf 'COLLECT archive='; sha256sum /run/asterinas-wave3.tgz
