# What the rebased kernel needed to run systemd (2026-11-18)

The rebase branch could mount the Debian root but never finish booting it. This
is the chain of defects that were actually behind that, in the order they
blocked the boot. All of them reproduce and are fixable in QEMU with the same
kernel binary and rootfs; only the last one is board specific.

## The defects

| # | Defect | Symptom | Fix |
| --- | --- | --- | --- |
| 1 | `time::init()` stopped calling `system_time::init()` (`9229acf1c` removed the call, `8e90d6159` restored the module but not the call) | `adjtimex`/`clock_settime` panic with `Option::unwrap()` on `None`; because a panic restarts when the boot deadline is armed, the board looked like it reset itself a second into the boot | `b6f4c6a25` |
| 2 | devtmpfs never registered `/dev/kmsg` (`mem::init_in_first_kthread` added Full/Null/Random/Urandom/Zero only) | systemd-journald exits with status 1 | `0b3e8a9bf` |
| 3 | `SO_TIMESTAMP` (option 29) had no entry in `CSocketOptionName` | journald treats the `ENOPROTOOPT` from `setsockopt` as fatal; `Notify message sent ... "ERRNO=92"` then `EXIT_STATUS=1` | `0b3e8a9bf` |
| 4 | `notify_enqueue()` had no caller at all: `9bdaea9bd` split the signal enqueue and `8e07bd7e0` switched both call sites to the un-notifying half | a pending signal never re-arms the signalfd pollee, epoll never reports it, PID 1 never sees its children exit, and every oneshot service sits in "start operation timed out" for 90 s with all CPUs idle | `0b3e8a9bf` |
| 5 | `/proc/<pid>/mountinfo` had no `Pollable`, so libmount's `EPOLLIN\|EPOLLET` monitor never got another edge | `Mount process finished, but there is no mount.` and every `.mount` unit failed with `MOUNT_FAILURE_PROTOCOL` (`result 'protocol'`) | `d2709e89c` (downstream `9beb74a7c`, dropped by the rebase) |
| 6 | `waitid`'s `WEXITED` was missing from the supported mask | 343 warnings per boot; behaviour was already correct | `eb703edd7` (downstream `877da06f8`) |

Defects 4 and 5 were each invisible until the previous one was fixed: the
panic hid the journald failure, journald hid the signalfd failure, and the
signalfd failure hid the mount-table one. Every one of them looked like "the
board hangs" from the outside.

## How to tell them apart

* A panic that restarts the board: `asterinas.reboot_after=0
  asterinas.panic_reboot=0` both restart the board and hide the message.
  `recovery_shall_restart()` is `PANIC_RESTART_ENABLED || is_armed()`, so the
  deadline alone is enough to turn a panic into what looks like a power reset.
* All CPUs idle (`thread apply all bt` on QEMU's gdb stub shows only
  `bsp_idle_loop`/`ap_idle_loop`) means every task is blocked, not spinning.
  That is the signature of the signalfd and mountinfo defects, not of a
  deadlock.
* `Started`/`Reached target` never reach the console in this image:
  `/etc/systemd/system.conf.d/10-console-budget.conf` sets `LogTarget=journal`
  and `ShowStatus=no`. Boot with `loglevel=info systemd.show_status=yes
  systemd.log_target=kmsg` to see progress.

## Verifying in QEMU

The board's boot path is stage1 plus systemd, and it behaves differently from a
plain `root=/dev/vda` boot:

```
qemu-system-riscv64 -machine virt -cpu rv64 -smp 2 -m 2G -display none -snapshot \
  -serial file:/tmp/qemu-sd.log \
  -kernel target/osdk/asterinas/asterinas-osdk-bin.Image \
  -initrd /tmp/qemu-stage1.cpio \
  -drive file=target/debian-riscv/browser-web/rootfs/debian-root.ext2,format=raw,if=none,id=rootdisk \
  -device virtio-blk-device,drive=rootdisk \
  -append "console=ttyS0 loglevel=info init=/init asterinas.reboot_after=0 systemd.show_status=yes -- --root-init=systemd"
```

`/tmp/qemu-stage1.cpio` is built with
`tools/riscv/debian/rootfs/build_stage1.sh`. `-display none` also means no
framebuffer, hence no `/dev/tty1`, so units with `StandardInput=tty` fail with
`Failed to set up standard input: No such file or directory` — that failure is
an artefact of the QEMU setup, not of the kernel.

After the mountinfo fix the boot contains zero `result 'protocol'` lines. The
next blockers observed there are the missing `/dev/ttyN` nodes (see above) and
the cgroup gaps (`cgroup.kill` absent, `cgroup.threads` registered without a
handler, `memory.events` returning `EIO`).

## Board operations that cost hours

* **The console bridge queues, it does not drop.** It types at 20 ms/byte
  (50 B/s); writing a 250-byte chunk every 0.9 s leaves a backlog that lands
  minutes later, which truncated a transfer and, worse, delivered stale
  `setenv` and `bootargs` lines during a later boot. Wait for each command's
  result before sending the next one — `tools/riscv/board/megrez_uboot_session.py`
  does that.
* **`chroot /mnt2` has its own `/mnt3`.** A file written to `/mnt3/...` inside
  the chroot lands in a directory on p2, not on the p3 partition, and the
  hash you check afterwards verifies the wrong copy. Copy with `cat` outside
  the chroot and verify through a p3 mount.
* **The vendor initramfs has no `wc`, `cp`, `head`, or `sha256sum`.** Use
  `chroot /mnt2 /usr/bin/stat -c %s` and `chroot /mnt2 /usr/bin/sha256sum`.
  A size check built on `wc` silently reports nothing, which is how a
  half-transferred payload looked like a complete one.
* **The campus portal binds the session to the source address.** After DHCP
  hands out a different address the board loses the network even though the
  portal still shows it online; `ip addr add <authenticated address>/24 dev
  end0` restores it immediately. The login itself must be issued *from the
  board* (`megrez_portal_login.py` refuses to sign for another address), and a
  second login for an address that is already online returns `sign_error`.
* **`srun` works on this portal** — `suc_msg: login_ok` — it is only the
  duplicate login that fails.
