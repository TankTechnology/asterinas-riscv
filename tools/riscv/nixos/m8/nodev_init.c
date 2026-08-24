// SPDX-License-Identifier: MPL-2.0

// Freestanding RISC-V `/init` for the devtmpfs auto-create regression test.
//
// The initramfs deliberately contains no `/dev`. Reaching `_start` proves the
// kernel created `/dev` before starting PID 1; opening the directory and its
// console device verifies that devtmpfs was mounted and populated.

typedef unsigned long usize;

#define SYS_OPENAT 56
#define SYS_CLOSE 57
#define SYS_WRITE 64
#define SYS_SCHED_YIELD 124

#define AT_FDCWD (-100)
#define O_RDONLY 0
#define O_RDWR 2
#define O_DIRECTORY 0x10000

static long syscall1(long number, long arg0) {
    register long a0 __asm__("a0") = arg0;
    register long a7 __asm__("a7") = number;
    __asm__ volatile("ecall" : "+r"(a0) : "r"(a7) : "memory");
    return a0;
}

static long syscall3(long number, long arg0, long arg1, long arg2) {
    register long a0 __asm__("a0") = arg0;
    register long a1 __asm__("a1") = arg1;
    register long a2 __asm__("a2") = arg2;
    register long a7 __asm__("a7") = number;
    __asm__ volatile("ecall" : "+r"(a0) : "r"(a1), "r"(a2), "r"(a7) : "memory");
    return a0;
}

static long syscall4(long number, long arg0, long arg1, long arg2, long arg3) {
    register long a0 __asm__("a0") = arg0;
    register long a1 __asm__("a1") = arg1;
    register long a2 __asm__("a2") = arg2;
    register long a3 __asm__("a3") = arg3;
    register long a7 __asm__("a7") = number;
    __asm__ volatile(
        "ecall" : "+r"(a0) : "r"(a1), "r"(a2), "r"(a3), "r"(a7) : "memory"
    );
    return a0;
}

static usize string_length(const char *string) {
    usize length = 0;
    while (string[length] != '\0')
        ++length;
    return length;
}

static void say(const char *string) {
    (void)syscall3(SYS_WRITE, 1, (long)string, (long)string_length(string));
}

static long open_path(const char *path, long flags) {
    return syscall4(SYS_OPENAT, AT_FDCWD, (long)path, flags, 0);
}

void _start(void) {
    say(">>> M8 nodev init: no /dev was in the initramfs <<<\n");

    long dev_fd = open_path("/dev", O_RDONLY | O_DIRECTORY);
    say(dev_fd >= 0 ? "__M8_DEV__=DIR\n" : "__M8_DEV__=MISSING\n");
    if (dev_fd >= 0)
        (void)syscall1(SYS_CLOSE, dev_fd);

    long console_fd = open_path("/dev/console", O_RDWR);
    say(console_fd >= 0 ? "__M8_CONSOLE__=PRESENT\n" : "__M8_CONSOLE__=MISSING\n");
    say(console_fd >= 0 ? "__M8_OPEN_CONSOLE__=OK\n" : "__M8_OPEN_CONSOLE__=FAIL\n");
    if (console_fd >= 0)
        (void)syscall1(SYS_CLOSE, console_fd);

    long loop_control_fd = open_path("/dev/loop-control", O_RDWR);
    say(loop_control_fd >= 0 ? "__M8_LOOP_CONTROL__=PRESENT\n"
                             : "__M8_LOOP_CONTROL__=MISSING\n");
    if (loop_control_fd >= 0)
        (void)syscall1(SYS_CLOSE, loop_control_fd);

    say(">>> M8 nodev init done <<<\n");
    for (;;)
        (void)syscall1(SYS_SCHED_YIELD, 0);
}
