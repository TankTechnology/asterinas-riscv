# Freestanding FWIF ABI probe headers

These declarations let the RISC-V cross compiler preprocess the pinned
Volcanic headers when a cross libc sysroot is unavailable. They are only for
`rgx_fwif_abi_probe.c`; they must never be used to build kernel or user-space
code. The emitted section is also compiled with the host's real libc headers
and compared byte for byte against the RISC-V output.
