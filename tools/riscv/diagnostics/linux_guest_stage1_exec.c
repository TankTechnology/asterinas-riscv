// SPDX-License-Identifier: MPL-2.0

// A comparison-only /init for the Linux kernel used as the performance control.
// Keep the product Stage1 argument parser strict: the Linux built-in command
// line is intentionally not forwarded to the original /stage1-init.

// `-std=c11` is strict ISO C, which hides both syscall() and the SYS_* numbers
// this needs to load a module.
#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <unistd.h>

#ifndef STAGE1_EXEC_PATH
#define STAGE1_EXEC_PATH "/stage1-init"
#endif

// The boot-critical modules, in dependency order rather than alphabetical:
// ext4 needs jbd2 and crc16, jbd2 needs crc16, and the block device has to
// exist before anything can be mounted on it.
//
// Asterinas has no loadable modules -- its drivers are compiled in -- so this
// step has no counterpart there. That difference is part of what the control
// compares, not a defect in it: the Debian kernel is built with
// CONFIG_VIRTIO_MMIO=m, CONFIG_VIRTIO_BLK=m and CONFIG_EXT4_FS=m, so on this
// side nothing at all can be mounted until these are in.
//
// crc32c_generic is on this list even though nothing in modules.dep points at
// it. It is a runtime, feature-dependent dependency rather than a symbol one:
// ext4 resolves "crc32c" through the crypto API only when the filesystem it is
// mounting carries metadata checksums, and that call fails with -ENOENT when
// the algorithm is absent. A closure computed with depmod therefore looks
// complete while the mount still fails, and the only clue is one kernel line
// -- "EXT4-fs (vda): Cannot load crc32c driver." -- above a bare ENOENT.
//
// Overridable so the unit test can supply its own list instead of reaching for
// modules that only exist inside a built initramfs.
#ifndef BOOT_MODULES
#define BOOT_MODULES \
    "/lib/modules/" KERNEL_RELEASE "/kernel/lib/crc16.ko", \
    "/lib/modules/" KERNEL_RELEASE "/kernel/crypto/crc32c_generic.ko", \
    "/lib/modules/" KERNEL_RELEASE "/kernel/fs/mbcache.ko", \
    "/lib/modules/" KERNEL_RELEASE "/kernel/fs/jbd2.ko", \
    "/lib/modules/" KERNEL_RELEASE "/kernel/fs/ext4.ko", \
    "/lib/modules/" KERNEL_RELEASE "/kernel/drivers/virtio/virtio_mmio.ko", \
    "/lib/modules/" KERNEL_RELEASE "/kernel/drivers/block/virtio_blk.ko",
#endif

static const char *const BOOT_MODULE_PATHS[] = {BOOT_MODULES NULL};

// Load one module from an already-open file descriptor.
//
// finit_module is used rather than init_module so the kernel reads the image
// itself from the file, which keeps the module in the page cache and avoids
// copying it through userspace. The modules are stored uncompressed in the
// initramfs for the same reason: it removes any question of whether this
// kernel decompresses module images on this path.
static int load_module(const char *path) {
  int descriptor = open(path, O_RDONLY | O_CLOEXEC);
  if (descriptor < 0) {
    fprintf(stderr, "linux-guest-module-open: %s: %s\n", path, strerror(errno));
    return -1;
  }
  int result = (int)syscall(SYS_finit_module, descriptor, "", 0);
  int saved_errno = errno;
  close(descriptor);
  if (result != 0) {
    // Already-loaded is not a failure worth stopping a boot for, and it is the
    // normal outcome when a module is also pulled in by another one's
    // dependency list.
    if (saved_errno != EEXIST) {
      fprintf(stderr, "linux-guest-module-load: %s: %s\n", path,
              strerror(saved_errno));
      return -1;
    }
  }
  return 0;
}

int main(int argc, char *argv[]) {
  (void)argc;
  (void)argv;
  // Linux does not expose virtio block nodes in this CPIO's initial /dev.
  // Asterinas creates them itself, so only the Linux comparison needs this.
  if (mkdir("/dev", 0755) != 0 && errno != EEXIST) {
    perror("linux-guest-dev-directory");
    return 127;
  }
  if (mount("devtmpfs", "/dev", "devtmpfs", 0, NULL) != 0) {
    perror("linux-guest-devtmpfs");
    return 127;
  }
  // A module that fails to load is reported and then ignored: stage1's own
  // "no root device" failure is the one that says what actually went wrong,
  // and stopping here would hide the remaining modules' diagnostics.
  for (const char *const *module = BOOT_MODULE_PATHS; *module != NULL; ++module) {
    (void)load_module(*module);
  }
  char *const stage1_argv[] = {
      STAGE1_EXEC_PATH,
      "--root-init=systemd",
      "--debug-console=root",
      NULL,
  };
  execv(STAGE1_EXEC_PATH, stage1_argv);
  perror("linux-guest-stage1-exec");
  return 127;
}
