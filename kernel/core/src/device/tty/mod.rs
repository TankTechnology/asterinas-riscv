// SPDX-License-Identifier: MPL-2.0

use device_id::{DeviceId, MajorId, MinorId};
use ostd::sync::LocalIrqDisabled;

use self::{line_discipline::LineDiscipline, termio::CFontOp};
use crate::{
    device::{Device, DeviceType, DevtmpfsInodeMeta},
    events::IoEvents,
    fs::file::{PerOpenFileOps, StatusFlags},
    prelude::*,
    process::{
        JobControl, Terminal, broadcast_signal_async,
        signal::{PollHandle, Pollable, Pollee},
    },
    util::ioctl::{RawIoctl, dispatch_ioctl},
};

mod device;
mod driver;
mod file;
mod flags;
mod hvc;
pub(super) mod ioctl_defs;
mod line_discipline;
mod serial;
pub(super) mod termio;
mod vt;

pub(super) use driver::TtyDriver;
pub(super) use flags::TtyFlags;

pub(super) fn init_in_first_process() -> Result<()> {
    hvc::init_in_first_process()?;
    serial::init_in_first_process()?;
    device::init_in_first_process()?;
    vt::init_in_first_process()?;

    Ok(())
}

const IO_CAPACITY: usize = 4096;

/// A teletyper (TTY).
///
/// This abstracts the general functionality of a TTY in a way that
///  - Any input device driver can use [`Tty::push_input`] to push input characters, and users can
///    [`Tty::read`] from the TTY;
///  - Users can also [`Tty::write`] output characters to the TTY and the output device driver will
///    receive the characters from [`TtyDriver::push_output`] where the generic parameter `D` is
///    the [`TtyDriver`].
///
/// ```text
/// +------------+     +-------------+
/// |input device|     |output device|
/// |   driver   |     |   driver    |
/// +-----+------+     +------^------+
///       |                   |
///       |     +-------+     |
///       +----->  TTY  +-----+
///             +-------+
/// Tty::push_input   D::push_output
/// ```
pub struct Tty<D> {
    index: u32,
    driver: D,
    ldisc: SpinLock<LineDiscipline, LocalIrqDisabled>,
    job_control: JobControl,
    pollee: Pollee,
    tty_flags: TtyFlags,
    weak_self: Weak<Self>,
}

impl<D> Tty<D> {
    pub(super) fn new(index: u32, driver: D) -> Arc<Self> {
        Arc::new_cyclic(move |weak_ref| Tty {
            index,
            driver,
            ldisc: SpinLock::new(LineDiscipline::new()),
            job_control: JobControl::new(),
            pollee: Pollee::new(),
            tty_flags: TtyFlags::new(),
            weak_self: weak_ref.clone(),
        })
    }

    pub fn index(&self) -> u32 {
        self.index
    }

    pub(super) fn driver(&self) -> &D {
        &self.driver
    }

    /// Returns whether new characters can be pushed into the input buffer.
    ///
    /// This method should return `false` if the input buffer is full.
    pub(super) fn can_push(&self) -> bool {
        !self.ldisc.lock().is_full()
    }

    /// Notifies that output readiness or its availability may have changed.
    ///
    /// This method should be called whenever output readiness may have changed.
    pub(super) fn notify_output_change(&self) {
        // An output-state callback is intentionally untyped: rechecking the
        // driver may reveal either capacity or an error.
        self.pollee.notify(IoEvents::OUT | IoEvents::ERR);
    }

    /// Notifies that the other end has been closed.
    pub(super) fn notify_hup(&self) {
        self.pollee.notify(IoEvents::ERR | IoEvents::HUP);
    }

    /// Returns the TTY flags.
    pub(super) fn tty_flags(&self) -> &TtyFlags {
        &self.tty_flags
    }
}

