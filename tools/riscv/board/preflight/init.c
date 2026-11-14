/* SPDX-License-Identifier: MPL-2.0 */
//
// Minimal init for the pre-deployment smoke boot.
//
// The only thing a smoke boot has to prove is that the image reaches
// userspace: a kernel that panics before init runs is exactly the
// "board comes up dead" case this gate exists to catch. Print the marker and
// then block forever -- exiting would look like a failed init to the kernel
// and turn a passing boot into a panic.
#include <stdio.h>
#include <unistd.h>

int main(void)
{
    printf("ASTERINAS_PREFLIGHT_USERSPACE_OK\n");
    fflush(stdout);
    for (;;)
        pause();
    return 0;
}
