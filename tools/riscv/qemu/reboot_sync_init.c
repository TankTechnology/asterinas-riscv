// SPDX-License-Identifier: MPL-2.0
// A freestanding RISC-V init that never calls sync.
// The same QEMU process boots twice against one ext2 disk. The second boot
// must observe the first boot's newly created file, then power off. The first
// boot either calls reboot(2) or waits for the opt-in software reboot timer.

typedef unsigned long size_t;

#define AT_FDCWD (-100L)
#define NR_DUP3 24
#define NR_MOUNT 40
#define NR_OPENAT 56
#define NR_CLOSE 57
#define NR_READ 63
#define NR_WRITE 64
#define NR_SCHED_YIELD 124
#define NR_REBOOT 142
#define O_RDONLY 0
#define O_WRONLY 1
#define O_CREAT 0100
#define O_EXCL 0200
#define ENOENT 2
#define EROFS 30
#define MS_RDONLY 1
#define MS_REMOUNT 32
#define REBOOT_MAGIC1 0xfee1dead
#define REBOOT_MAGIC2 0x28121969
#define REBOOT_RESTART 0x01234567
#define REBOOT_POWER_OFF 0x4321fedc

static long call5(long number, long first, long second, long third,
                  long fourth, long fifth) {
    register long a0 __asm__("a0") = first;
    register long a1 __asm__("a1") = second;
    register long a2 __asm__("a2") = third;
    register long a3 __asm__("a3") = fourth;
    register long a4 __asm__("a4") = fifth;
    register long a7 __asm__("a7") = number;
    __asm__ volatile("ecall" : "+r"(a0) : "r"(a1), "r"(a2), "r"(a3),
                     "r"(a4), "r"(a7) : "memory");
    return a0;
}

static size_t length(const char *text) {
    size_t count = 0;
    while (text[count] != '\0') count++;
    return count;
}

static void emit(const char *text) {
    size_t bytes = length(text);
    while (bytes != 0) {
        long written = call5(NR_WRITE, 1, (long)text, (long)bytes, 0, 0);
        if (written <= 0) break;
        text += written;
        bytes -= (size_t)written;
    }
}

static void emit_number(long value) {
    char reversed[24];
    char number[24];
    size_t count = 0;
    size_t output = 0;
    if (value < 0) {
        number[output++] = '-';
        value = -value;
    }
    do {
        reversed[count++] = '0' + (value % 10);
        value /= 10;
    } while (value != 0);
    while (count != 0) number[output++] = reversed[--count];
    number[output] = '\0';
    emit(number);
}

static void stop(const char *stage) {
    emit("REBOOT_SYNC_FAIL stage=");
    emit(stage);
    emit("\n");
    (void)call5(NR_REBOOT, REBOOT_MAGIC1, REBOOT_MAGIC2, REBOOT_POWER_OFF, 0, 0);
    for (;;) (void)call5(NR_SCHED_YIELD, 0, 0, 0, 0, 0);
}

static void request_reboot(long command) {
    (void)call5(NR_REBOOT, REBOOT_MAGIC1, REBOOT_MAGIC2, command, 0, 0);
    stop("reboot-returned");
}

void _start(void) {
    static const char payload[] = "asterinas-reboot-sync-v1\n";
    static const char path[] = "/ext2/reboot-sync-payload";
    long console = call5(NR_OPENAT, AT_FDCWD, (long)"/dev/console", O_WRONLY, 0, 0);
    if (console >= 0) {
        (void)call5(NR_DUP3, console, 1, 0, 0, 0);
        (void)call5(NR_DUP3, console, 2, 0, 0, 0);
        if (console > 2) (void)call5(NR_CLOSE, console, 0, 0, 0, 0);
    }
    if (call5(NR_MOUNT, (long)"/dev/vda", (long)"/ext2", (long)"ext2", 0, 0) < 0)
        stop("mount-ext2");

    long fd = call5(NR_OPENAT, AT_FDCWD, (long)path, O_RDONLY, 0, 0);
    if (fd >= 0) {
        char actual[sizeof(payload)];
        long got = call5(NR_READ, fd, (long)actual, sizeof(actual), 0, 0);
        (void)call5(NR_CLOSE, fd, 0, 0, 0, 0);
        if (got != (long)sizeof(payload) - 1) stop("second-read-length");
        for (size_t i = 0; i < sizeof(payload) - 1; i++) {
            if (actual[i] != payload[i]) stop("second-read-content");
        }
        emit("REBOOT_SYNC_PASS boot=2\n");
        request_reboot(REBOOT_POWER_OFF);
    }
    if (fd != -ENOENT) {
        emit("REBOOT_SYNC_LOOKUP_RETURN=");
        emit_number(fd);
        emit("\n");
        stop("first-lookup");
    }

    fd = call5(NR_OPENAT, AT_FDCWD, (long)path,
               O_WRONLY | O_CREAT | O_EXCL, 0600, 0);
    if (fd < 0) stop("first-create");
    if (call5(NR_WRITE, fd, (long)payload, sizeof(payload) - 1, 0, 0) !=
        (long)sizeof(payload) - 1) stop("first-write");
#ifndef REBOOT_SYNC_READONLY_REMOUNT
    if (call5(NR_CLOSE, fd, 0, 0, 0, 0) != 0) stop("first-close");
#endif
    emit("REBOOT_SYNC_FIRST_WRITTEN boot=1\n");
#ifdef REBOOT_SYNC_READONLY_REMOUNT
    if (call5(NR_MOUNT, 0, (long)"/ext2", 0,
              MS_RDONLY | MS_REMOUNT, 0) != 0)
        stop("readonly-remount");
    if (call5(NR_WRITE, fd, (long)payload, 1, 0, 0) != -EROFS)
        stop("readonly-existing-fd-write");
    if (call5(NR_CLOSE, fd, 0, 0, 0, 0) != 0) stop("readonly-existing-fd-close");
    long readonly_fd = call5(NR_OPENAT, AT_FDCWD, (long)path, O_WRONLY, 0, 0);
    if (readonly_fd != -EROFS) {
        emit("REBOOT_SYNC_READONLY_OPEN_RETURN=");
        emit_number(readonly_fd);
        emit("\n");
        if (readonly_fd >= 0) (void)call5(NR_CLOSE, readonly_fd, 0, 0, 0, 0);
        stop("readonly-write-probe");
    }
    emit("REBOOT_SYNC_READONLY_REMOUNT boot=1\n");
#endif
#ifdef REBOOT_SYNC_WATCHDOG
    for (;;) (void)call5(NR_SCHED_YIELD, 0, 0, 0, 0, 0);
#else
    request_reboot(REBOOT_RESTART);
#endif
}
