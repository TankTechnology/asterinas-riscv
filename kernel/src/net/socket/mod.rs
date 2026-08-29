// SPDX-License-Identifier: MPL-2.0

use core::fmt::Display;

use options::SocketOption;
use util::{MessageHeader, RecvFlags, RecvOutput, SendFlags, SockShutdownCmd, SocketAddr};

use crate::{
    fs::{
        file::{
            AccessMode, CreationFlags, FileCommon, FileLike, SettableStatusFlags,
            file_table::FdFlags,
        },
        pseudofs::SockFs,
    },
    prelude::*,
    util::{MultiRead, MultiWrite, ioctl::RawIoctl},
};

pub mod ip;
pub mod netlink;
pub mod options;
pub mod unix;
pub mod util;
pub mod vsock;

mod private {
    use core::time::Duration;

    use crate::{events::IoEvents, prelude::*, process::signal::Pollable};

    /// Common methods for sockets, but private to the network module.
    ///
    /// These are implementation details of sockets, so shouldn't be accessed outside the network
    /// module. Therefore, the whole trait is sealed.
    pub trait SocketPrivate: Pollable {
        /// Returns whether the socket is in non-blocking mode.
        fn is_nonblocking(&self) -> bool;

        /// Blocks until some events occur to complete I/O operations.
        ///
        /// If the socket is in non-blocking mode and the I/O operations cannot be completed
        /// immediately, this method will fail with [`EAGAIN`] instead of blocking.
        ///
        /// [`EAGAIN`]: crate::error::Errno::EAGAIN
        #[track_caller]
        fn block_on<F, R>(
            &self,
            events: IoEvents,
            timeout: Option<Duration>,
            mut try_op: F,
        ) -> Result<R>
        where
            Self: Sized,
            F: FnMut() -> Result<R>,
        {
            if self.is_nonblocking() {
                try_op()
            } else {
                self.wait_events(events, timeout.as_ref(), try_op)
                    .map_err(|err| match err.error() {
                        Errno::ETIME => {
                            Error::with_message(Errno::EAGAIN, "the socket timeout expired")
                        }
                        _ => err,
                    })
            }
        }
    }
}

/// Operations defined on a socket.
pub trait Socket: private::SocketPrivate + Send + Sync {
    /// Assigns the specified address to the socket.
    fn bind(&self, _socket_addr: SocketAddr) -> Result<()> {
        return_errno_with_message!(Errno::EOPNOTSUPP, "bind() is not supported");
    }

    /// Builds a connection for the given address
    fn connect(&self, _socket_addr: SocketAddr) -> Result<()> {
        return_errno_with_message!(Errno::EOPNOTSUPP, "connect() is not supported");
    }

    /// Listens for connections on the socket.
    fn listen(&self, _backlog: usize) -> Result<()> {
        return_errno_with_message!(Errno::EOPNOTSUPP, "listen() is not supported");
    }

    /// Accepts a connection on the socket.
    fn accept(&self, _is_nonblocking: bool) -> Result<(Arc<dyn FileLike>, SocketAddr)> {
        return_errno_with_message!(Errno::EOPNOTSUPP, "accept() is not supported");
    }

    /// Shuts down part of a full-duplex connection.
    fn shutdown(&self, _cmd: SockShutdownCmd) -> Result<()> {
        return_errno_with_message!(Errno::EOPNOTSUPP, "shutdown() is not supported");
    }

    /// Gets the address of this socket.
    fn addr(&self) -> Result<SocketAddr> {
        return_errno_with_message!(Errno::EOPNOTSUPP, "getsockname() is not supported");
    }

    /// Gets the address of the peer socket.
    fn peer_addr(&self) -> Result<SocketAddr> {
        return_errno_with_message!(Errno::EOPNOTSUPP, "getpeername() is not supported");
    }

    /// Gets options on the socket.
    ///
    /// If the method succeeds, the result will be stored in the `option` parameter.
    fn get_option(&self, _option: &mut dyn SocketOption) -> Result<()> {
        return_errno_with_message!(Errno::EOPNOTSUPP, "getsockopt() is not supported");
    }

    /// Sets options on the socket.
    fn set_option(&self, _option: &dyn SocketOption) -> Result<()> {
        return_errno_with_message!(Errno::EOPNOTSUPP, "setsockopt() is not supported");
    }

    /// Sends a message on the socket.
    fn sendmsg(
        &self,
        reader: &mut dyn MultiRead,
        message_header: MessageHeader,
        flags: SendFlags,
    ) -> Result<usize>;

    /// Receives a message from the socket.
    ///
    /// If successful, the `writer` buffer will be filled with the received content.
    /// This method returns the length, flags, and header of the received message.
    fn recvmsg(
        &self,
        writer: &mut dyn MultiWrite,
        flags: RecvFlags,
    ) -> Result<(RecvOutput, MessageHeader)>;

    /// Returns the common state for this socket.
    fn common(&self) -> &FileCommon;
}

