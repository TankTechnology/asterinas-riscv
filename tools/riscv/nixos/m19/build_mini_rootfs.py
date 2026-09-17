#!/usr/bin/env python3
"""Build a minimal virgl test rootfs from the Debian M19 rootfs.

Seeds from eglrender2 + the Mesa DRI/GBM/EGL driver files, recursively copies
their shared-library closure (plus the runtime-dlopen'd DRI/GBM/vendor files and
glvnd/driconf config), and adds busybox + eglrender2 + ioctltrace + an init.
Output is written to a target dir (default /tmp/mini-virgl).
"""
import os, shutil, subprocess, sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
SRC_ROOT = (REPO / "target/m19/rootfs").resolve()
DST_ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/mini-virgl").resolve()
TOOL_BUILD = REPO / "target/drm-m19/mini-tools"
LIB = "usr/lib/riscv64-linux-gnu"

def source_path(rel):
    path = (SRC_ROOT / rel).resolve(strict=True)
    try:
        path.relative_to(SRC_ROOT)
    except ValueError as error:
        raise ValueError(f"rootfs path escapes source tree: {rel}") from error
    return path

def soname_of(path):
    out = subprocess.run(["readelf", "-d", path], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if "SONAME" in line and "Library soname" in line:
            return line.split("[", 1)[1].split("]", 1)[0]
    return None

def needed_of(path):
    out = subprocess.run(["readelf", "-d", path], capture_output=True, text=True).stdout
    res = []
    for line in out.splitlines():
        if "NEEDED" in line and "Shared library:" in line:
            res.append(line.split("[", 1)[1].split("]", 1)[0])
    return res

def find_lib(soname):
    for d in (LIB, "usr/lib", "lib"):
        p = SRC_ROOT / d / soname
        if p.exists():
            return str(source_path(Path(d) / soname).relative_to(SRC_ROOT))
    return None

TOOL_BUILD.mkdir(parents=True, exist_ok=True)
gnucc = os.environ.get("RISC_V_GNU_CC", "riscv64-linux-gnu-gcc")
mesa_include = os.environ.get("MESA_HEADERS")
if mesa_include is None:
    mesa_include = str(REPO / "target/drm-m19/mesa-headers")
    os.makedirs(mesa_include, exist_ok=True)
    for header_dir in ("EGL", "GLES2", "KHR"):
        shutil.copytree(
            os.path.join("/usr/include", header_dir),
            os.path.join(mesa_include, header_dir),
            dirs_exist_ok=True,
        )
    shutil.copy2("/usr/include/gbm.h", os.path.join(mesa_include, "gbm.h"))

eglrender2 = TOOL_BUILD / "eglrender2"
ioctltrace = TOOL_BUILD / "ioctltrace.so"
subprocess.run([
    gnucc, "-O2", "-o", eglrender2,
    REPO / "tools/riscv/nixos/m19/eglrender2.c",
    f"-I{mesa_include}", f"-L{SRC_ROOT / LIB}",
    f"-Wl,-rpath-link,{SRC_ROOT / LIB}",
    SRC_ROOT / LIB / "libEGL.so.1",
    SRC_ROOT / LIB / "libGLESv2.so.2",
    SRC_ROOT / LIB / "libgbm.so.1",
], check=True)
subprocess.run([
    gnucc, "-O2", "-shared", "-fPIC", "-o", ioctltrace,
    REPO / "tools/riscv/nixos/m19/ioctltrace.c", "-ldl",
], check=True)

real_files = set()
visited = set()

def collect_library_closure(rel):
    rel = str(source_path(rel).relative_to(SRC_ROOT))
    if rel in visited:
        return
    visited.add(rel)
    real_files.add(rel)
    for lib in needed_of(source_path(rel)):
        if lib.startswith("ld-linux"):
            continue
        library_path = find_lib(lib)
        if library_path:
            collect_library_closure(library_path)

def collect_tool_dependencies(path):
    for library in needed_of(path):
        if not library.startswith("ld-linux"):
            library_path = find_lib(library)
            if library_path:
                collect_library_closure(library_path)

gallium_libraries = list((SRC_ROOT / LIB).glob("libgallium-*.so"))
if len(gallium_libraries) != 1:
    raise RuntimeError(f"expected one Mesa gallium library, found {gallium_libraries}")

for seed_path in [
    "usr/bin/modetest",
    "usr/bin/kmscube",
    f"{LIB}/dri/libdril_dri.so",
    f"{LIB}/gbm/dri_gbm.so",
    str(gallium_libraries[0].relative_to(SRC_ROOT)),
    f"{LIB}/libEGL_mesa.so.0.0.0",
]:
    collect_library_closure(seed_path)
collect_tool_dependencies(eglrender2)
collect_tool_dependencies(ioctltrace)

loader_real = f"{LIB}/ld-linux-riscv64-lp64d.so.1"
if (SRC_ROOT / loader_real).exists():
    real_files.add(loader_real)

if DST_ROOT.exists():
    shutil.rmtree(DST_ROOT)
DST_ROOT.mkdir(parents=True)

def copy(rel):
    src = source_path(rel)
    dst = DST_ROOT / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)

for rel in sorted(real_files):
    copy(rel)

for rel in sorted(real_files):
    b = os.path.basename(rel)
    if ".so" in b:
        so = soname_of(source_path(rel))
        if so and so != b:
            link = DST_ROOT / os.path.dirname(rel) / so
            if not os.path.lexists(link):
                os.symlink(b, link)

