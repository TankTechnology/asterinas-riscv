// SPDX-License-Identifier: MPL-2.0

mod epoll;
mod event_file;
mod event_source;
#[expect(clippy::module_inception)]
mod events;
mod io_events;
mod observer;
mod subject;

pub use self::event_source::EventSource;
pub use self::io_events::IoEvents;
pub(crate) use self::{
    epoll::{EpollCtl, EpollEvent, EpollFile, EpollFlags},
    event_file::{EventFile, EventFileFlags},
    events::{Events, EventsFilter},
    observer::Observer,
    subject::SyncSubject,
};
