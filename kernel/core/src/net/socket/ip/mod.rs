// SPDX-License-Identifier: MPL-2.0

mod addr;
mod common;
mod datagram;
mod ioctl;
pub(crate) mod options;
mod stream;

pub(crate) use addr::IpAddressFamily;
pub(crate) use datagram::{DatagramSocket, observer::DatagramObserver};
pub(crate) use stream::{StreamSocket, observer::StreamObserver, options as stream_options};
