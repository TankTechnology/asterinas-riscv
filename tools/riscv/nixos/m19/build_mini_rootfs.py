#!/usr/bin/env python3
"""Build a minimal virgl test rootfs from the Debian M19 rootfs.

Seeds from eglrender2 + the Mesa DRI/GBM/EGL driver files, recursively copies
their shared-library closure (plus the runtime-dlopen'd DRI/GBM/vendor files and
glvnd/driconf config), and adds busybox + eglrender2 + ioctltrace + an init.
Output is written to a target dir (default /tmp/mini-virgl).
"""
import os, subprocess, shutil, sys

SRC = "/home/arch-anjie/Program/asterinas-riscv-drm/target/m19/rootfs"
DST = sys.argv[1] if len(sys.argv) > 1 else "/tmp/mini-virgl"
LIB = "usr/lib/riscv64-linux-gnu"

def abspath(rel):
    return os.path.join(SRC, rel)

def soname_of(rel):
    out = subprocess.run(["readelf", "-d", abspath(rel)], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if "SONAME" in line and "Library soname" in line:
            return line.split("[", 1)[1].split("]", 1)[0]
    return None

def needed_of(rel):
    out = subprocess.run(["readelf", "-d", abspath(rel)], capture_output=True, text=True).stdout
    res = []
    for line in out.splitlines():
        if "NEEDED" in line and "Shared library:" in line:
            res.append(line.split("[", 1)[1].split("]", 1)[0])
    return res

def find_lib(soname):
    for d in (LIB, "usr/lib", "lib"):
        p = os.path.join(SRC, d, soname)
        if os.path.exists(p):
            return os.path.relpath(p, SRC)
    return None

real_files = set()
visited = set()

def walk(rel):
    if os.path.islink(abspath(rel)):
        rel = os.path.relpath(os.path.realpath(abspath(rel)), SRC)
    if rel in visited:
        return
    visited.add(rel)
    real_files.add(rel)
    for lib in needed_of(rel):
        if lib.startswith("ld-linux"):
            continue
        lp = find_lib(lib)
        if lp:
            walk(lp)

for s in [
    "root/eglrender2",
    f"{LIB}/dri/libdril_dri.so",
    f"{LIB}/gbm/dri_gbm.so",
    f"{LIB}/libgallium-25.0.7-2+deb13u1.so",
    f"{LIB}/libEGL_mesa.so.0.0.0",
]:
    walk(s)

loader_real = f"{LIB}/ld-linux-riscv64-lp64d.so.1"
if os.path.exists(abspath(loader_real)):
    real_files.add(loader_real)

if os.path.exists(DST):
    shutil.rmtree(DST)
os.makedirs(DST)

def copy(rel):
    src = abspath(rel)
    dst = os.path.join(DST, rel)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)

for rel in sorted(real_files):
    copy(rel)

for rel in sorted(real_files):
    b = os.path.basename(rel)
    if ".so" in b and not os.path.islink(abspath(rel)):
        so = soname_of(rel)
        if so and so != b:
            link = os.path.join(DST, os.path.dirname(rel), so)
            if not os.path.lexists(link):
                os.symlink(b, link)

shutil.rmtree(os.path.join(DST, LIB, "dri"), ignore_errors=True)
shutil.copytree(os.path.join(SRC, LIB, "dri"), os.path.join(DST, LIB, "dri"), symlinks=True)
shutil.rmtree(os.path.join(DST, LIB, "gbm"), ignore_errors=True)
shutil.copytree(os.path.join(SRC, LIB, "gbm"), os.path.join(DST, LIB, "gbm"), symlinks=True)
for share in ("usr/share/glvnd", "usr/share/drirc.d"):
    shutil.rmtree(os.path.join(DST, share), ignore_errors=True)
    shutil.copytree(os.path.join(SRC, share), os.path.join(DST, share), symlinks=True)

os.makedirs(os.path.join(DST, "lib"), exist_ok=True)
link = os.path.join(DST, "lib", "ld-linux-riscv64-lp64d.so.1")
if not os.path.lexists(link):
    os.symlink("../" + loader_real, link)

os.makedirs(os.path.join(DST, "bin"), exist_ok=True)
shutil.copy2(os.path.join(SRC, "usr/bin/busybox"), os.path.join(DST, "bin/busybox"))
os.makedirs(os.path.join(DST, "root"), exist_ok=True)
shutil.copy2(os.path.join(SRC, "root/eglrender2"), os.path.join(DST, "root/eglrender2"))
shutil.copy2(os.path.join(SRC, "root/virgltest"), os.path.join(DST, "root/virgltest"))
shutil.copy2(os.path.join(SRC, "root/ioctltrace.so"), os.path.join(DST, "root/ioctltrace.so"))

init = """#!/bin/busybox sh
/bin/busybox mount -t devtmpfs devtmpfs /dev
/bin/busybox mount -t proc proc /proc
/bin/busybox mkdir -p /sys
/bin/busybox mount -t sysfs sysfs /sys
echo MINI_VIRGL_START
ls -l /dev/dri 2>&1
echo "SYSFS_VENDOR=$(/bin/busybox cat /sys/dev/char/226:0/device/vendor 2>&1)"
echo "SYSFS_DEVICE=$(/bin/busybox cat /sys/dev/char/226:0/device/device 2>&1)"
echo MINI_RAW_BEGIN
LD_PRELOAD=/root/ioctltrace.so /root/virgltest 2>&1
echo "MINI_RAW_RC=$?"
export EGL_LOG_LEVEL=debug
export LIBGL_DEBUG=verbose
echo MINI_EGL_BEGIN
LD_PRELOAD=/root/ioctltrace.so /root/eglrender2 2>&1
echo "MINI_EGL_RC=$?"
exec /bin/busybox sh
"""
os.makedirs(os.path.join(DST, "etc"), exist_ok=True)
with open(os.path.join(DST, "init"), "w") as f:
    f.write(init)
os.chmod(os.path.join(DST, "init"), 0o755)

total = 0
for dp, _, fs in os.walk(DST):
    for f in fs:
        p = os.path.join(dp, f)
        if not os.path.islink(p):
            total += os.path.getsize(p)
print(f"built {DST}: {len(real_files)} real files, {total/1024/1024:.1f} MiB")
