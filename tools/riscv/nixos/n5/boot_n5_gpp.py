#!/usr/bin/env python3
import subprocess, os, selectors, signal, time, re

REPO = "/home/arch-anjie/Program/asterinas-riscv-nixos"
UBOOT = f"{REPO}/target/qemu-uboot/cache/u-boot-build/u-boot"
BOOT_DISK = "/home/arch-anjie/Program/asterinas-riscv-nixos/target/n5-disks/boot.ext4"
ROOT_DISK = "/home/arch-anjie/Program/asterinas-riscv-nixos/target/n5-disks/root.ext2"
K, I, D = 0x80200000, 0x83000000, 0x88000000

cmds = [
    ("virtio scan", "=>"),
    (f"ext4load virtio 0:0 {K:#x} /asterinas.booti", "bytes read"),
    (f"ext4load virtio 0:0 {D:#x} /qemu-virt.dtb", "bytes read"),
    (f"fdt addr {D:#x}", "Working FDT set"),
    ('setenv bootargs "console=ttyS0 loglevel=warn init=/init"', "=>"),
    (f"ext4load virtio 0:0 {I:#x} /initramfs.cpio.gz", "bytes read"),
    ("setenv initrd_size ${filesize}", "=>"),
    (f"booti {K:#x} {I:#x}:${{initrd_size}} {D:#x}", "Starting kernel ..."),
]

argv = ["qemu-system-riscv64", "-machine", "virt",
        "-cpu", "rv64,sv48=false,svpbmt=true,zkr=true,svadu=false,svade=true",
        "-m", "2G", "-smp", "1", "-display", "none", "-monitor", "none",
        "-serial", "stdio", "-no-reboot", "-kernel", UBOOT,
        "-drive", f"if=none,format=raw,file={BOOT_DISK},id=bootdisk",
        "-device", "virtio-blk-device,drive=bootdisk",
        "-drive", f"if=none,format=raw,file={ROOT_DISK},id=rootdisk",
        "-device", "virtio-blk-device,drive=rootdisk",
        "-netdev", "user,id=n0", "-device", "virtio-net-device,netdev=n0"]

p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                     stderr=subprocess.STDOUT, start_new_session=True)
sel = selectors.DefaultSelector(); sel.register(p.stdout, selectors.EVENT_READ)
buf = bytearray(); transcript = bytearray()

def read_until(needle, timeout):
    nb = needle if isinstance(needle, bytes) else needle.encode()
    deadline = time.monotonic() + timeout
    while nb not in buf:
        rem = deadline - time.monotonic()
        if rem <= 0: raise TimeoutError(f"timeout {needle!r}")
        for key, _ in sel.select(min(rem, 0.1)):
            c = os.read(key.fileobj.fileno(), 65536)
            if not c: raise RuntimeError("closed")
            transcript.extend(c); buf.extend(c)
    i = buf.index(nb); del buf[:i+len(nb)]

def drain(seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        for key, _ in sel.select(0.1):
            c = os.read(key.fileobj.fileno(), 65536)
            if not c: return
            transcript.extend(c)

def send(s):
    p.stdin.write((s+"\n").encode()); p.stdin.flush()

try:
    read_until("=> ", 60)
    for text, exp in cmds:
        send(text); read_until(exp, 40)
        if exp != "=>":
            try: read_until("=> ", 30)
            except TimeoutError: pass
    try: read_until("login:", 180)
    except TimeoutError: pass
    send("root")
    try: read_until("#", 30)
    except TimeoutError: pass
    drain(1)

    send("export PATH=/usr/local/riscv64-gcc/bin:$PATH"); drain(1)
    send("cat > /tmp/t.cpp <<'EOF'\n#include <iostream>\nint main(){ std::cout << \"CPP_HELLO\" << std::endl; return 0; }\nEOF"); drain(2)
    send("g++ /tmp/t.cpp -o /tmp/t 2>&1; echo GPP_RC=$?"); drain(150)
    send("/tmp/t; echo RUN_RC=$?"); drain(20)
finally:
    try: os.killpg(p.pid, signal.SIGKILL)
    except Exception: pass

clean = re.sub(rb"\x1b\[[0-9;]*m", b"", bytes(transcript)).decode("utf-8", "replace")
print(clean[-2200:])