impl<T: Socket + 'static> FileLike for T {
    fn read(&self, writer: &mut VmWriter) -> Result<usize> {
        if !writer.has_avail() {
            // Linux always returns `Ok(0)` in this case, so we follow it.
            return Ok(0);
        }

        // TODO: Set correct flags
        self.recvmsg(writer, RecvFlags::empty())
            .map(|(output, _)| output.len())
    }

    fn write(&self, reader: &mut VmReader) -> Result<usize> {
        // TODO: Set correct flags
        self.sendmsg(
            reader,
            MessageHeader::new(None, Vec::new()),
            SendFlags::empty(),
        )
    }

    fn settable_status_flags(&self) -> SettableStatusFlags {
        SettableStatusFlags::minimal().with_o_async()
    }

    fn access_mode(&self) -> AccessMode {
        // Reference: <https://elixir.bootlin.com/linux/v7.0/source/net/socket.c#L483>.
        AccessMode::O_RDWR
    }

    fn as_socket(&self) -> Option<&dyn Socket> {
        Some(self)
    }

    fn common(&self) -> &FileCommon {
        Socket::common(self)
    }

    fn dump_proc_fdinfo(self: Arc<Self>, fd_flags: FdFlags) -> Box<dyn Display> {
        struct FdInfo {
            flags: u32,
            ino: u64,
        }

        impl Display for FdInfo {
            fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
                writeln!(f, "pos:\t{}", 0)?;
                writeln!(f, "flags:\t0{:o}", self.flags)?;
                writeln!(f, "mnt_id:\t{}", SockFs::mount_node().id())?;
                writeln!(f, "ino:\t{}", self.ino)
            }
        }

        let mut flags = self.common().status_flags().bits() | self.access_mode() as u32;
        if fd_flags.contains(FdFlags::CLOEXEC) {
            flags |= CreationFlags::O_CLOEXEC.bits();
        }

        Box::new(FdInfo {
            flags,
            ino: self.common().path().inode().ino(),
        })
    }

    fn ioctl(&self, raw_ioctl: RawIoctl) -> Result<i32> {
        use aster_bigtcp::iface::InterfaceFlags;

        use crate::{
            net::{iface::Iface, net_ns::current_net_ns},
            util::ioctl::{InData, InOutData, dispatch_ioctl, ioc},
        };

        /// The legacy `SIOCGIFFLAGS`/`SIOCSIFFLAGS` argument: the interface
        /// name (16 bytes) followed by the flags (a 16-bit `short`).
        #[repr(C)]
        #[derive(Clone, Copy, Debug, Pod)]
        struct Ifreq {
            name: [u8; 16],
            flags: i16,
        }

        /// `SIOCGIFFLAGS` (0x8913): get interface flags.
        type GetIfaceFlags = ioc!(SIOCGIFFLAGS, 0x8913, InOutData<Ifreq>);
        /// `SIOCSIFFLAGS` (0x8914): set interface flags.
        type SetIfaceFlags = ioc!(SIOCSIFFLAGS, 0x8914, InData<Ifreq>);

        /// Resolves an interface in the current network namespace by its
        /// NUL-terminated `ifr_name`.
        fn lookup_iface(name: &[u8; 16]) -> Result<Arc<Iface>> {
            let target = CStr::from_bytes_until_nul(name)
                .map_err(|_| Error::with_message(Errno::ENODEV, "invalid interface name"))?;
            current_net_ns()
                .ifaces()
                .iter()
                .find(|iface| iface.name() == target)
                .cloned()
                .ok_or_else(|| Error::with_message(Errno::ENODEV, "no interface with that name"))
        }

        dispatch_ioctl!(match raw_ioctl {
            cmd @ GetIfaceFlags => {
                let mut ifreq = cmd.read()?;
                let iface = lookup_iface(&ifreq.name)?;
                // `ifr_flags` is 16 bits wide, so only the low 16 bits of the
                // interface flags are visible through this legacy ioctl.
                ifreq.flags = iface.flags().bits() as i16;
                cmd.write(&ifreq)?;
            }
            cmd @ SetIfaceFlags => {
                let ifreq = cmd.read()?;
                let iface = lookup_iface(&ifreq.name)?;
                // Mirror `do_new_link`'s no-change-mask semantics: only `UP`
                // and `RUNNING` are togglable, everything else is preserved.
                let incoming = InterfaceFlags::from_bits_truncate(ifreq.flags as u16 as u32);
                let old_flags = iface.flags();
                let new_flags = (old_flags & !(InterfaceFlags::UP | InterfaceFlags::RUNNING))
                    | (incoming & (InterfaceFlags::UP | InterfaceFlags::RUNNING));
                iface.set_flags(new_flags);
            }
            _ => {
                return_errno_with_message!(Errno::ENOTTY, "ioctl is not supported for sockets");
            }
        });
        Ok(0)
    }
}
