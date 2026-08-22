// SPDX-License-Identifier: MPL-2.0

//! Bounded interrupt-IN report queue for a USB HID boot keyboard.

use alloc::boxed::Box;
use core::{
    array,
    task::{Context, Poll},
};

use usb_if::endpoint::{RequestId, TransferCompletion, TransferStatus};

use super::{BOOT_KEYBOARD_REPORT_LEN, UsbKeyboardError};

pub(super) const REPORT_QUEUE_DEPTH: usize = 8;

pub(super) trait ReportEndpoint {
    fn submit_report(
        &mut self,
        report: &mut [u8; BOOT_KEYBOARD_REPORT_LEN],
    ) -> Result<RequestId, UsbKeyboardError>;

    fn poll_report_request(
        &mut self,
        request: RequestId,
        context: &mut Context<'_>,
    ) -> Poll<Result<TransferCompletion, UsbKeyboardError>>;
}

struct ReportSlot {
    // TransferRequest retains the buffer address, so the pointee must not move while submitted.
    report: Box<[u8; BOOT_KEYBOARD_REPORT_LEN]>,
    request: Option<RequestId>,
}

pub(super) struct BootKeyboardReportQueue {
    slots: [ReportSlot; REPORT_QUEUE_DEPTH],
    next_completion: usize,
}

impl BootKeyboardReportQueue {
    pub(super) fn empty() -> Self {
        Self {
            slots: array::from_fn(|_| ReportSlot {
                report: Box::new([0; BOOT_KEYBOARD_REPORT_LEN]),
                request: None,
            }),
            next_completion: 0,
        }
    }

    pub(super) fn fill(
        &mut self,
        endpoint: &mut impl ReportEndpoint,
    ) -> Result<(), UsbKeyboardError> {
        for slot in &mut self.slots {
            debug_assert!(slot.request.is_none());
            slot.report.fill(0);
            slot.request = Some(endpoint.submit_report(slot.report.as_mut())?);
        }
        Ok(())
    }

    pub(super) fn poll(
        &mut self,
        endpoint: &mut impl ReportEndpoint,
        context: &mut Context<'_>,
    ) -> Result<Option<[u8; BOOT_KEYBOARD_REPORT_LEN]>, UsbKeyboardError> {
        let slot = &mut self.slots[self.next_completion];
        let request = slot.request.ok_or(UsbKeyboardError::Transfer)?;
        let completion = match endpoint.poll_report_request(request, context) {
            Poll::Pending => return Ok(None),
            Poll::Ready(Err(error)) => {
                slot.request = None;
                return Err(error);
            }
            Poll::Ready(Ok(completion)) => {
                slot.request = None;
                completion
            }
        };

        if completion.status != TransferStatus::Completed {
            return Err(UsbKeyboardError::Transfer);
        }
        if completion.actual_length != BOOT_KEYBOARD_REPORT_LEN {
            return Err(UsbKeyboardError::InvalidReportLength);
        }

        let report = *slot.report;
        slot.report.fill(0);
        slot.request = Some(endpoint.submit_report(slot.report.as_mut())?);
        self.next_completion = if self.next_completion + 1 == REPORT_QUEUE_DEPTH {
            0
        } else {
            self.next_completion + 1
        };
        Ok(Some(report))
    }
}

#[cfg(ktest)]
mod tests {
    use alloc::vec::Vec;
    use core::task::{Context, Poll};

    use usb_if::endpoint::{RequestId, TransferCompletion, TransferStatus};

    use super::{BootKeyboardReportQueue, ReportEndpoint};
    use crate::{bus::usb::UsbKeyboardError, prelude::ktest};

    const REPORT_ONE: [u8; super::BOOT_KEYBOARD_REPORT_LEN] = [0, 0, 0x04, 0, 0, 0, 0, 0];
    const REPORT_TWO: [u8; super::BOOT_KEYBOARD_REPORT_LEN] = [0, 0, 0x05, 0, 0, 0, 0, 0];

    fn numbered_report(number: u8) -> [u8; super::BOOT_KEYBOARD_REPORT_LEN] {
        [0, 0, number, 0, 0, 0, 0, 0]
    }

    #[derive(Clone, Copy, Debug, Eq, PartialEq)]
    enum Operation {
        Submit(u64),
        Poll(u64),
    }

    #[derive(Clone, Copy, Debug)]
    struct ScriptedCompletion {
        request: RequestId,
        outcome: Result<(TransferStatus, usize), UsbKeyboardError>,
    }

    struct FakeEndpoint {
        next_id: u64,
        scripted_reports: Vec<[u8; super::BOOT_KEYBOARD_REPORT_LEN]>,
        completions: Vec<ScriptedCompletion>,
        pending: Vec<RequestId>,
        fail_submission: Option<u64>,
        operations: Vec<Operation>,
    }

