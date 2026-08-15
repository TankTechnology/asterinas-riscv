#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# BROWSER-M12 TLS certificate-matrix harness.
#
# Tests how the guest's TLS client (the standalone curl binary, which shares its
# libcurl fetcher with NetSurf) and NetSurf itself behave against a set of HTTPS
# endpoints whose certificates exercise each validation decision point: valid,
# expired, wrong-hostname, and untrusted self-signed. The endpoints are served on
# the host loopback and reached from the guest through QEMU slirp user networking
# (guest 10.0.2.2 == host 127.0.0.1), so no external network is involved.
#
# Layout (mirrors render_matrix_net.sh):
#   1. generate the cert matrix (gen_tls_certs.py)
#   2. build the base rootfs once (build_systemd_desktop.sh --no-pack; the build
#      script now also installs the standalone curl binary)
#   3. post-process the rootfs: append the test CA to the guest CA bundle, install
#      the test-CA-only bundle, and drop in curl-cert-test.{service,sh} so every
#      boot also runs the curl matrix
#   4. serve the endpoints (tls_cert_server.py) and wait until they are listening
#   5. boot each case (net_validate.sh re-packs an independent boot disk + boots)
#   6. summarize per-case serial-log score + server-side handshake transcript
#
# Env knobs: OUT_DIR (default /tmp/browser-m12), CERT_DIR (/tmp/tls-certs),
#            SETTLE (150), LOGLEVEL (warn).

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "${REPO_ROOT}"

SRC_DIR="${REPO_ROOT}/tools/riscv/systemd"
XORG_DIR="${REPO_ROOT}/tools/riscv/xorg"
BUILD_SCRIPT="${SRC_DIR}/build_systemd_desktop.sh"
NET_VALIDATE="${XORG_DIR}/net_validate.sh"
PIXEL_VALIDATE="${XORG_DIR}/pixel_validate.py"
ROOTFS="${REPO_ROOT}/target/systemd-desktop/rootfs"

OUT_DIR="${OUT_DIR:-/tmp/browser-m12}"
CERT_DIR="${CERT_DIR:-/tmp/tls-certs}"
SETTLE="${SETTLE:-150}"
LOGLEVEL="${LOGLEVEL:-warn}"
PORT_BASE="${PORT_BASE:-8443}"
HOST_IP="10.0.2.2"

die() { echo "tls_cert_matrix: $*" >&2; exit 2; }

# name|url — one entry per case. The `curl` case leaves NETSURF_URL unset so
# netsurf.service falls back to its bundled local home page; the point there is
# the curl-cert-test.service output, not NetSurf. The netsurf-* cases point
# NetSurf at one bad/valid endpoint each.
ALL_CASES=(
  "curl|"
  "netsurf-valid|https://${HOST_IP}:$((PORT_BASE+0))/"
  "netsurf-expired|https://${HOST_IP}:$((PORT_BASE+1))/"
  "netsurf-wronghost|https://${HOST_IP}:$((PORT_BASE+2))/"
  "netsurf-selfsigned|https://${HOST_IP}:$((PORT_BASE+3))/"
)

declare -a CASES
if [ "$#" -gt 0 ]; then
    for want in "$@"; do
        for entry in "${ALL_CASES[@]}"; do
            [ "${entry%%|*}" = "$want" ] && CASES+=("$entry")
        done
    done
else
    CASES=("${ALL_CASES[@]}")
