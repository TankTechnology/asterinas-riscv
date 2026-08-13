#include <stdio.h>

#include "libgreet.h"

/* Dynamically linked against musl libc and libgreet.so (DT_NEEDED). */
int main(void)
{
    printf("__M2_HELLO_DYN__ %s\n", greet("riscv64"));
    return 0;
}
