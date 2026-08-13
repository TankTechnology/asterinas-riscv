// SPDX-License-Identifier: MPL-2.0
//
// Shared-library TLS probe for the M4 repro. A `__thread` variable in a shared
// object is accessed via the dynamic thread vector (DTV) / `__tls_get_addr`,
// unlike `tls_repro.c`'s local-exec TLS in the main executable. On the current
// kernel the new thread's DTV is NULL, so `__tls_get_addr` faults at address
// `module_id * 8` (0x8 for the first module) — the exact nix/Boole-GC symptom.
// See M4-report.md "Remaining gap: general-dynamic TLS (DTV)".

__thread int shared_tls_var = 99;

int *get_shared_tls(void) {
    return &shared_tls_var;
}
