// SPDX-License-Identifier: MPL-2.0

//! Bounded, opt-in diagnostics for the first userspace process on RISC-V.

use core::{
    fmt,
    sync::atomic::{AtomicBool, AtomicU8, Ordering},
};

use ostd::{
    arch::cpu::context::{CpuException, FaultInstruction, UserContext},
    cpu::PinCurrentCpu,
    task::disable_preempt,
    user::UserContextApi,
    util::id_set::Id,
};

use crate::cpu::LinuxAbi;

const PREFIX: &str = "ASTERINAS_FIRST_PROCESS_DIAG";
const STARTUP_INACTIVE: u8 = 0;
const STARTUP_PROCESS_READY: u8 = 1;
const STARTUP_DEVICE_READY: u8 = 2;
const STARTUP_STDIO_READY: u8 = 3;
const WRITE_SYSCALL_NUMBER: usize = 64;

static REQUESTED: AtomicBool = AtomicBool::new(false);
static FORCE: AtomicBool = AtomicBool::new(false);
static ACTIVE: AtomicBool = AtomicBool::new(false);
static STARTUP_STAGE: AtomicU8 = AtomicU8::new(STARTUP_INACTIVE);

aster_cmdline::define_flag_param!("asterinas.first_process_diag", REQUESTED);
aster_cmdline::define_flag_param!("asterinas.first_process_diag_force", FORCE);

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum ConsoleRegistryToken {
    Empty,
    Registered,
}

impl fmt::Display for ConsoleRegistryToken {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Empty => "empty",
            Self::Registered => "registered",
        })
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum ReturnReasonToken {
    UserException,
    UserSyscall,
    KernelEvent,
}

