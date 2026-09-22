// SPDX-License-Identifier: MPL-2.0

//! The interrupt level.

use crate::{cpu::PrivilegeLevel, cpu_local_cell};

/// The current interrupt level on a CPU.
///
/// This type tracks the current nesting depth on the CPU
/// where the code is executing.
/// There are three levels:
/// * Level 0 (the task context);
/// * Level 1 (the interrupt context);
/// * Level 2 (the interrupt context due to nested interrupts).
///
/// An `InterruptLevel` is specific to a single CPU
/// and is meaningless when used by or sent to other CPUs,
/// hence it is `!Send` and `!Sync`.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum InterruptLevel {
    /// Level 0 (the task context).
    L0,
    /// Level 1 (the interrupt context).
    ///
    /// The intern value specifies the CPU privilege level of the interrupted code.
    L1(PrivilegeLevel),
    /// Level 2 (the interrupt context due to nested interrupts).
    L2,
}

impl !Send for InterruptLevel {}
impl !Sync for InterruptLevel {}

impl InterruptLevel {
    /// Returns the current interrupt level of this CPU.
    pub fn current() -> Self {
        // Parameters about the encoding of INTERRUPT_LEVEL
        const LEVEL_VAL_OFFSET: u8 = 1;
        const CPU_PRIV_MASK: u8 = 1 << 0;

        let raw_level = INTERRUPT_LEVEL.load();
        let level = raw_level >> LEVEL_VAL_OFFSET;
        match level {
            0 => Self::L0,
            1 => {
                let cpu_priv_at_irq = if (raw_level & CPU_PRIV_MASK) == 0 {
                    PrivilegeLevel::Kernel
                } else {
                    PrivilegeLevel::User
                };
                Self::L1(cpu_priv_at_irq)
            }
            2 => Self::L2,
            // Report the raw byte as well. `enter` adds 0b010 for every
            // interrupt taken from the kernel, so a level of 3 cannot come
            // from nesting alone once the increment is known to be atomic
            // against interrupts --- the value says which of the two it is.
            _ => unreachable!(
                "level must between 0 and 2 (inclusive), raw={raw_level:#010b}"
            ),
        }
    }

    /// Returns the interrupt level as an integer between 0 and 2 (inclusive).
    pub fn as_u8(&self) -> u8 {
        match self {
            Self::L0 => 0,
            Self::L1(_) => 1,
            Self::L2 => 2,
        }
    }

    /// Checks if the CPU is currently in the task context (level 0).
    pub fn is_task_context(&self) -> bool {
        *self == Self::L0
    }

    /// Checks if the CPU is currently in the interrupt context (level 1 or 2).
    pub fn is_interrupt_context(&self) -> bool {
        matches!(self, Self::L1(_) | Self::L2)
    }
}

/// Enters the scope of interrupt handling,
/// increasing the interrupt level by one.
///
/// The `cpu_priv_at_irq` argument specifies the CPU privilege level of
/// the code interrupted by the IRQ.
pub(super) fn enter<F: FnOnce()>(f: F, cpu_priv_at_irq: PrivilegeLevel) {
    let increment = {
        let bit_0 = match cpu_priv_at_irq {
            PrivilegeLevel::Kernel => 0,
            PrivilegeLevel::User => 1,
        };
        let bit_1 = 0b10;
        bit_1 | bit_0
    };
    // Record where this handler started. A level that ends up one layer too
    // high means a handler entered and did not unwind, and only the CPU and
    // the task identity say whether it was abandoned rather than merely slow.
    let cpu_entered = crate::cpu::CpuId::current_racy();
    let task_entered = crate::task::current_task_ptr_for_diagnosis();
    INTERRUPT_LEVEL.add_assign(increment);

    f();

    let cpu_left = crate::cpu::CpuId::current_racy();
    let task_left = crate::task::current_task_ptr_for_diagnosis();
    if cpu_entered != cpu_left || task_entered != task_left {
        crate::warn!(
            "IRQ handler did not return to where it started: cpu {cpu_entered:?} -> {cpu_left:?}, \
             task {task_entered:#x} -> {task_left:#x}, raw={:#010b}",
            INTERRUPT_LEVEL.load(),
        );
    }

    INTERRUPT_LEVEL.sub_assign(increment);
}

/// A value nothing should ever write, sitting in the same CPU-local page as
/// the cells that have been observed going wrong.
///
/// The level, the preemption guard count and the current-task pointer are
/// three independent cells that have each been caught holding impossible
/// values while the base, the allocation and the access atomicity all checked
/// out. A sentinel in the same page separates "one cell's arithmetic is wrong"
/// from "something else is writing this page".
pub(crate) fn canary_intact() -> bool {
    CANARY.load() == CANARY_VALUE
}

/// The interrupt level cell exactly as stored, without decoding it.
///
/// `current()` cannot be used from a diagnostic that runs while the level is
/// already out of range: the decode is what panics. The raw byte can always be
/// read, so a report taken at a failure names the state instead of recursing
/// into the same failure.
pub(crate) fn raw_for_diagnosis() -> u8 {
    INTERRUPT_LEVEL.load()
}

const CANARY_VALUE: u32 = 0x5A5A_5A5A;

cpu_local_cell! {
    static CANARY: u32 = CANARY_VALUE;

    /// The interrupt level of the current IRQ.
    ///
    /// We pack two pieces of information into a single byte:
    /// 1. The current interrupt level (bit 1 - 7);
    /// 2. The CPU privilege level of the code interrupted by the IRQ (bit 0).
    ///
    /// More specifically,
    /// the encoding of this byte is summarized in the table below.
    ///
    /// | Values   | Meaning             |
    /// |----------|---------------------|
    /// | `0b00_0` | L0                  |
    /// | `0b01_0` | L1 from kernel      |
    /// | `0b01_1` | L1 from user        |
    /// | `0b10_0` | L2 (L1 from kernel) |
    /// | `0b10_1` | L2 (L1 from user)   |
    ///
    /// This compact encoding allows us to update this value
    /// in a single arithmetic operation (see `enter`).
    static INTERRUPT_LEVEL: u8 = 0;
}
