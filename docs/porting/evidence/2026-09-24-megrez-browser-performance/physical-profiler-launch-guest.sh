#!/bin/sh
set -eu

profile=/run/asterinas-profiler-profile
mkdir -p "$profile"
cat > "$profile/user.js" <<'EOF'
user_pref("marionette.port", 2829);
user_pref("network.proxy.type", 0);
user_pref("browser.newtabpage.enabled", false);
user_pref("dom.ipc.processCount", 1);
user_pref("fission.autostart", false);
user_pref("media.rdd-process.enabled", true);
user_pref("gfx.webrender.force-disabled", true);
EOF
chown -R asterinas:asterinas "$profile"
browser_pid=$(pgrep -xo firefox)
nsenter -t "$browser_pid" -n runuser -u asterinas -- \
  env HOME=/home/asterinas DISPLAY=:0 \
  XAUTHORITY=/home/asterinas/.Xauthority \
  MOZ_AVOID_OPENGL_ALTOGETHER=1 \
  /usr/bin/firefox --no-remote --new-instance --marionette \
  --remote-allow-system-access --profile "$profile" about:blank \
  >/run/asterinas-profiler-firefox.log 2>&1 &
launcher_pid=$!
printf '%s\n' "$launcher_pid" >/run/asterinas-profiler-firefox.launcher-pid
printf 'LAUNCHER_PID %s\n' "$launcher_pid"
for attempt in $(seq 1 75); do
  if nsenter -t "$browser_pid" -n python3 -c \
    'import socket; s=socket.create_connection(("127.0.0.1",2829),0.5); s.close()' \
    >/dev/null 2>&1; then
    printf 'MARIONETTE_READY attempt=%s\n' "$attempt"
    exit 0
  fi
  if ! kill -0 "$launcher_pid" 2>/dev/null; then
    tail -n 8 /run/asterinas-profiler-firefox.log
    exit 1
  fi
  sleep 1
done
tail -n 8 /run/asterinas-profiler-firefox.log
exit 1