impl<D: TtyDriver> Tty<D> {
    /// Pushes characters into the output buffer.
    ///
    /// This method returns the number of bytes pushed or fails with an error if no bytes can be
    /// pushed because the buffer is full.
    pub fn push_input(&self, chs: &[u8]) -> Result<usize> {
        // Echo bytes are collected under the lock but written only after it
        // is released, so a slow output device never stalls input processing.
        let mut echoes: Vec<u8> = Vec::new();

        let mut len = 0;
        {
            let mut ldisc = self.ldisc.lock();
            for ch in chs {
                let res = ldisc.push_char(*ch, |signum| {
                    if let Some(foreground) = self.job_control.foreground() {
                        broadcast_signal_async(Arc::downgrade(&foreground), signum);
                    }
                });
                match res {
                    Ok(Some((first, second))) => {
                        echoes.push(first);
                        if let Some(second) = second {
                            echoes.push(second);
                        }
                        len += 1;
                    }
                    Ok(None) => len += 1,
                    Err(_) => {
                        if len == 0 {
                            return_errno_with_message!(
                                Errno::EAGAIN,
                                "the line discipline is full"
                            );
                        }
                        break;
                    }
                }
            }
        }

        if !echoes.is_empty() {
            let mut echo = self.driver.echo_callback();
            echo(&echoes);
        }

        self.pollee.notify(IoEvents::IN | IoEvents::RDNORM);
        Ok(len)
    }

    fn check_io_events(&self) -> IoEvents {
        let mut events = IoEvents::empty();

        if self.ldisc.lock().buffer_len() > 0 {
            events |= IoEvents::IN | IoEvents::RDNORM;
        }

        match self.driver.poll_output_ready() {
            Ok(true) => events |= IoEvents::OUT,
            Ok(false) => {}
            Err(_) => events |= IoEvents::ERR,
        }

        if self.tty_flags.is_other_closed() {
            events |= IoEvents::ERR | IoEvents::HUP;
        }

        events
    }
}

impl<D: TtyDriver> Pollable for Tty<D> {
    fn poll(&self, mask: IoEvents, poller: Option<&mut PollHandle>) -> IoEvents {
        self.pollee
            .poll_with(mask, poller, || self.check_io_events())
    }
}

impl<D: TtyDriver> Tty<D> {
    pub fn read(&self, writer: &mut VmWriter, status_flags: StatusFlags) -> Result<usize> {
        if self.tty_flags.is_other_closed() {
            return Ok(0);
        }

        self.job_control.wait_until_in_foreground()?;

        // TODO: Add support for timeout.
        let mut buf = vec![0u8; writer.avail().min(IO_CAPACITY)];
        let is_nonblocking = status_flags.contains(StatusFlags::O_NONBLOCK);
        let read_len = if is_nonblocking {
            self.ldisc.lock().try_read(&mut buf)?
        } else {
            self.wait_events(IoEvents::IN, None, || self.ldisc.lock().try_read(&mut buf))?
        };
        self.pollee.invalidate();
        self.driver.notify_input();

        // TODO: Confirm what we should do if `write_fallible` fails in the middle.
        writer.write_fallible(&mut buf[..read_len].into())?;
        Ok(read_len)
    }

    pub fn write(&self, reader: &mut VmReader, status_flags: StatusFlags) -> Result<usize> {
        if self.tty_flags.is_other_closed() {
            return_errno_with_message!(Errno::EIO, "the TTY is closed");
        }

        let mut buf = vec![0u8; reader.remain().min(IO_CAPACITY)];
        let write_len = {
            let mut snapshot = reader.clone();
            snapshot.read_fallible(&mut buf.as_mut_slice().into())
        }?;

        let try_push_output_fn = || {
            let result = self.driver.push_output(&buf[..write_len]);
            if result
                .as_ref()
                .is_err_and(|error| error.error() == Errno::EAGAIN)
            {
                // Besides closing the check-to-arm race, this converts a
                // readiness-access failure into EIO before a blocking writer
                // can sleep indefinitely.
                let _ = self.driver.poll_output_ready()?;
            }
            result
        };

        // TODO: Add support for timeout.
        let is_nonblocking = status_flags.contains(StatusFlags::O_NONBLOCK);
        let result = if is_nonblocking {
            try_push_output_fn()
        } else {
            self.wait_events(IoEvents::OUT, None, try_push_output_fn)
        };
        self.pollee.invalidate();

        match &result {
            Ok(_) => match self.driver.poll_output_ready() {
                Ok(true) => self.notify_output_change(),
                Ok(false) => {}
                Err(_) => self.pollee.notify(IoEvents::ERR),
            },
            Err(error) if error.error() == Errno::EIO => self.pollee.notify(IoEvents::ERR),
            Err(_) => {}
        }

        let accepted_len = result?;
        reader.skip(accepted_len);
        Ok(accepted_len)
    }

