#!/usr/bin/env bash
# Cross-compile libcurl (static .a, no TLS) for the riscv64 desktop.
#
# NetSurf's GTK frontend includes <curl/curl.h> unconditionally (content/fetch.c),
# so the header is required even when the curl fetcher is compiled out. Build a
# minimal static libcurl WITHOUT OpenSSL — HTTP works, HTTPS is deferred to a
# later milestone (OpenSSL is not yet in the cross prefix). Installs curl/curl.h,
# libcurl.a and libcurl.pc into target/riscv-cross/usr.
#
#   deps present: zlib (compression), glibc (threaded resolver via pthread)
#   deps omitted: openssl, libpsl, libidn2, libssh2, nghttp2, brotli, zstd,
#                 ldap, rtmp, gssapi
set -euo pipefail

ROOT=/home/arch-anjie/Program/asterinas-riscv
PREFIX="$ROOT/target/riscv-cross/usr"
SRC="$ROOT/target/riscv-cross/src"
HOST=riscv64-linux-gnu
VER=8.14.1
JOBS="$(nproc)"

export CC="$HOST-gcc"
export PKG_CONFIG="$ROOT/target/riscv-cross/pkg-config-static"
export PKG_CONFIG_LIBDIR="$PREFIX/lib/pkgconfig:$PREFIX/share/pkgconfig"
export PKG_CONFIG_SYSROOT_DIR="$ROOT/target/riscv-cross"
export CFLAGS="-O2 -Wno-implicit-function-declaration -Wno-implicit-int -Wno-int-conversion -Wno-incompatible-pointer-types -Wno-return-mismatch"
export LDFLAGS="-L$PREFIX/lib"

cd "$SRC/curl-$VER"

if [ ! -f Makefile ]; then
    ./configure --host="$HOST" --prefix="$PREFIX" \
        --disable-shared --enable-static \
        --without-ssl \
        --without-libpsl --without-libidn2 --without-libssh2 \
        --without-nghttp2 --without-brotli --without-zstd --without-librtmp \
        --without-gssapi --without-libgsasl \
        --disable-ldap --disable-ldaps --disable-manual \
        2>&1 | tail -20
fi

make -j"$JOBS"
make install

echo "== result =="
for f in "$PREFIX/include/curl/curl.h" "$PREFIX/lib/libcurl.a" "$PREFIX/lib/pkgconfig/libcurl.pc"; do
    [ -f "$f" ] && echo "OK   $f" || echo "MISS $f"
done