    impl FakeEndpoint {
        fn with_reports(reports: &[[u8; super::BOOT_KEYBOARD_REPORT_LEN]]) -> Self {
            Self {
                next_id: 1,
                scripted_reports: reports.to_vec(),
                completions: Vec::new(),
                pending: Vec::new(),
                fail_submission: None,
                operations: Vec::new(),
            }
        }

        fn complete(&mut self, request: u64, status: TransferStatus, actual_length: usize) {
            self.completions.push(ScriptedCompletion {
                request: RequestId::new(request),
                outcome: Ok((status, actual_length)),
            });
        }

        fn mark_ready(&mut self, request: u64) {
            self.complete(
                request,
                TransferStatus::Completed,
                super::BOOT_KEYBOARD_REPORT_LEN,
            );
        }

        fn fail_poll(&mut self, request: u64, error: UsbKeyboardError) {
            self.completions.push(ScriptedCompletion {
                request: RequestId::new(request),
                outcome: Err(error),
            });
        }

        fn fail_submission(&mut self, request: u64) {
            self.fail_submission = Some(request);
        }
    }

    impl ReportEndpoint for FakeEndpoint {
        fn submit_report(
            &mut self,
            report: &mut [u8; super::BOOT_KEYBOARD_REPORT_LEN],
        ) -> Result<RequestId, UsbKeyboardError> {
            let raw_id = self.next_id;
            self.next_id += 1;
            self.operations.push(Operation::Submit(raw_id));
            if self.fail_submission == Some(raw_id) {
                return Err(UsbKeyboardError::Transfer);
            }

            *report = self
                .scripted_reports
                .get(raw_id as usize - 1)
                .copied()
                .unwrap_or([0; super::BOOT_KEYBOARD_REPORT_LEN]);
            let request = RequestId::new(raw_id);
            self.pending.push(request);
            Ok(request)
        }

        fn poll_report_request(
            &mut self,
            request: RequestId,
            _context: &mut Context<'_>,
        ) -> Poll<Result<TransferCompletion, UsbKeyboardError>> {
            self.operations.push(Operation::Poll(request.raw()));
            let Some(completion_index) = self
                .completions
                .iter()
                .position(|completion| completion.request == request)
            else {
                return Poll::Pending;
            };

            let scripted = self.completions.remove(completion_index);
            let pending_index = self
                .pending
                .iter()
                .position(|pending| *pending == request)
                .unwrap();
            self.pending.remove(pending_index);

            Poll::Ready(
                scripted
                    .outcome
                    .map(|(status, actual_length)| TransferCompletion {
                        request_id: request,
                        status,
                        actual_length,
                        iso_packets: Vec::new(),
                    }),
            )
        }
    }

    fn poll_first(
        status: TransferStatus,
        actual_length: usize,
    ) -> Result<Option<[u8; super::BOOT_KEYBOARD_REPORT_LEN]>, UsbKeyboardError> {
        let mut queue = BootKeyboardReportQueue::empty();
        let mut endpoint = FakeEndpoint::with_reports(&[REPORT_ONE]);
        queue.fill(&mut endpoint).unwrap();
        endpoint.complete(1, status, actual_length);
        let mut context = Context::from_waker(core::task::Waker::noop());
        queue.poll(&mut endpoint, &mut context)
    }

    #[ktest]
    fn fills_all_receive_slots_before_polling() {
        let mut queue = BootKeyboardReportQueue::empty();
        let mut endpoint = FakeEndpoint::with_reports(&[]);

        queue.fill(&mut endpoint).unwrap();

        assert_eq!(endpoint.pending.len(), super::REPORT_QUEUE_DEPTH);
        assert_eq!(
            endpoint.operations,
            (1..=super::REPORT_QUEUE_DEPTH as u64)
                .map(Operation::Submit)
                .collect::<Vec<_>>()
        );
    }

    #[ktest]
    fn waits_for_the_earliest_submitted_report() {
        let mut queue = BootKeyboardReportQueue::empty();
        let mut endpoint = FakeEndpoint::with_reports(&[REPORT_ONE, REPORT_TWO]);
        queue.fill(&mut endpoint).unwrap();
        endpoint.mark_ready(2);

        let mut context = Context::from_waker(core::task::Waker::noop());
        assert_eq!(queue.poll(&mut endpoint, &mut context), Ok(None));
        assert_eq!(queue.poll(&mut endpoint, &mut context), Ok(None));
        assert_eq!(
            &endpoint.operations[endpoint.operations.len() - 2..],
            &[Operation::Poll(1), Operation::Poll(1)]
        );
        assert_eq!(endpoint.completions.len(), 1);
        assert_eq!(endpoint.completions[0].request, RequestId::new(2));

        endpoint.mark_ready(1);
        assert_eq!(
            queue.poll(&mut endpoint, &mut context),
            Ok(Some(REPORT_ONE))
        );
        assert_eq!(
            queue.poll(&mut endpoint, &mut context),
            Ok(Some(REPORT_TWO))
        );
    }

