// SPDX-License-Identifier: MPL-2.0
// M9 "hello" — the classic smoke binary, installed into /nix/store via a
// Nix derivation (path B: prebuilt, cross-compiled on the host with
// riscv64-linux-musl-gcc). Prints a fixed greeting.
#include <stdio.h>

int main(void) {
    puts("Hello, world! (from a Nix-installed binary on Asterinas RISC-V)");
    return 0;
}
