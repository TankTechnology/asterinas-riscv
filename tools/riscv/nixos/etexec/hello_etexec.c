// SPDX-License-Identifier: MPL-2.0
//
// Repro for the ET_EXEC + PT_INTERP ELF-loader gap: a non-PIE dynamically
// linked binary. Prints a marker via write() (no stdio buffering) so we can
// tell "reached main and ran" apart from "exited 0 silently".

#include <string.h>
#include <unistd.h>

int main(void)
{
	const char *msg = "hello from ET_EXEC (non-PIE, dynamic)\n";
	(void)write(1, msg, strlen(msg));
	return 0;
}
