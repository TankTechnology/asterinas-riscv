// SPDX-License-Identifier: MPL-2.0

use alloc::vec;
use core::str::FromStr;

use ostd::prelude::*;

use crate::{
    net::socket::{
        Socket,
        netlink::{
            GroupIdSet, NetlinkSocketAddr, NetlinkUeventSocket,
            kobject_uevent::{
                UeventMessage,
                message::{
                    syn_uevent::{SyntheticUevent, Uuid},
                    uevent::Uevent,
                },
            },
            table::{NetlinkUeventProtocol, SupportedNetlinkProtocol},
        },
        util::{RecvFlags, SocketAddr},
    },
    prelude::*,
    util::net::SockType,
};

#[ktest]
fn socket_filter_applies_before_readiness_and_truncates() {
    use crate::{
        events::IoEvents,
        net::socket::options::{AttachFilter, DetachFilter},
        process::signal::Pollable,
        util::bpf::SockFilter,
    };

    crate::net::socket::netlink::init();
    let socket = NetlinkUeventSocket::new(true, SockType::SOCK_DGRAM);
    let mut option = AttachFilter::new();
    option.set(Arc::new(vec![SockFilter {
        code: 0x06,
        jt: 0,
        jf: 0,
        k: 0,
    }]));
    socket.set_option(&option).unwrap();
    socket
        .bind(SocketAddr::Netlink(NetlinkSocketAddr::new(
            101,
            GroupIdSet::new(1),
        )))
        .unwrap();

    let message = super::UeventMessage {
        uevent: b"abcdef".to_vec(),
        src_addr: NetlinkSocketAddr::new_unspecified(),
    };
    NetlinkUeventProtocol::multicast(GroupIdSet::new(1), message.clone()).unwrap();
    assert!(!socket.poll(IoEvents::IN, None).contains(IoEvents::IN));
    let mut buffer = [0u8; 16];
    let mut writer = VmWriter::from(buffer.as_mut_slice()).to_fallible();
    assert!(
        socket
            .try_recv(&mut writer, RecvFlags::empty())
            .is_err_and(|err| err.error() == Errno::EAGAIN)
    );

    option.set(Arc::new(vec![SockFilter {
        code: 0x06,
        jt: 0,
        jf: 0,
        k: 3,
    }]));
    socket.set_option(&option).unwrap();
    NetlinkUeventProtocol::multicast(GroupIdSet::new(1), message.clone()).unwrap();
    assert!(socket.poll(IoEvents::IN, None).contains(IoEvents::IN));
    let (output, _) = socket.try_recv(&mut writer, RecvFlags::empty()).unwrap();
    assert_eq!(output.len(), 3);
    assert_eq!(&buffer[..3], b"abc");

    let mut detach = DetachFilter::new();
    detach.set(0);
    socket.set_option(&detach).unwrap();
    assert!(
        socket
            .set_option(&detach)
            .is_err_and(|err| err.error() == Errno::ENOENT)
    );
    NetlinkUeventProtocol::multicast(GroupIdSet::new(1), message).unwrap();
    let mut writer = VmWriter::from(buffer.as_mut_slice()).to_fallible();
    let (output, _) = socket.try_recv(&mut writer, RecvFlags::empty()).unwrap();
    assert_eq!(output.len(), 6);
    assert_eq!(&buffer[..6], b"abcdef");
}

#[ktest]
fn uuid() {
    let uuid = Uuid::from_str("12345678-1234-1234-1234-123456789012");
    assert!(uuid.is_ok());

    let uuid = Uuid::from_str("12345678-1234-1234-1234-12345678901");
    assert!(uuid.is_err());

    let uuid = Uuid::from_str("12345678-1234-1234-1234-1234567890g");
    assert!(uuid.is_err());
}

#[ktest]
fn synthetic_uevent() {
    let uevent = SyntheticUevent::from_str("add");
    assert!(uevent.is_ok());

    let uevent = SyntheticUevent::from_str("add 12345678-1234-1234-1234-123456789012");
    assert!(uevent.is_ok());

    let uevent = SyntheticUevent::from_str("add 12345678-1234-1234-1234-123456789012 NAME=lo");
    assert!(uevent.is_ok());
}

#[ktest]
fn multicast_synthetic_uevent() {
    crate::net::socket::netlink::init();

    // Creates a new netlink uevent socket and joins the group for kobject uevents.
    let socket = NetlinkUeventSocket::new(true, SockType::SOCK_DGRAM);
    let socket_addr = SocketAddr::Netlink(NetlinkSocketAddr::new(100, GroupIdSet::new(0x1)));
    socket.bind(socket_addr).unwrap();

    // Tries to receive and returns EAGAIN if no message is available.
    let mut buffer = vec![0u8; 1024];
    let mut writer = VmWriter::from(buffer.as_mut_slice()).to_fallible();
    let flags = RecvFlags::empty();
    let res = socket.try_recv(&mut writer, flags);
    assert!(res.is_err_and(|err| err.error() == Errno::EAGAIN));

    // Broadcasts a uevent message.
    let uevent = {
        let lo_infos = vec![
            ("INTERFACE".to_string(), "lo".to_string()),
            ("IFINDEX".to_string(), "1".to_string()),
        ];
        let synth_uevent = SyntheticUevent::from_str("add").unwrap();
        Uevent::new_from_syn(
            synth_uevent,
            "/devices/virtual/net/lo".to_string(),
            "net".to_string(),
            lo_infos,
        )
    };
    let uevent_message =
        UeventMessage::new(uevent, NetlinkSocketAddr::new(0, GroupIdSet::new(0x1)));
    NetlinkUeventProtocol::multicast(GroupIdSet::new(0x1), uevent_message).unwrap();

    let (output, _) = socket.try_recv(&mut writer, flags).unwrap();
    assert!(output.flags().is_empty());
    let s = core::str::from_utf8(&buffer[..output.len()]).unwrap();

    assert_eq!(
        s,
        "add@/devices/virtual/net/lo\0ACTION=add\0DEVPATH=/devices/virtual/net/lo\0SUBSYSTEM=net\0SYNTH_UUID=0\0INTERFACE=lo\0IFINDEX=1\0SEQNUM=1\0"
    );
}