    pub fn ioctl(&self, raw_ioctl: RawIoctl) -> Result<i32> {
        use ioctl_defs::*;

        use crate::util::ioctl::common_defs::GetNumBytesToRead;

        dispatch_ioctl!(match raw_ioctl {
            cmd @ GetTermios => {
                let ldisc = self.ldisc.lock();
                let termios = ldisc.termios();

                cmd.write(termios)?;
            }
            cmd @ GetTermios2 => {
                let ldisc = self.ldisc.lock();
                let termios = ldisc.termios();

                cmd.write(termios)?;
            }
            cmd @ SetTermios => {
                let termios = cmd.read()?;

                let mut ldisc = self.ldisc.lock();
                self.driver().on_termios_change(ldisc.termios(), &termios);
                ldisc.set_termios(termios);
            }
            cmd @ SetTermios2 => {
                let termios2 = cmd.read()?;

                let mut ldisc = self.ldisc.lock();
                self.driver().on_termios_change(ldisc.termios(), &termios2);
                ldisc.set_termios2(termios2);
            }
            cmd @ SetTermiosWait => {
                let termios = cmd.read()?;

                // TODO: If applicable, wait for the output buffer to drain. For now, we don't need
                // to do anything here because:
                //  - Linux does not consider a pty to have an output buffer, so it does not drain
                //    it. See
                //    <https://elixir.bootlin.com/linux/v5.10.247/source/drivers/tty/pty.c#L137-L148>.
                //  - We don't currently have an output buffer for other TTYs.
                let mut ldisc = self.ldisc.lock();
                self.driver().on_termios_change(ldisc.termios(), &termios);
                ldisc.set_termios(termios);
            }
            cmd @ SetTermios2Wait => {
                let termios2 = cmd.read()?;

                // TODO: If applicable, wait for the output buffer to drain. (See comments above.)
                let mut ldisc = self.ldisc.lock();
                self.driver().on_termios_change(ldisc.termios(), &termios2);
                ldisc.set_termios2(termios2);
            }
            cmd @ SetTermiosFlush => {
                let termios = cmd.read()?;

                // TODO: If applicable, wait for the output buffer to drain. (See comments above.)
                let mut ldisc = self.ldisc.lock();
                ldisc.drain_input();
                self.driver().on_termios_change(ldisc.termios(), &termios);
                ldisc.set_termios(termios);

                self.pollee.invalidate();
            }
            cmd @ SetTermios2Flush => {
                let termios2 = cmd.read()?;

                // TODO: If applicable, wait for the output buffer to drain. (See comments above.)
                let mut ldisc = self.ldisc.lock();
                ldisc.drain_input();
                self.driver().on_termios_change(ldisc.termios(), &termios2);
                ldisc.set_termios2(termios2);

                self.pollee.invalidate();
            }
            cmd @ GetWinSize => {
                let winsize = self.ldisc.lock().window_size();

                cmd.write(&winsize)?;
            }
            cmd @ SetWinSize => {
                let winsize = cmd.read()?;

                self.ldisc.lock().set_window_size(winsize);
            }
            cmd @ GetNumBytesToRead => {
                if self.tty_flags.is_other_closed() {
                    return_errno_with_message!(Errno::EIO, "the TTY is closed");
                }

                let buffer_len = self.ldisc.lock().buffer_len() as i32;

                cmd.write(&buffer_len)?;
            }

            _ => {
                let terminal = self.weak_self.upgrade().unwrap() as Arc<dyn Terminal>;

                // Process job-control ioctls.
                if terminal.job_ioctl(raw_ioctl, false)? {
                    return Ok(0);
                }

                // Process driver-specific ioctls.
                if self.driver.ioctl(self, raw_ioctl)? {
                    return Ok(0);
                }

                return_errno_with_message!(Errno::ENOTTY, "the ioctl command is unknown");
            }
        });

        Ok(0)
    }
}

