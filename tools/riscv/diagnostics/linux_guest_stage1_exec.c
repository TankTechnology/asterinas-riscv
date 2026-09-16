// SPDX-License-Identifier: MPL-2.0

// A comparison-only /init for Linux kernels with CONFIG_CMDLINE_EXTEND=y.
// Keep the product Stage1 argument parser strict: the Linux built-in command
// line is intentionally not forwarded to the original /stage1-init.

#include <errno.h>
#include <stdio.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <unistd.h>

#ifndef STAGE1_EXEC_PATH
#define STAGE1_EXEC_PATH "/stage1-init"
#endif

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
