// SPDX-License-Identifier: MPL-2.0

//! `print` and `println` macros
//!
//! FIXME: It will print to all `virtio-console` devices, which is not a good choice.
//!

use alloc::{collections::btree_map::BTreeMap, fmt, string::String, sync::Arc};
use core::{fmt::Write, marker::PhantomData};

use aster_console::AnyConsoleDevice;
use ostd::sync::{LocalIrqDisabled, SpinLockGuard};

use super::diagnostics::Observer;

/// Prints the formatted arguments to the standard output.
pub fn _print(args: fmt::Arguments) {
    print_observed(args, &mut super::diagnostics::Unmeasured);
}

pub(super) fn print_observed<O: Observer>(args: fmt::Arguments, observer: &mut O) {
    // We must call `all_devices_lock` instead of `all_devices` here, as `all_devices` invokes the
    // `clone` method of `String` and `Arc`, which may lead to a deadlock when there is low memory
    // in the heap. (The heap allocator will log a message when memory is low.)
    //
    // Also, holding the lock will prevent the logs from interleaving.
    let start = observer.timestamp();
    let devices = aster_console::all_devices_lock();
    let acquired = observer.timestamp();

    struct Printer<'a, O>(
        SpinLockGuard<'a, BTreeMap<String, Arc<dyn AnyConsoleDevice>>, LocalIrqDisabled>,
        u64,
        PhantomData<O>,
    );
    impl<O: Observer> Write for Printer<'_, O> {
        fn write_str(&mut self, s: &str) -> fmt::Result {
            if self.0.is_empty() {
                if O::COUNT_BYTES {
                    self.1 = self.1.saturating_add(s.len() as u64);
                }
                ostd::early_print!("{}", s);
            } else {
                for console in self.0.values() {
                    if O::COUNT_BYTES {
                        self.1 = self.1.saturating_add(s.len() as u64);
                    }
                    console.send_diagnostic_or_restart(s.as_bytes());
                }
            }
            Ok(())
        }
    }

    let mut printer = Printer::<O>(devices, 0, PhantomData);
    let result = printer.write_fmt(args);
    let end = observer.timestamp();
    let bytes = printer.1;
    drop(printer);
    observer.console(start, acquired, end, bytes);
    result.unwrap();
}

/// Copied from Rust std: <https://github.com/rust-lang/rust/blob/master/library/std/src/macros.rs>
#[macro_export]
macro_rules! print {
    ($($arg:tt)*) => {{
        $crate::_print(format_args!($($arg)*));
    }};
}

/// Copied from Rust std: <https://github.com/rust-lang/rust/blob/master/library/std/src/macros.rs>
#[macro_export]
macro_rules! println {
    () => {
        $crate::print!("\n")
    };
    ($($arg:tt)*) => {{
        $crate::_print(::core::format_args_nl!($($arg)*));
    }};
}
