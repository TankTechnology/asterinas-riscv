// SPDX-License-Identifier: MPL-2.0

use crate::{events::IoEvents, prelude::*, process::signal::Pollee, util::bpf::SockFilter};

pub(crate) struct MessageReceiver<Message> {
    message_queue: Arc<Mutex<MessageQueue<Message>>>,
    pollee: Pollee,
}

pub(super) struct MessageQueue<Message> {
    messages: VecDeque<Message>,
    total_length: usize,
    error: Option<Error>,
    filter: Option<Arc<Vec<SockFilter>>>,
}

impl<Message> MessageQueue<Message> {
    /// Creates a pair of a [`MessageQueue`] and a [`MessageReceiver`].
    pub(super) fn new_pair(pollee: Pollee) -> (Arc<Mutex<Self>>, MessageReceiver<Message>) {
        let queue = Arc::new(Mutex::new(Self {
            messages: VecDeque::new(),
            total_length: 0,
            error: None,
            filter: None,
        }));
        let receiver = MessageReceiver {
            message_queue: queue.clone(),
            pollee,
        };
        (queue, receiver)
    }

    pub(super) fn set_filter(&mut self, filter: Option<Arc<Vec<SockFilter>>>) -> bool {
        let removed = self.filter.is_some();
        self.filter = filter;
        removed
    }

    /// Returns whether the message queue is empty.
    pub(super) fn is_empty(&self) -> bool {
        self.messages.is_empty()
    }

    /// Returns whether the message queue contains errors.
    ///
    /// Currently, the message queue contains errors only if the queue is full but the kernel still
    /// wants to enqueue new messages.
    pub(super) fn has_errors(&self) -> bool {
        self.error.is_some()
    }
}

/// Messages that fit into the [`MessageQueue`].
pub(crate) trait QueueableMessage {
    /// Counts and returns the length of the message.
    fn total_len(&self) -> usize;

    /// Applies a packet filter before queue accounting and readiness notification.
    fn apply_filter(&mut self, _program: &[SockFilter]) -> bool {
        false
    }
}

impl<Message: QueueableMessage> MessageQueue<Message> {
    /// Dequeues a message if executing the closure returns `Ok((true, _))`.
    ///
    /// The closure will be executed with a reference to the message that is ready to be dequeued
    /// and the length of the message.
    ///
    /// If the queue contains errors (see [`Self::has_errors`]), the error will be cleared and
    /// returned. In this case, the closure will not be executed.
    pub(super) fn dequeue_if<F, R>(&mut self, f: F) -> Result<R>
    where
        F: FnOnce(&Message, usize) -> Result<(bool, R)>,
    {
        if let Some(error) = self.error.take() {
            return Err(error);
        }

        let Some(message) = self.messages.front() else {
            return_errno_with_message!(Errno::EAGAIN, "the receive buffer is empty");
        };

        let length = message.total_len();
        let (should_pop, result) = f(message, length)?;
        if should_pop {
            self.messages.pop_front().unwrap();
            self.total_length -= length;
        }

        Ok(result)
    }

    /// Enqueues a message and returns an event only for accepted or overflowing packets.
    #[must_use]
    pub(self) fn enqueue(&mut self, mut message: Message) -> Option<IoEvents> {
        if let Some(filter) = &self.filter {
            if !message.apply_filter(filter) {
                return None;
            }
        }
        let length = message.total_len();

        // Currently, we don't support sending netlink messages between user spaces, so only the
        // kernel can enqueue new messages. If the kernel fails to enqueue a new message, `ENOBUFS`
        // will be returned when userspace calls `recv`.
        if NETLINK_DEFAULT_BUF_SIZE - self.total_length < length {
            self.error = Some(Error::with_message(
                Errno::ENOBUFS,
                "the receive buffer is full",
            ));
            return Some(IoEvents::ERR);
        }

        self.messages.push_back(message);
        self.total_length += length;

        Some(IoEvents::IN)
    }
}

impl<Message: QueueableMessage> MessageReceiver<Message> {
    pub(super) fn enqueue_message(&self, message: Message) {
        let event = self.message_queue.lock().enqueue(message);
        if let Some(event) = event {
            self.pollee.notify(event);
        }
    }
}

pub(in crate::net) const NETLINK_DEFAULT_BUF_SIZE: usize = 65536;
