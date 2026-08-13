#include <stdio.h>

#include "libgreet.h"

const char *greet(const char *who)
{
    static char buf[64];
    snprintf(buf, sizeof(buf), "hello from libgreet to %s", who);
    return buf;
}
