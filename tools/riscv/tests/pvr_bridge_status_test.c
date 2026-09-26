// SPDX-License-Identifier: MPL-2.0

// Include the tracer to exercise its actual bounded status reader without a
// RockOS GPU. The test uses the real self-process memory-read syscall.
#include "../perf/ioctltrace.c"

#include <assert.h>
#include <sys/mman.h>

int main(void)
{
    unsigned char connect_output[17] = {0};
    unsigned char heap_output[44] = {0};
    uint32_t value = 0;
    uint32_t expected = 37;
    void *unreadable;

    memcpy(connect_output + 8, &expected, sizeof(expected));
    assert(read_pvr_bridge_status(1, 0, connect_output,
                                  sizeof(connect_output), &value));
    assert(value == expected);

    expected = 43;
    memcpy(heap_output + 32, &expected, sizeof(expected));
    assert(read_pvr_bridge_status(6, 24, heap_output,
                                  sizeof(heap_output), &value));
    assert(value == expected);

    assert(!read_pvr_bridge_status(1, 0, connect_output, 11, &value));
    assert(!read_pvr_bridge_status(99, 0, connect_output,
                                   sizeof(connect_output), &value));

    unreadable = mmap(NULL, 4096, PROT_NONE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    assert(unreadable != MAP_FAILED);
    assert(!read_pvr_bridge_status(1, 0, unreadable, 17, &value));
    assert(munmap(unreadable, 4096) == 0);
    return 0;
}
