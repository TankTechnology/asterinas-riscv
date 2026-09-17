#!/usr/bin/env python3
"""Launch QEMU with GTK display to show the 3D kmscube demo (virgl).
   The in-guest eglkms renders a spinning cube directly to the DRM
   scanout, which appears in the QEMU GTK window."""
import os, pty, select, subprocess, sys, time

BOOTDISK = "/home/arch-anjie/Program/asterinas-riscv-drm/target/drm-m16/boot.ext4"
UBOOT    = "/home/arch-anjie/Program/asterinas-riscv-drm/target/drm-m16/u-boot"

BOOTARGS = "console=ttyS0 loglevel=info init=/init"

CMDS = [
    (b"virtio scan", b"=>", 30),
    (b"ext4ls virtio 0:0 /", b"asterinas.booti", 10),
    (b"ext4load virtio 0:0 0x80200000 /asterinas.booti", b"bytes read", 30),
    (b"setenv aster_size ${filesize}", b"=>", 5),
    (b"ext4load virtio 0:0 0x90000000 /qemu-virt.dtb", b"bytes read", 10),
    (b"fdt addr 0x90000000", b"Working FDT set", 5),
    (b"fdt resize 0x1000", b"=>", 5),
    (b'setenv bootargs "' + BOOTARGS.encode() + b'"', b"=>", 5),
    (b'fdt set /chosen bootargs "' + BOOTARGS.encode() + b'"', b"=>", 5),
    (b"ext4load virtio 0:0 0x83000000 /initramfs.cpio.gz", b"bytes read", 120),
    (b"setenv initrd_size ${filesize}", b"=>", 5),
    (b"booti 0x80200000 0x83000000:${initrd_size} 0x90000000", b"Starting kernel", 30),
]

def main():
    argv = [
        "qemu-system-riscv64",
        "-machine", "virt",
        "-cpu", "rv64,sv48=false,svpbmt=true,zkr=true,svadu=false,svade=true",
        "-m", "2G",
        "-smp", "4",
        "-display", "gtk,gl=on",
        "-serial", "stdio",
        "-no-reboot",
        "-kernel", UBOOT,
        "-drive", f"if=none,format=raw,file={BOOTDISK},id=bootdisk",
        "-device", "virtio-blk-device,drive=bootdisk",
        "-device", "virtio-gpu-gl-device",
    ]

    master_fd, slave_fd = pty.openpty()
    proc = subprocess.Popen(argv, stdin=slave_fd, stdout=slave_fd,
                            stderr=slave_fd, close_fds=True)
    os.close(slave_fd)

    def read_until(pattern, timeout=10):
        deadline = time.time() + timeout
        buf = b""
        while time.time() < deadline:
            r, _, _ = select.select([master_fd], [], [], 0.2)
            if r:
                try:
                    data = os.read(master_fd, 65536)
                except OSError:
                    return False
                if not data:
                    return False
                buf += data
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()
                if pattern in buf:
                    return True
        return False

    print("[boot] Waiting for U-Boot prompt...")
    if not read_until(b"=>", 60):
        print("[boot] U-Boot timeout")
        proc.wait()
        return 1

    for cmd, expected, to in CMDS:
        os.write(master_fd, cmd + b"\r\n")
        time.sleep(0.3)
        if not read_until(expected, to):
            print(f"[boot] WARNING: '{cmd.decode()}' missed '{expected.decode()}'")

    print("[boot] Kernel booted — watch the GTK window for the 3D cube!")
    print("[boot] The eglkms demo should render a spinning cube.")
    print("[boot] Press Ctrl+C to quit.")

    # Stream serial output until QEMU exits
    try:
        while proc.poll() is None:
            r, _, _ = select.select([master_fd], [], [], 0.5)
            if r:
                try:
                    data = os.read(master_fd, 65536)
                except OSError:
                    break
                if not data:
                    break
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()
    except KeyboardInterrupt:
        print("\n[boot] Interrupted, shutting down...")
    finally:
        os.close(master_fd)
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())