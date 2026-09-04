// SPDX-License-Identifier: MPL-2.0

use bitflags::bitflags;

use super::{
    c_types::sigaction_t,
    constants::*,
    sig_mask::{SigMask, SigSet},
    sig_num::SigNum,
};
use crate::prelude::*;

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub enum SigHandler {
    #[default]
    Dfl,
    Ign,
    User(usize),
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct SigAction {
    handler: SigHandler,
    flags: SigActionFlags,
    restorer_addr: usize,
    mask: SigMask,
}

impl Default for SigAction {
    fn default() -> Self {
        Self {
            handler: SigHandler::Dfl,
            flags: SigActionFlags::empty(),
            restorer_addr: 0,
            mask: SigMask::default(),
        }
    }
}

impl From<sigaction_t> for SigAction {
    fn from(input: sigaction_t) -> Self {
        let handler = match input.handler_ptr {
            SIG_DFL => SigHandler::Dfl,
            SIG_IGN => SigHandler::Ign,
            handler_addr => SigHandler::User(handler_addr),
        };

        Self {
            handler,
            flags: SigActionFlags::from_bits_truncate(input.flags),
            restorer_addr: input.restorer_ptr,
            mask: SigSet::from(input.mask),
        }
    }
}

impl SigAction {
    pub fn as_c_type(&self) -> sigaction_t {
        let handler_ptr = match self.handler {
            SigHandler::Dfl => SIG_DFL,
            SigHandler::Ign => SIG_IGN,
            SigHandler::User(handler_addr) => handler_addr,
        };

        sigaction_t {
            handler_ptr,
            flags: self.flags.as_u32(),
            restorer_ptr: self.restorer_addr,
            mask: self.mask.into(),
            ..Default::default()
        }
    }

    pub fn handler(&self) -> SigHandler {
        self.handler
    }

    pub fn flags(&self) -> SigActionFlags {
        self.flags
    }

    pub fn restorer_addr(&self) -> usize {
        self.restorer_addr
    }

    pub fn mask(&self) -> SigMask {
        self.mask
    }

    /// Resets only the handler while retaining the action metadata.
    ///
    /// Linux uses this behavior for `SA_RESETHAND`: a query from inside the
    /// handler observes `SIG_DFL`, but flags such as `SA_SIGINFO` remain set.
    pub fn reset_handler(&mut self) {
        self.handler = SigHandler::Dfl;
    }

    /// Returns whether signals will be ignored.
    ///
    /// Signals will be ignored because either
    ///  * the signal action is explicitly set to ignore the signals, or
    ///  * the signal action is default and the default action is to ignore the signals.
    pub fn will_ignore(&self, signum: SigNum) -> bool {
        match self.handler {
            SigHandler::Dfl => {
                let default_action = SigDefaultAction::from_signum(signum);
                matches!(default_action, SigDefaultAction::Ign)
            }
            SigHandler::Ign => true,
            SigHandler::User(_) => false,
        }
    }
}

bitflags! {
    pub struct SigActionFlags: u32 {
        const SA_NOCLDSTOP  = 1;
        const SA_NOCLDWAIT  = 2;
        const SA_SIGINFO    = 4;
        const SA_ONSTACK    = 0x08000000;
        const SA_RESTART    = 0x10000000;
        const SA_NODEFER    = 0x40000000;
        const SA_RESETHAND  = 0x80000000;
        const SA_RESTORER   = 0x04000000;
    }
}

impl TryFrom<u32> for SigActionFlags {
    type Error = Error;

    fn try_from(bits: u32) -> Result<Self> {
        let flags = SigActionFlags::from_bits(bits)
            .ok_or_else(|| Error::with_message(Errno::EINVAL, "invalid sig action flag"))?;
        Ok(flags)
    }
}

impl SigActionFlags {
    pub fn as_u32(&self) -> u32 {
        self.bits()
    }

    pub fn contains_unsupported_flag(&self) -> bool {
        self.intersects(SigActionFlags::SA_NOCLDSTOP | SigActionFlags::SA_NOCLDWAIT)
    }
}

/// The default action to signals
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SigDefaultAction {
    Term, // Default action is to terminate the process.
    Ign,  // Default action is to ignore the signal.
    Core, // Default action is to terminate the process and dump core (see core(5)).
    Stop, // Default action is to stop the process.
    Cont, // Default action is to continue the process if it is currently stopped.
}

impl SigDefaultAction {
    pub fn from_signum(num: SigNum) -> SigDefaultAction {
        match num {
            SIGABRT | // = SIGIOT
            SIGBUS  |
            SIGFPE  |
            SIGILL  |
            SIGQUIT |
            SIGSEGV |
            SIGSYS  | // = SIGUNUSED
            SIGTRAP |
            SIGXCPU |
            SIGXFSZ
                => SigDefaultAction::Core,
            SIGCHLD |
            SIGURG  |
            SIGWINCH
                => SigDefaultAction::Ign,
            SIGCONT
                => SigDefaultAction::Cont,
            SIGSTOP |
            SIGTSTP |
            SIGTTIN |
            SIGTTOU
                => SigDefaultAction::Stop,
            _
                => SigDefaultAction::Term,
        }
    }
}
