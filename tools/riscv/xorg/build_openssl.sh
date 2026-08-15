#!/usr/bin/env bash
# Cross-compile OpenSSL 3.0 (static libssl.a + libcrypto.a) for the riscv64 desktop.
#
# This is the missing dependency for NetSurf's HTTPS fetch: NetSurf's curl
# fetcher (content/fetchers/curl.c) includes <openssl/ssl.h> unconditionally and
# calls SSL_CTX_* directly, and libcurl needs OpenSSL for TLS. Without it the
# curl fetcher must stay compiled out (file://-only, the M1 state).
#
# Installs libssl.a/libcrypto.a, the headers, and openssl.pc/libssl.pc/
# libcrypto.pc into target/riscv-cross/usr so that both libcurl's configure
# (--with-openssl) and NetSurf's pkg_config_find_and_add_enabled(OPENSSL,openssl)
# can discover it via pkg-config.
#
#   linux64-riscv64: upstream OpenSSL target for riscv64-linux-gnu
#   no-shared      : static .a only (matches the rest of the desktop stack)
#   no-asm         : pure-C build (avoids RISC-V asm/cross-march pitfalls)
#   no-tests       : skip test binaries (broken under cross anyway)
set -euo pipefail

ROOT=/home/arch-anjie/Program/asterinas-riscv
PREFIX="$ROOT/target/riscv-cross/usr"
SRC="$ROOT/target/riscv-cross/src"
HOST=riscv64-linux-gnu
VER=3.0.15
JOBS="$(nproc)"

# NOTE: do NOT export CC/AR/RANLIB here. OpenSSL derives them from the
# --cross-compile-prefix below; setting CC too makes it double the prefix
# (riscv64-linux-gnu-riscv64-linux-gnu-gcc).

cd "$SRC"

# Fetch from the Debian pool (fast CDN). openssl.org redirects to GitHub, which
# is ~20 KB/s from this host and stalls on the ~15 MB release asset; the Debian
# .orig.tar.gz is the pristine upstream tarball and extracts to openssl-$VER/.
if [ ! -d "openssl-$VER" ]; then
    URL="https://deb.debian.org/debian/pool/main/o/openssl/openssl_${VER}.orig.tar.gz"
    curl -sL --retry 3 --max-time 300 -o "openssl-${VER}.tar.gz" "$URL"
    gzip -t "openssl-${VER}.tar.gz"
    tar xzf "openssl-${VER}.tar.gz"
fi

cd "$SRC/openssl-$VER"

if [ ! -f Makefile ]; then
    # --openssldir is the *runtime* cert/config location in the guest; NetSurf
    # overrides the CA bundle via CURLOPT_CAINFO anyway, so this just needs to be
    # a conventional path (not the cross prefix).
    ./Configure linux64-riscv64 \
        --prefix="$PREFIX" \
        --openssldir=/etc/ssl \
        --cross-compile-prefix="$HOST-" \
        no-shared no-asm no-tests
fi

make -j"$JOBS"
make install_sw

echo "== result =="
rc=0
for f in "$PREFIX/lib/libssl.a" "$PREFIX/lib/libcrypto.a" \
         "$PREFIX/include/openssl/ssl.h" \
         "$PREFIX/lib/pkgconfig/openssl.pc"; do
    if [ -f "$f" ]; then
        echo "OK   $f"
    else
        echo "MISS $f"
        rc=1
    fi
done
# NetSurf's pkg_config_find_and_add looks up the `openssl` module specifically.
if "$ROOT/target/riscv-cross/pkg-config-static" --exists openssl; then
    echo "OK   pkg-config finds openssl"
else
    echo "MISS pkg-config openssl module"
    rc=1
fi
exit "$rc"