impl fmt::Display for ReturnReasonToken {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::UserException => "user_exception",
            Self::UserSyscall => "user_syscall",
            Self::KernelEvent => "kernel_event",
        })
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum ExceptionValue {
    Stval(usize),
    Instruction(usize),
    Unavailable,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct ExceptionSnapshot {
    cause: &'static str,
    value: ExceptionValue,
}

impl From<&CpuException> for ExceptionSnapshot {
    fn from(exception: &CpuException) -> Self {
        let (cause, value) = match exception {
            CpuException::InstructionMisaligned => {
                ("instruction_misaligned", ExceptionValue::Unavailable)
            }
            CpuException::InstructionFault => ("instruction_fault", ExceptionValue::Unavailable),
            CpuException::IllegalInstruction(instruction) => {
                let instruction = match instruction {
                    FaultInstruction::Normal(value) => *value as usize,
                    FaultInstruction::Compressed(value) => *value as usize,
                };
                (
                    "illegal_instruction",
                    ExceptionValue::Instruction(instruction),
                )
            }
            CpuException::Breakpoint => ("breakpoint", ExceptionValue::Unavailable),
            CpuException::LoadMisaligned(stval) => {
                ("load_misaligned", ExceptionValue::Stval(*stval))
            }
            CpuException::LoadFault(stval) => ("load_fault", ExceptionValue::Stval(*stval)),
            CpuException::StoreMisaligned(stval) => {
                ("store_misaligned", ExceptionValue::Stval(*stval))
            }
            CpuException::StoreFault(stval) => ("store_fault", ExceptionValue::Stval(*stval)),
            CpuException::UserEnvCall => ("user_env_call", ExceptionValue::Unavailable),
            CpuException::SupervisorEnvCall => ("supervisor_env_call", ExceptionValue::Unavailable),
            CpuException::InstructionPageFault(stval) => {
                ("instruction_page_fault", ExceptionValue::Stval(*stval))
            }
            CpuException::LoadPageFault(stval) => {
                ("load_page_fault", ExceptionValue::Stval(*stval))
            }
            CpuException::StorePageFault(stval) => {
                ("store_page_fault", ExceptionValue::Stval(*stval))
            }
            CpuException::Unknown => ("unknown", ExceptionValue::Unavailable),
        };
        Self { cause, value }
    }
}

impl fmt::Display for ExceptionSnapshot {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "cause={}", self.cause)?;
        match self.value {
            ExceptionValue::Stval(value) => {
                write!(f, " detail_kind=stval detail=0x{:x}", value)
            }
            ExceptionValue::Instruction(value) => {
                write!(f, " detail_kind=instruction detail=0x{:x}", value)
            }
            ExceptionValue::Unavailable => f.write_str(" detail_kind=unavailable"),
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum Marker {
    DiagnosticActive {
        console_registry: ConsoleRegistryToken,
    },
    ProcessComponentsReady,
    DeviceInitReady,
    StdioInitReady,
    UserEnter {
        cpu: usize,
        sepc: usize,
        sp: usize,
    },
    FirstReturn {
        reason: ReturnReasonToken,
        sepc: usize,
        exception: Option<ExceptionSnapshot>,
    },
    FirstSyscall {
        id: usize,
        sepc: usize,
    },
    FirstWriteReturned {
        fd: usize,
        requested: usize,
        result: isize,
    },
}

impl fmt::Display for Marker {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::DiagnosticActive { console_registry } => write!(
                f,
                "{} stage=diagnostic_active console_registry={}",
                PREFIX, console_registry
            ),
            Self::ProcessComponentsReady => {
                write!(f, "{} stage=process_components_ready", PREFIX)
            }
            Self::DeviceInitReady => write!(f, "{} stage=device_init_ready", PREFIX),
            Self::StdioInitReady => write!(f, "{} stage=stdio_init_ready", PREFIX),
            Self::UserEnter { cpu, sepc, sp } => write!(
                f,
                "{} stage=user_enter cpu={} sepc=0x{:x} sp=0x{:x}",
                PREFIX, cpu, sepc, sp
            ),
            Self::FirstReturn {
                reason,
                sepc,
                exception,
            } => {
                write!(
                    f,
                    "{} stage=user_first_return reason={} sepc=0x{:x}",
                    PREFIX, reason, sepc
                )?;
                if let Some(exception) = exception {
                    write!(f, " {}", exception)?;
                }
                Ok(())
            }
            Self::FirstSyscall { id, sepc } => write!(
                f,
                "{} stage=user_first_syscall id={} sepc=0x{:x}",
                PREFIX, id, sepc
            ),
            Self::FirstWriteReturned {
                fd,
                requested,
                result,
            } => write!(
                f,
                "{} stage=user_first_write_returned fd={} requested={} result={}",
                PREFIX, fd, requested, result
            ),
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct WriteSnapshot {
    fd: usize,
    requested: usize,
}

#[derive(Debug, Default)]
struct DiagnosticState {
    user_enter_seen: bool,
    first_return_seen: bool,
    first_syscall_seen: bool,
    pending_write: Option<WriteSnapshot>,
    first_write_returned: bool,
}

impl DiagnosticState {
    fn mark_user_enter(&mut self, cpu: usize, sepc: usize, sp: usize) -> Option<Marker> {
        if self.user_enter_seen {
            return None;
        }
        self.user_enter_seen = true;
        Some(Marker::UserEnter { cpu, sepc, sp })
    }

    fn mark_first_return(
        &mut self,
        reason: ReturnReasonToken,
        sepc: usize,
        exception: Option<ExceptionSnapshot>,
    ) -> Option<Marker> {
        if self.first_return_seen {
            return None;
        }
        self.first_return_seen = true;
        Some(Marker::FirstReturn {
            reason,
            sepc,
            exception,
        })
    }

    fn mark_first_syscall(&mut self, id: usize, sepc: usize) -> Option<Marker> {
        if self.first_syscall_seen {
            return None;
        }
        self.first_syscall_seen = true;
        Some(Marker::FirstSyscall { id, sepc })
    }

    fn capture_write(&mut self, id: usize, args: [usize; 6]) {
        if id == WRITE_SYSCALL_NUMBER && !self.first_write_returned {
            self.pending_write = Some(WriteSnapshot {
                fd: args[0],
                requested: args[2],
            });
        }
    }

    fn complete_write(&mut self, result: isize) -> Option<Marker> {
        if self.first_write_returned {
            return None;
        }
        let write = self.pending_write.take()?;
        self.first_write_returned = true;
        Some(Marker::FirstWriteReturned {
            fd: write.fd,
            requested: write.requested,
            result,
        })
    }
}

#[derive(Debug, Default)]
pub(super) struct FirstProcessDiagnostics {
    state: DiagnosticState,
}

impl FirstProcessDiagnostics {
    pub(super) fn new_if_active() -> Option<Self> {
        ACTIVE.load(Ordering::Acquire).then(Self::default)
    }

    pub(super) fn on_user_enter(&mut self, user_ctx: &UserContext) {
        let guard = disable_preempt();
        let cpu = guard.current_cpu().as_usize();
        drop(guard);
        emit_if_some(self.state.mark_user_enter(
            cpu,
            user_ctx.instruction_pointer(),
            user_ctx.stack_pointer(),
        ));
    }

    pub(super) fn on_user_exception(&mut self, user_ctx: &UserContext, exception: &CpuException) {
        emit_if_some(self.state.mark_first_return(
            ReturnReasonToken::UserException,
            user_ctx.instruction_pointer(),
            Some(ExceptionSnapshot::from(exception)),
        ));
    }

    pub(super) fn on_user_syscall_trap(&mut self, user_ctx: &UserContext) {
        emit_if_some(self.state.mark_first_return(
            ReturnReasonToken::UserSyscall,
            syscall_instruction_pointer(user_ctx.instruction_pointer()),
            None,
        ));
    }

    pub(super) fn on_kernel_event(&mut self, user_ctx: &UserContext) {
        emit_if_some(self.state.mark_first_return(
            ReturnReasonToken::KernelEvent,
            user_ctx.instruction_pointer(),
            None,
        ));
    }

    pub(super) fn on_syscall_enter(&mut self, user_ctx: &UserContext) {
        let id = user_ctx.syscall_num();
        let args = user_ctx.syscall_args();
        let sepc = syscall_instruction_pointer(user_ctx.instruction_pointer());
        let marker = self.state.mark_first_syscall(id, sepc);
        self.state.capture_write(id, args);
        emit_if_some(marker);
    }

    pub(super) fn on_syscall_return(&mut self, user_ctx: &UserContext) {
        emit_if_some(self.state.complete_write(user_ctx.syscall_ret() as isize));
    }
}

pub(super) fn on_process_components_ready() {
    let console_registry_empty = aster_console::all_devices_lock().is_empty();
    let Some(markers) = activation_markers(
        REQUESTED.load(Ordering::Relaxed),
        FORCE.load(Ordering::Relaxed),
        console_registry_empty,
        &ACTIVE,
        &STARTUP_STAGE,
    ) else {
        return;
    };
    for marker in markers {
        emit(marker);
    }
}

pub(super) fn on_device_init_ready() {
    emit_if_some(advance_startup_stage(
        &STARTUP_STAGE,
        STARTUP_PROCESS_READY,
        STARTUP_DEVICE_READY,
        Marker::DeviceInitReady,
    ));
}

pub(super) fn on_stdio_init_ready() {
    emit_if_some(advance_startup_stage(
        &STARTUP_STAGE,
        STARTUP_DEVICE_READY,
        STARTUP_STDIO_READY,
        Marker::StdioInitReady,
    ));
}

fn activation_markers(
    requested: bool,
    forced: bool,
    console_registry_empty: bool,
    active: &AtomicBool,
    stage: &AtomicU8,
) -> Option<[Marker; 2]> {
    if !(requested && (console_registry_empty || forced)) {
        return None;
    }
    stage
        .compare_exchange(
            STARTUP_INACTIVE,
            STARTUP_PROCESS_READY,
            Ordering::AcqRel,
            Ordering::Acquire,
        )
        .ok()?;
    active.store(true, Ordering::Release);
    let console_registry = if console_registry_empty {
        ConsoleRegistryToken::Empty
    } else {
        ConsoleRegistryToken::Registered
    };
    Some([
        Marker::DiagnosticActive { console_registry },
        Marker::ProcessComponentsReady,
    ])
}

fn advance_startup_stage(
    stage: &AtomicU8,
    expected: u8,
    next: u8,
    marker: Marker,
) -> Option<Marker> {
    stage
        .compare_exchange(expected, next, Ordering::AcqRel, Ordering::Acquire)
        .ok()
        .map(|_| marker)
}

fn syscall_instruction_pointer(sepc: usize) -> usize {
    sepc.saturating_sub(4)
}

fn emit_if_some(marker: Option<Marker>) {
    if let Some(marker) = marker {
        emit(marker);
    }
}

fn emit(marker: Marker) {
    ostd::early_println!("{}", marker);
}

#[cfg(ktest)]
mod tests {
    use alloc::string::ToString;
    use core::sync::atomic::{AtomicBool, AtomicU8};

    use ostd::prelude::*;

    use super::{
        ConsoleRegistryToken, DiagnosticState, ExceptionSnapshot, ExceptionValue, Marker,
        ReturnReasonToken, activation_markers,
    };

    #[ktest]
    fn activation_is_explicit_and_registered_console_requires_force() {
        let active = AtomicBool::new(false);
        let stage = AtomicU8::new(0);
        assert!(activation_markers(false, false, true, &active, &stage).is_none());
        assert!(activation_markers(true, false, false, &active, &stage).is_none());

        let markers = activation_markers(true, true, false, &active, &stage).unwrap();
        assert_eq!(
            markers[0],
            Marker::DiagnosticActive {
                console_registry: ConsoleRegistryToken::Registered,
            }
        );
        assert!(activation_markers(true, true, false, &active, &stage).is_none());

        let empty_active = AtomicBool::new(false);
        let empty_stage = AtomicU8::new(0);
        let markers = activation_markers(true, false, true, &empty_active, &empty_stage).unwrap();
        assert_eq!(
            markers[0],
            Marker::DiagnosticActive {
                console_registry: ConsoleRegistryToken::Empty,
            }
        );
    }

    #[ktest]
    fn markers_match_the_existing_audit_contract() {
        assert_eq!(
            Marker::UserEnter {
                cpu: 0,
                sepc: 0x1000,
                sp: 0x2000,
            }
            .to_string(),
            "ASTERINAS_FIRST_PROCESS_DIAG stage=user_enter cpu=0 sepc=0x1000 sp=0x2000"
        );
        assert_eq!(
            Marker::FirstReturn {
                reason: ReturnReasonToken::UserException,
                sepc: 0x1000,
                exception: Some(ExceptionSnapshot {
                    cause: "load_page_fault",
                    value: ExceptionValue::Stval(0x4000),
                }),
            }
            .to_string(),
            "ASTERINAS_FIRST_PROCESS_DIAG stage=user_first_return reason=user_exception sepc=0x1000 cause=load_page_fault detail_kind=stval detail=0x4000"
        );
    }

    #[ktest]
    fn user_markers_are_bounded_and_write_is_paired() {
        let mut state = DiagnosticState::default();
        assert!(state.mark_user_enter(0, 0x1000, 0x2000).is_some());
        assert!(state.mark_user_enter(1, 0x3000, 0x4000).is_none());
        assert!(
            state
                .mark_first_return(ReturnReasonToken::UserSyscall, 0x1000, None)
                .is_some()
        );
        assert!(
            state
                .mark_first_return(ReturnReasonToken::KernelEvent, 0x1000, None)
                .is_none()
        );
        assert!(state.mark_first_syscall(64, 0x1000).is_some());
        assert!(state.mark_first_syscall(56, 0x1000).is_none());
        state.capture_write(64, [1, 0, 50, 0, 0, 0]);
        assert!(state.complete_write(50).is_some());
        assert!(state.complete_write(50).is_none());
    }
}
