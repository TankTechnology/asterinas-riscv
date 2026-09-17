#!/usr/bin/env python3
"""Boot DRM kernel with correct QEMU args, M16 initramfs verification."""
import os, sys, pty, time, subprocess, select

BOOTDISK = "/tmp/drm-m16/boot.ext4"
UBOOT = "/tmp/drm-m16/u-boot"

def run():
    argv = [
        "qemu-system-riscv64",
        "-machine", "virt",
        "-cpu", "rv64,sv48=false,svpbmt=true,zkr=true,svadu=false,svade=true",
        "-m", "1G",
        "-smp", "1",
        "-display", "none",
        "-monitor", "none",
        "-serial", "stdio",
        "-no-reboot",
        "-kernel", UBOOT,
        "-drive", "if=none,format=raw,file=/tmp/drm-m16/boot.ext4,id=bootdisk",
        "-device", "virtio-blk-device,drive=bootdisk",
        "-device", "virtio-gpu-device",
    ]

    cmds = [
        (b"virtio scan", b"=>", 30),
        (b"ext4ls virtio 0:0 /", b"asterinas.booti", 10),
        (b"ext4load virtio 0:0 0x80200000 /asterinas.booti", b"bytes read", 10),
        (b"setenv aster_size ${filesize}", b"=>", 5),
        (b"ext4load virtio 0:0 0x88000000 /qemu-virt.dtb", b"bytes read", 10),
        (b"fdt addr 0x88000000", b"Working FDT set", 5),
        (b"fdt resize 0x1000", b"=>", 5),
        (b'setenv bootargs "console=ttyS0 loglevel=info init=/init"', b"=>", 5),
        (b'fdt set /chosen bootargs "console=ttyS0 loglevel=info init=/init"', b"=>", 5),
        (b"ext4load virtio 0:0 0x83000000 /initramfs.cpio.gz", b"bytes read", 10),
        (b"setenv initrd_size ${filesize}", b"=>", 5),
        (b"booti 0x80200000 0x83000000:${initrd_size} 0x88000000", b"Starting kernel", 10),
    ]

    master_fd, slave_fd = pty.openpty()
    proc = subprocess.Popen(argv, stdin=slave_fd, stdout=slave_fd,
                            stderr=slave_fd, close_fds=True)
    os.close(slave_fd)
    output = b""

    def read_until(pattern, timeout=10):
        nonlocal output
        deadline = time.time() + timeout
        while time.time() < deadline:
            r, _, _ = select.select([master_fd], [], [], 0.2)
            if r:
                data = os.read(master_fd, 4096)
                if not data:
                    return False
                output += data
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()
                if pattern in output:
                    return True
        return False

    def send(cmd):
        os.write(master_fd, cmd + b"\r\n")

    if not read_until(b"=>", 30):
        print("[boot] U-Boot timeout")
        return output

    for cmd, expected, to in cmds:
        send(cmd)
        time.sleep(0.3)
        if not read_until(expected, to):
            print(f"[boot] WARNING: '{cmd.decode()}' missed expected")
        if expected in output:
            output = output.split(expected, 1)[-1] if len(output.split(expected)) > 1 else b""

    print("[boot] Waiting for kernel (M16_VERIFY_DONE)...")
    deadline = time.time() + 120
    while time.time() < deadline:
        r, _, _ = select.select([master_fd], [], [], 0.5)
        if r:
            try:
                data = os.read(master_fd, 4096)
            except:
                break
            if not data:
                break
            output += data
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
            if b"M16_VERIFY_DONE" in output:
                print("[boot] M16 VERIFY DONE!")
                break
            if b"panic" in output or b"Panic" in output:
                print("[boot] PANIC!")
                break

    time.sleep(1)
    try:
        while True:
            r, _, _ = select.select([master_fd], [], [], 0.3)
            if not r:
                break
            data = os.read(master_fd, 4096)
            if not data:
                break
            output += data
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
    except:
        pass

    os.close(master_fd)
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except:
        proc.kill()
    return output

def main():
    print("[boot] Launching QEMU (DRM kernel, M16 initramfs, correct args)...")
    output = run()
    text = output.decode('utf-8', errors='replace')
    print("\n=== M16 VT Verification Results ===")
    results = {"vt_ok": 0, "vt_miss": 0, "tty0": False, "drm": False, "render": False}
    for line in text.split('\n'):
        if 'M16_' in line:
            print(f"  {line}")
            if 'M16_VT_OK' in line:
                results["vt_ok"] += 1
            if 'M16_VT_MISS' in line:
                results["vt_miss"] += 1
            if 'M16_TTY0_OK' in line:
                results["tty0"] = True
            if 'M16_DRM_OK' in line:
                results["drm"] = True
            if 'M16_RENDER_OK' in line:
                results["render"] = True
    print(f"\nSummary: VT_OK={results['vt_ok']} VT_MISS={results['vt_miss']} "
          f"TTY0={results['tty0']} DRM={results['drm']} RENDER={results['render']}")
    if results["vt_ok"] >= 10 and results["tty0"] and results["drm"]:
        print("M16_VT_PASS: all VT nodes present + DRM card0")
        return 0
    else:
        print("M16_VT_FAIL: some checks failed")
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
