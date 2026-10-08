// SPDX-License-Identifier: MPL-2.0

/* Freestanding RISC-V init for the offline PowerVR META release gate.
 *
 * The initramfs contains the four host-validated firmware segments.  This
 * program sends each segment as one PVR1 frame, then sends the exact PVRR
 * release command.  It deliberately does not use a shell, Python, or a
 * network service: the only guest-to-host boundary is the serial log.
 */

typedef unsigned long size_t;
typedef unsigned char uint8_t;
typedef unsigned int uint32_t;

#define AT_FDCWD (-100L)
#define NR_DUP3 24
#define NR_OPENAT 56
#define NR_CLOSE 57
#define NR_READ 63
#define NR_WRITE 64
#define NR_SCHED_YIELD 124
#define O_RDWR 2
#define O_RDONLY 0
#define ENOENT 2
#define EAGAIN 11
#define STATUS_SIZE 48
#define FRAME_HEADER_SIZE 12
#define MAX_SEGMENT_SIZE 73312

static long call5(long number, long first, long second, long third, long fourth,
                  long fifth) {
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
        if (written <= 0) return;
        text += written;
        bytes -= (size_t)written;
    }
}

static void emit_number(long value) {
    char reversed[24];
    char output[24];
    size_t count = 0;
    size_t written = 0;
    if (value < 0) {
        output[written++] = '-';
        value = -value;
    }
    do {
        reversed[count++] = (char)('0' + value % 10);
        value /= 10;
    } while (value != 0);
    while (count != 0) output[written++] = reversed[--count];
    output[written] = '\0';
    emit(output);
}

static void hold(void) {
    for (;;) (void)call5(NR_SCHED_YIELD, 0, 0, 0, 0, 0);
}

static void fail(const char *stage, long code) {
    emit("PVR_RELEASE_FAIL stage=");
    emit(stage);
    emit(" code=");
    emit_number(code);
    emit("\n");
    hold();
}

static long read_full(long fd, uint8_t *buffer, size_t size) {
    size_t offset = 0;
    while (offset < size) {
        long got = call5(NR_READ, fd, (long)(buffer + offset),
                         (long)(size - offset), 0, 0);
        if (got <= 0) return got;
        offset += (size_t)got;
    }
    return (long)offset;
}

static long write_full(long fd, const uint8_t *buffer, size_t size) {
    size_t offset = 0;
    while (offset < size) {
        long sent = call5(NR_WRITE, fd, (long)(buffer + offset),
                          (long)(size - offset), 0, 0);
        if (sent <= 0) return sent;
        offset += (size_t)sent;
    }
    return (long)offset;
}

static void put_u32_le(uint8_t *where, uint32_t value) {
    where[0] = (uint8_t)value;
    where[1] = (uint8_t)(value >> 8);
    where[2] = (uint8_t)(value >> 16);
    where[3] = (uint8_t)(value >> 24);
}

static uint32_t get_u32_le(const uint8_t *where) {
    return (uint32_t)where[0] | ((uint32_t)where[1] << 8) |
           ((uint32_t)where[2] << 16) | ((uint32_t)where[3] << 24);
}

static const char *const segment_paths[] = {
    "/pvr/code.bin", "/pvr/data.bin", "/pvr/coremem_code.bin",
    "/pvr/coremem_data.bin",
};
static const uint32_t segment_sizes[] = {52064, 18432, 73312, 9984};

static uint8_t frame[FRAME_HEADER_SIZE + MAX_SEGMENT_SIZE];
static uint8_t status[STATUS_SIZE];

static void stage_one(long control, uint32_t segment) {
    long fd = call5(NR_OPENAT, AT_FDCWD, (long)segment_paths[segment],
                    O_RDONLY, 0, 0);
    if (fd < 0) fail("open-segment", fd);
    put_u32_le(frame, 0x31525650U); /* PVR1 */
    put_u32_le(frame + 4, segment);
    put_u32_le(frame + 8, segment_sizes[segment]);
    if (read_full(fd, frame + FRAME_HEADER_SIZE, segment_sizes[segment]) !=
        (long)segment_sizes[segment])
        fail("read-segment", segment);
    (void)call5(NR_CLOSE, fd, 0, 0, 0, 0);
    if (write_full(control, frame, FRAME_HEADER_SIZE + segment_sizes[segment]) !=
        (long)(FRAME_HEADER_SIZE + segment_sizes[segment]))
        fail("stage-frame", segment);
}

void _start(void) {
    long console = call5(NR_OPENAT, AT_FDCWD, (long)"/dev/console", O_RDWR,
                         0, 0);
    if (console >= 0) {
        (void)call5(NR_DUP3, console, 1, 0, 0, 0);
        (void)call5(NR_DUP3, console, 2, 0, 0, 0);
        if (console > 2) (void)call5(NR_CLOSE, console, 0, 0, 0, 0);
    }

    long control = call5(NR_OPENAT, AT_FDCWD, (long)"/dev/powervr-control",
                         O_RDWR, 0, 0);
    if (control == -ENOENT || control == -EAGAIN) fail("open-control", control);
    if (control < 0) fail("open-control", control);
    emit("PVR_RELEASE_STAGE begin\n");
    for (uint32_t segment = 0; segment < 4; segment++)
        stage_one(control, segment);

    static const uint8_t release[] = {'P', 'V', 'R', 'R'};
    if (write_full(control, release, sizeof(release)) != (long)sizeof(release))
        fail("release", -1);
    if (read_full(control, status, sizeof(status)) != (long)sizeof(status))
        fail("status", -1);
    emit("PVR_RELEASE_PASS started=");
    emit_number(get_u32_le(status + 8));
    emit(" faults=");
    emit_number(get_u32_le(status + 16));
    emit(" hwr=");
    emit_number(get_u32_le(status + 24));
    emit("\n");
    (void)call5(NR_CLOSE, control, 0, 0, 0, 0);
    hold();
}
