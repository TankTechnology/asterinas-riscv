// SPDX-License-Identifier: MPL-2.0

/*
 * Minimal libc-free RISC-V probe for Linux's deferred System V SHM removal
 * semantics. It is intentionally self-contained so it can be injected into a
 * cached initramfs without rebuilding the full regression-test dependency set.
 */

typedef unsigned long usize;

#define IPC_PRIVATE 0
#define IPC_CREAT 01000
#define IPC_RMID 0
#define EINVAL 22

#define SYS_WRITE 64
#define SYS_EXIT 93
#define SYS_SHMGET 194
#define SYS_SHMCTL 195
#define SYS_SHMAT 196
#define SYS_SHMDT 197

static long syscall3(long number, long arg0, long arg1, long arg2) {
  register long a0 __asm__("a0") = arg0;
  register long a1 __asm__("a1") = arg1;
  register long a2 __asm__("a2") = arg2;
  register long a7 __asm__("a7") = number;

  __asm__ volatile("ecall" : "+r"(a0) : "r"(a1), "r"(a2), "r"(a7) : "memory");
  return a0;
}

static __attribute__((noreturn)) void exit_with(long status) {
  syscall3(SYS_EXIT, status, 0, 0);
  __builtin_unreachable();
}

void _start(void) {
  static const char message[] = "System V SHM deferred removal probe passed.\n";
  static const char rollback_failure[] =
      "System V SHM rejected attach leaked an attachment.\n";
  static const char phantom_failure[] =
      "System V SHM failed attach retained a phantom attachment.\n";
  long shmid;
  long first_addr;
  long second_addr;
  long final_addr;
  volatile unsigned long *first;
  volatile unsigned long *second;

  shmid = syscall3(SYS_SHMGET, IPC_PRIVATE, 4096, IPC_CREAT | 0600);
  if (shmid < 0)
    exit_with(1);

  first_addr = syscall3(SYS_SHMAT, shmid, 0, 0);
  if (first_addr < 0)
    exit_with(2);
  first = (volatile unsigned long *)first_addr;
  *first = 0x12345678;

  if (syscall3(SYS_SHMCTL, shmid, IPC_RMID, 0) != 0)
    exit_with(3);

  /* A rejected attach must not retain a phantom attachment count. */
  if (syscall3(SYS_SHMAT, shmid, 1, 0) != -EINVAL) {
    syscall3(SYS_WRITE, 2, (long)rollback_failure,
             sizeof(rollback_failure) - 1);
    exit_with(10);
  }

  /* This is the attach that Xorg performs after Firefox's IPC_RMID. */
  second_addr = syscall3(SYS_SHMAT, shmid, 0, 0);
  if (second_addr < 0)
    exit_with(4);
  second = (volatile unsigned long *)second_addr;
  if (*second != 0x12345678)
    exit_with(5);

  if (syscall3(SYS_SHMDT, second_addr, 0, 0) != 0)
    exit_with(6);
  if (syscall3(SYS_SHMDT, first_addr, 0, 0) != 0)
    exit_with(7);

  /* The ID must disappear after the last attachment is detached. */
  final_addr = syscall3(SYS_SHMAT, shmid, 0, 0);
  if (final_addr != -EINVAL) {
    syscall3(SYS_WRITE, 2, (long)phantom_failure, sizeof(phantom_failure) - 1);
    exit_with(8);
  }

  if (syscall3(SYS_WRITE, 1, (long)message, sizeof(message) - 1) < 0)
    exit_with(9);
  exit_with(0);
}