fi
[ ${#CASES[@]} -gt 0 ] || die "no matching cases"

command -v qemu-system-riscv64 >/dev/null || die "qemu-system-riscv64 not found"
[ -x "$BUILD_SCRIPT" ] || die "missing $BUILD_SCRIPT"
[ -x "$NET_VALIDATE" ] || die "missing $NET_VALIDATE"

mkdir -p "$OUT_DIR"
rm -f "$OUT_DIR/results.txt"

# 1. Generate certs.
echo "[certs] generating cert matrix into $CERT_DIR"
python3 "$XORG_DIR/gen_tls_certs.py" "$CERT_DIR" > "$OUT_DIR/gen-certs.log" 2>&1 \
    || die "cert generation failed (see $OUT_DIR/gen-certs.log)"

# 2. Base rootfs once (curl is installed by the build script itself).
echo "[base] assembling base rootfs (--no-pack)"
NETSURF_URL= bash "$BUILD_SCRIPT" --no-pack > "$OUT_DIR/base-build.log" 2>&1 \
    || die "base rootfs build failed (see $OUT_DIR/base-build.log)"

# 3. Post-process rootfs: trust the test CA + install the curl-cert-test unit
#    (the unit runs the curl matrix via 13 inline ExecStart= lines — no shell
#    script, because the desktop busybox has no echo/[/test builtins).
#    The bundled CA file is copied read-only (host mode 444), so make it
#    writable before appending the test CA.
echo "[base] post-processing rootfs (test CA + curl-cert-test)"
chmod u+w "$ROOTFS/etc/ssl/certs/ca-certificates.crt"
cat "$CERT_DIR/ca.crt" >> "$ROOTFS/etc/ssl/certs/ca-certificates.crt"
cp "$CERT_DIR/ca.crt" "$ROOTFS/etc/ssl/certs/test-ca.crt"
cp "$SRC_DIR/units/curl-cert-test.service" "$ROOTFS/etc/systemd/system/curl-cert-test.service"
mkdir -p "$ROOTFS/etc/systemd/system/graphical.target.wants"
ln -sf ../curl-cert-test.service "$ROOTFS/etc/systemd/system/graphical.target.wants/curl-cert-test.service"

# 4. Serve the endpoints; wait until all four are listening. The server prints
#    both its [serve] startup lines and its per-connection [tls] handshake
#    transcript to stdout (redirected to tls-server.out).
echo "[serve] starting TLS server ($PORT_BASE..$((PORT_BASE+3)))"
python3 "$XORG_DIR/tls_cert_server.py" "$CERT_DIR" \
    --port-base "$PORT_BASE" \
    > "$OUT_DIR/tls-server.out" 2>&1 &
SERVER_PID=$!
trap 'kill "$SERVER_PID" 2>/dev/null || true' EXIT
for _ in $(seq 1 50); do
    up="$(grep -c '^\[serve\]' "$OUT_DIR/tls-server.out" 2>/dev/null || true)"
    [ "$up" -ge 4 ] && break
    sleep 0.2
done
[ "$(grep -c '^\[serve\]' "$OUT_DIR/tls-server.out" 2>/dev/null || true)" -ge 4 ] \
    || die "TLS server did not come up (see $OUT_DIR/tls-server.out)"

prepare_case() {
    local name="$1" url="$2" dir initrd
    dir="$OUT_DIR/$name"
    initrd="$dir/initramfs.cpio"
    mkdir -p "$dir"
    if [ -n "$url" ]; then
        printf 'NETSURF_URL=%s\n' "$url" > "$ROOTFS/etc/netsurf.conf"
    else
        rm -f "$ROOTFS/etc/netsurf.conf"
    fi
    ( cd "$ROOTFS" && find . | cpio -o -H newc > "$initrd" )
    local bytes; bytes="$(wc -c < "$initrd")"
    if [ "$bytes" -lt 1000000 ]; then
        echo "[prepare] $name FAILED: initramfs $bytes bytes (cpio failed — /tmp full?)" >&2
        return 1
    fi
    echo "[prepare] $name -> ${url:-<local home>} ($bytes bytes)"
}

# Score a case from its serial log. For the curl case: report the TLS_TEST lines.
# For netsurf-*: http200 + redraw => rendered; "Building certificate viewer"
# (sslcert_viewer_init) => cert-blocked (NetSurf's cert dialog, page not
# rendered). Note the favicon fetch (http://www.google.com/favicon.ico) code7s
# on this host, so a bare code7 line is NOT a reliable transport-error signal
# (M10 §3.4); the cert-dialog marker is.
score_case() {
    local name="$1" log="$2"
    if [ "$name" = "curl" ]; then
        grep -a '^TLS_TEST' "$log" || true
        return 0
    fi
    local nav http200 redraw certviewer code7 code56 code6 code28
    nav="$(grep -qa 'browser_window_navigate' "$log" && echo yes || echo no)"
    http200="$(grep -qa 'HTTP status code 200' "$log" && echo yes || echo no)"
    redraw="$(grep -qa 'content_scaled_redraw' "$log" && echo yes || echo no)"
    certviewer="$(grep -qa 'Building certificate viewer' "$log" && echo yes || echo no)"
    code7="$(grep -qa 'Unknown cURL response code 7' "$log" && echo yes || echo no)"
    code56="$(grep -qa 'Unknown cURL response code 56' "$log" && echo yes || echo no)"
    code6="$(grep -qa 'Unknown cURL response code 6' "$log" && echo yes || echo no)"
    code28="$(grep -qa 'Unknown cURL response code 28' "$log" && echo yes || echo no)"
    if [ "$http200" = yes ] || [ "$redraw" = yes ]; then
        echo "[score] $name: rendered (http200=$http200 redraw=$redraw)"
    elif [ "$certviewer" = yes ]; then
        echo "[score] $name: cert-blocked (certificate viewer shown, no render)"
    else
        echo "[score] $name: other (nav=$nav http200=$http200 redraw=$redraw certviewer=$certviewer code7=$code7 code56=$code56 code6=$code6 code28=$code28)"
    fi
}

boot_case() {
    local name="$1"
    local dir="$OUT_DIR/$name"
    echo "[boot] $name starting"
    OUT_DIR="$OUT_DIR" bash "$NET_VALIDATE" "$name" "$dir/initramfs.cpio" "$SETTLE" "$LOGLEVEL" \
        > "$dir/net_validate.out" 2>&1
    rm -f "$dir/boot.ext4" "$dir/mon.sock"
    score_case "$name" "$dir/serial.log" | tee -a "$OUT_DIR/results.txt"
    if [ "$name" != "curl" ] && [ -s "$dir/shot.ppm" ]; then
        python3 "$PIXEL_VALIDATE" "$dir/shot.ppm" > "$dir/pixel.out" 2>&1
        echo "[pixel] $name: $(cat "$dir/pixel.out")"
    fi
    echo "[boot] $name done"
}

echo "=== BROWSER-M12 TLS cert matrix: ${#CASES[@]} case(s) ==="

for entry in "${CASES[@]}"; do
    name="${entry%%|*}"; url="${entry#*|}"
    prepare_case "$name" "$url" || die "prepare failed for $name"
done

# Boot sequentially (PARALLEL=1: concurrent render guests interfere — M10 §3.5).
for entry in "${CASES[@]}"; do
    name="${entry%%|*}"
    boot_case "$name"
done

echo ""
echo "=== BROWSER-M12 TLS cert matrix results ==="
if [ -s "$OUT_DIR/results.txt" ]; then cat "$OUT_DIR/results.txt"; fi
echo ""
echo "=== server-side TLS handshake transcript ($OUT_DIR/tls-server.out) ==="
if [ -s "$OUT_DIR/tls-server.out" ]; then grep -a '^\[tls\]' "$OUT_DIR/tls-server.out"; fi
echo "Each dir: serial.log, shot.ppm (netsurf-*), net_validate.out."