shutil.rmtree(DST_ROOT / LIB / "dri", ignore_errors=True)
shutil.copytree(source_path(Path(LIB) / "dri"), DST_ROOT / LIB / "dri", symlinks=True)
shutil.rmtree(DST_ROOT / LIB / "gbm", ignore_errors=True)
shutil.copytree(source_path(Path(LIB) / "gbm"), DST_ROOT / LIB / "gbm", symlinks=True)
for share in ("usr/share/glvnd", "usr/share/drirc.d"):
    shutil.rmtree(DST_ROOT / share, ignore_errors=True)
    shutil.copytree(source_path(share), DST_ROOT / share, symlinks=True)

os.makedirs(DST_ROOT / "lib", exist_ok=True)
link = DST_ROOT / "lib/ld-linux-riscv64-lp64d.so.1"
if not os.path.lexists(link):
    os.symlink("../" + loader_real, link)

os.makedirs(DST_ROOT / "bin", exist_ok=True)
shutil.copy2(source_path("usr/bin/busybox"), DST_ROOT / "bin/busybox")
os.makedirs(DST_ROOT / "root", exist_ok=True)
shutil.copy2(eglrender2, DST_ROOT / "root/eglrender2")
shutil.copy2(ioctltrace, DST_ROOT / "root/ioctltrace.so")
muslcc = os.environ.get("RISC_V_MUSL_CC", "riscv64-linux-musl-gcc")
subprocess.run([
    muslcc, "-O2", "-static", "-o", DST_ROOT / "root/virgltest",
    str(REPO / "tools/riscv/nixos/m16/virgltest.c"),
], check=True)
subprocess.run([
    muslcc, "-O2", "-static", "-pthread", "-o",
    DST_ROOT / "root/primetest",
    str(REPO / "tools/riscv/nixos/m19/primetest.c"),
], check=True)
subprocess.run([
    muslcc, "-O2", "-static", "-pthread", "-o",
    DST_ROOT / "root/syncobjtest",
    str(REPO / "tools/riscv/nixos/m19/syncobjtest.c"),
], check=True)

init = """#!/bin/busybox sh
/bin/busybox mount -t devtmpfs devtmpfs /dev
/bin/busybox mount -t proc proc /proc
/bin/busybox mkdir -p /sys
/bin/busybox mount -t sysfs sysfs /sys
echo MINI_VIRGL_START
ls -l /dev/dri 2>&1
echo "SYSFS_VENDOR=$(/bin/busybox cat /sys/dev/char/226:0/device/vendor 2>&1)"
echo "SYSFS_DEVICE=$(/bin/busybox cat /sys/dev/char/226:0/device/device 2>&1)"
echo MINI_MODETEST_ENUM_BEGIN
/usr/bin/modetest -M virtio_gpu -c -e -p 2>&1
echo "MINI_MODETEST_ENUM_RC=$?"
echo MINI_MODETEST_LEGACY_BEGIN
echo | /usr/bin/modetest -M virtio_gpu -s '2@1:#0' 2>&1
echo "MINI_MODETEST_LEGACY_RC=$?"
echo MINI_MODETEST_ATOMIC_BEGIN
echo | /usr/bin/modetest -M virtio_gpu -a -s '2@1:#0' 2>&1
echo "MINI_MODETEST_ATOMIC_RC=$?"
export EGL_LOG_LEVEL=warning
export LIBGL_DEBUG=verbose
echo MINI_KMSCUBE_LEGACY_BEGIN
LD_PRELOAD=/root/ioctltrace.so /usr/bin/kmscube -D /dev/dri/card0 -c 5 2>&1
echo "MINI_KMSCUBE_LEGACY_RC=$?"
echo MINI_KMSCUBE_ATOMIC_BEGIN
LD_PRELOAD=/root/ioctltrace.so /usr/bin/kmscube -A -D /dev/dri/card0 -c 5 2>&1
echo "MINI_KMSCUBE_ATOMIC_RC=$?"
echo MINI_PUBLIC_KMS_DONE
echo MINI_PRIME_BEGIN
/root/primetest 2>&1
echo "MINI_PRIME_RC=$?"
echo MINI_SYNCOBJ_BEGIN
/root/syncobjtest 2>&1
echo "MINI_SYNCOBJ_RC=$?"
echo MINI_RAW_BEGIN
LD_PRELOAD=/root/ioctltrace.so /root/virgltest 2>&1
echo "MINI_RAW_RC=$?"
echo MINI_RAW_DONE
echo MINI_EGL_BEGIN
LD_PRELOAD=/root/ioctltrace.so /root/eglrender2 2>&1
echo "MINI_EGL_RC=$?"
echo MINI_EGL_DONE
exec /bin/busybox sh
"""
os.makedirs(DST_ROOT / "etc", exist_ok=True)
with open(DST_ROOT / "init", "w") as f:
    f.write(init)
os.chmod(DST_ROOT / "init", 0o755)

total = 0
for dp, _, fs in os.walk(DST_ROOT):
    for f in fs:
        p = os.path.join(dp, f)
        if not os.path.islink(p):
            total += os.path.getsize(p)
print(f"built {DST_ROOT}: {len(real_files)} real files, {total/1024/1024:.1f} MiB")
