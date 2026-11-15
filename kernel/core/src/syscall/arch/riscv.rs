// SPDX-License-Identifier: MPL-2.0

//! System call dispatch in the RISC-V architecture.

#[path = "./generic.rs"]
mod generic;

use crate::syscall::{
    riscv_flush_icache::sys_riscv_flush_icache, riscv_hwprobe::sys_riscv_hwprobe,
};

generic::define_syscalls_with_generic_syscall_table! {
    SYS_RISCV_HWPROBE = 258 => sys_riscv_hwprobe(args[..5]);
    SYS_RISCV_FLUSH_ICACHE = 259 => sys_riscv_flush_icache(args[..3]);
}