    #[ktest]
    fn preserves_fifo_order_across_queue_wraparound() {
        let queue_depth = super::REPORT_QUEUE_DEPTH as u64;
        let request_count = queue_depth * 2;
        let reports = (1..=request_count)
            .map(|request| numbered_report(request as u8))
            .collect::<Vec<_>>();
        let mut queue = BootKeyboardReportQueue::empty();
        let mut endpoint = FakeEndpoint::with_reports(&reports);
        queue.fill(&mut endpoint).unwrap();
        let mut context = Context::from_waker(core::task::Waker::noop());

        for request in 1..=request_count {
            endpoint.mark_ready(request);
            assert_eq!(
                queue.poll(&mut endpoint, &mut context),
                Ok(Some(numbered_report(request as u8)))
            );
            assert_eq!(endpoint.pending.len(), super::REPORT_QUEUE_DEPTH);
        }

        assert_eq!(
            endpoint.operations,
            (1..=queue_depth)
                .map(Operation::Submit)
                .chain((1..=request_count).flat_map(|request| {
                    [
                        Operation::Poll(request),
                        Operation::Submit(request + queue_depth),
                    ]
                }))
                .collect::<Vec<_>>()
        );
    }

    #[ktest]
    fn rejects_non_completed_transfer_statuses() {
        for status in [
            TransferStatus::Stalled,
            TransferStatus::Cancelled,
            TransferStatus::Error,
        ] {
            assert_eq!(
                poll_first(status, super::BOOT_KEYBOARD_REPORT_LEN),
                Err(UsbKeyboardError::Transfer)
            );
        }
    }

    #[ktest]
    fn rejects_short_and_oversized_reports() {
        for actual_length in [
            super::BOOT_KEYBOARD_REPORT_LEN - 1,
            super::BOOT_KEYBOARD_REPORT_LEN + 1,
        ] {
            assert_eq!(
                poll_first(TransferStatus::Completed, actual_length),
                Err(UsbKeyboardError::InvalidReportLength)
            );
        }
    }

    #[ktest]
    fn propagates_endpoint_poll_errors() {
        let mut queue = BootKeyboardReportQueue::empty();
        let mut endpoint = FakeEndpoint::with_reports(&[REPORT_ONE]);
        queue.fill(&mut endpoint).unwrap();
        endpoint.fail_poll(1, UsbKeyboardError::Enumeration);

        let mut context = Context::from_waker(core::task::Waker::noop());
        assert_eq!(
            queue.poll(&mut endpoint, &mut context),
            Err(UsbKeyboardError::Enumeration)
        );
        assert_eq!(endpoint.pending.len(), super::REPORT_QUEUE_DEPTH - 1);
        assert_eq!(endpoint.operations.last(), Some(&Operation::Poll(1)));
    }

    #[ktest]
    fn replenishes_a_completed_slot_before_delivery() {
        let mut queue = BootKeyboardReportQueue::empty();
        let mut endpoint = FakeEndpoint::with_reports(&[REPORT_ONE]);
        queue.fill(&mut endpoint).unwrap();
        endpoint.mark_ready(1);

        let mut context = Context::from_waker(core::task::Waker::noop());
        assert_eq!(
            queue.poll(&mut endpoint, &mut context),
            Ok(Some(REPORT_ONE))
        );
        assert_eq!(endpoint.pending.len(), super::REPORT_QUEUE_DEPTH);
        assert_eq!(
            &endpoint.operations[endpoint.operations.len() - 2..],
            &[Operation::Poll(1), Operation::Submit(9)]
        );
    }

    #[ktest]
    fn stops_when_replenishing_a_slot_fails() {
        let mut queue = BootKeyboardReportQueue::empty();
        let mut endpoint = FakeEndpoint::with_reports(&[REPORT_ONE]);
        queue.fill(&mut endpoint).unwrap();
        endpoint.mark_ready(1);
        endpoint.fail_submission(9);

        let mut context = Context::from_waker(core::task::Waker::noop());
        assert_eq!(
            queue.poll(&mut endpoint, &mut context),
            Err(UsbKeyboardError::Transfer)
        );
        assert_eq!(endpoint.pending.len(), super::REPORT_QUEUE_DEPTH - 1);
        assert_eq!(
            &endpoint.operations[endpoint.operations.len() - 2..],
            &[Operation::Poll(1), Operation::Submit(9)]
        );
        assert_eq!(endpoint.next_id, 10);
    }
}