impl<D: TtyDriver> Terminal for Tty<D> {
    fn job_control(&self) -> &JobControl {
        &self.job_control
    }
}

impl<D: TtyDriver> Device for Tty<D> {
    fn type_(&self) -> DeviceType {
        DeviceType::Char
    }

    fn id(&self) -> DeviceId {
        DeviceId::new(
            MajorId::new(D::DEVICE_MAJOR_ID as u16),
            MinorId::new(self.index),
        )
    }

    fn devtmpfs_meta(&self) -> Option<DevtmpfsInodeMeta<'_>> {
        self.driver.devtmpfs_meta(self.index)
    }

    fn open(&self) -> Result<Box<dyn PerOpenFileOps>> {
        D::open(self.weak_self.upgrade().unwrap())
    }
}
#[cfg(ktest)]
mod tests {
    use core::sync::atomic::{AtomicBool, AtomicU32, Ordering};

    use ostd::prelude::ktest;
    use spin::Once;

    use super::{termio::CTermios, *};
    use crate::{events::Observer, process::signal::PollAdaptor};

    static TEST_TTY: Once<Weak<Tty<LockCheckingDriver>>> = Once::new();

    struct LockCheckingDriver {
        echo_saw_unlocked_ldisc: AtomicBool,
    }

    struct ShortWriteDriver;

    struct ReadinessErrorDriver {
        failed: AtomicBool,
    }

    struct RecordingObserver {
        events: Arc<AtomicU32>,
    }

    impl Observer<IoEvents> for RecordingObserver {
        fn on_events(&self, events: &IoEvents) {
            self.events.fetch_or(events.bits(), Ordering::Relaxed);
        }
    }

    impl TtyDriver for LockCheckingDriver {
        const DEVICE_MAJOR_ID: u32 = 0;

        fn devtmpfs_meta(&self, _index: u32) -> Option<DevtmpfsInodeMeta<'_>> {
            None
        }

        fn open(_tty: Arc<Tty<Self>>) -> Result<Box<dyn PerOpenFileOps>> {
            unreachable!()
        }

        fn push_output(&self, chs: &[u8]) -> Result<usize> {
            Ok(chs.len())
        }

