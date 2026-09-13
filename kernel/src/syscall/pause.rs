// SPDX-License-Identifier: MPL-2.0

use ostd::sync::Waiter;

use super::SyscallReturn;
use crate::prelude::*;

pub fn sys_pause(_ctx: &Context) -> Result<SyscallReturn> {
    let waiter = Waiter::new_pair().0;

    waiter.pause_until(|| None::<()>).map_err(|err| {
        if err.error() == Errno::EINTR {
            Error::new(Errno::ERESTARTNOHAND)
        } else {
            err
        }
    })?;

    unreachable!("pause can only finish by signal interruption");
}