        fn echo_callback(&self) -> impl FnMut(&[u8]) + '_ {
            move |_| {
                let tty = TEST_TTY.get().unwrap().upgrade().unwrap();
                self.echo_saw_unlocked_ldisc
                    .store(tty.ldisc.try_lock().is_some(), Ordering::Relaxed);
            }
        }

        fn poll_output_ready(&self) -> Result<bool> {
            Ok(true)
        }

        fn notify_input(&self) {}

        fn on_termios_change(&self, _old_termios: &CTermios, _new_termios: &CTermios) {}
    }

    impl TtyDriver for ShortWriteDriver {
        const DEVICE_MAJOR_ID: u32 = 0;

        fn devtmpfs_meta(&self, _index: u32) -> Option<DevtmpfsInodeMeta<'_>> {
            None
        }

        fn open(_tty: Arc<Tty<Self>>) -> Result<Box<dyn PerOpenFileOps>> {
            unreachable!()
        }

        fn push_output(&self, chs: &[u8]) -> Result<usize> {
            Ok(chs.len().min(2))
        }

        fn echo_callback(&self) -> impl FnMut(&[u8]) + '_ {
            |_| {}
        }

        fn poll_output_ready(&self) -> Result<bool> {
            Ok(true)
        }

        fn notify_input(&self) {}

        fn on_termios_change(&self, _old_termios: &CTermios, _new_termios: &CTermios) {}
    }

    impl TtyDriver for ReadinessErrorDriver {
        const DEVICE_MAJOR_ID: u32 = 0;

        fn devtmpfs_meta(&self, _index: u32) -> Option<DevtmpfsInodeMeta<'_>> {
            None
        }

        fn open(_tty: Arc<Tty<Self>>) -> Result<Box<dyn PerOpenFileOps>> {
            unreachable!()
        }

        fn push_output(&self, _chs: &[u8]) -> Result<usize> {
            if self.failed.load(Ordering::Relaxed) {
                return_errno_with_message!(Errno::EIO, "the output device failed");
            }
            return_errno_with_message!(Errno::EAGAIN, "the output device is busy");
        }

        fn echo_callback(&self) -> impl FnMut(&[u8]) + '_ {
            |_| {}
        }

        fn poll_output_ready(&self) -> Result<bool> {
            if self.failed.load(Ordering::Relaxed) {
                return_errno_with_message!(Errno::EIO, "the output device failed");
            }
            Ok(false)
        }

        fn notify_input(&self) {}

        fn on_termios_change(&self, _old_termios: &CTermios, _new_termios: &CTermios) {}
    }

    #[ktest]
    fn tty_echo_runs_without_the_line_discipline_lock() {
        let tty = Tty::new(
            0,
            LockCheckingDriver {
                echo_saw_unlocked_ldisc: AtomicBool::new(false),
            },
        );
        TEST_TTY.call_once(|| Arc::downgrade(&tty));

        assert_eq!(tty.push_input(b"x").unwrap(), 1);
        assert!(tty.driver.echo_saw_unlocked_ldisc.load(Ordering::Relaxed));
    }

    #[ktest]
    fn short_write_advances_the_reader_only_by_reported_progress() {
        let tty = Tty::new(0, ShortWriteDriver);
        let mut reader = VmReader::from(b"abcd".as_slice()).to_fallible();

        assert_eq!(tty.write(&mut reader, StatusFlags::empty()).unwrap(), 2);
        assert_eq!(reader.remain(), 2);
    }

    #[ktest]
    fn output_change_wakes_observers_that_always_poll_errors() {
        let tty = Tty::new(0, ShortWriteDriver);
        let events = Arc::new(AtomicU32::new(0));
        let mut observer = PollAdaptor::with_observer(RecordingObserver {
            events: events.clone(),
        });

        assert!(
            tty.poll(IoEvents::IN, Some(observer.as_handle_mut()))
                .is_empty()
        );
        tty.notify_output_change();

        assert!(
            IoEvents::from_bits_truncate(events.load(Ordering::Relaxed)).contains(IoEvents::ERR)
        );
    }

    #[ktest]
    fn write_error_wakes_a_registered_output_waiter() {
        let tty = Tty::new(
            0,
            ReadinessErrorDriver {
                failed: AtomicBool::new(false),
            },
        );
        let events = Arc::new(AtomicU32::new(0));
        let mut observer = PollAdaptor::with_observer(RecordingObserver {
            events: events.clone(),
        });
        assert!(
            tty.poll(IoEvents::OUT, Some(observer.as_handle_mut()))
                .is_empty()
        );

        tty.driver.failed.store(true, Ordering::Relaxed);
        let mut reader = VmReader::from(b"x".as_slice()).to_fallible();
        let error = tty.write(&mut reader, StatusFlags::O_NONBLOCK).unwrap_err();

        assert_eq!(error.error(), Errno::EIO);
        assert!(
            IoEvents::from_bits_truncate(events.load(Ordering::Relaxed)).contains(IoEvents::ERR)
        );
    }
}
