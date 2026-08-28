// SPDX-License-Identifier: MPL-2.0

pub(crate) const RX_POLL_BUDGET: usize = 32;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum PollEndAction {
    Rearm,
    Reschedule,
    Stop,
}

#[derive(Debug, Default)]
pub(crate) struct RxPollBudget {
    processed: usize,
}

impl RxPollBudget {
    pub(crate) fn can_receive(&self) -> bool {
        self.processed < RX_POLL_BUDGET
    }

    pub(crate) fn record_received(&mut self) {
        debug_assert!(self.can_receive());
        self.processed += 1;
    }

    pub(crate) fn finish(&mut self, fatal: bool, more_rx: bool) -> PollEndAction {
        self.processed = 0;
        if fatal {
            PollEndAction::Stop
        } else if more_rx {
            PollEndAction::Reschedule
        } else {
            PollEndAction::Rearm
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn exactly_thirty_two_packets_exhaust_the_budget() {
        let mut budget = RxPollBudget::default();
        for _ in 0..RX_POLL_BUDGET {
            assert!(budget.can_receive());
            budget.record_received();
        }
        assert!(!budget.can_receive());
    }

    #[test]
    fn drained_poll_rearms_and_resets_the_budget() {
        let mut budget = RxPollBudget::default();
        budget.record_received();
        assert_eq!(budget.finish(false, false), PollEndAction::Rearm);
        assert!(budget.can_receive());
    }

    #[test]
    fn remaining_rx_reschedules_and_resets_the_budget() {
        let mut budget = RxPollBudget::default();
        budget.record_received();
        assert_eq!(budget.finish(false, true), PollEndAction::Reschedule);
        assert!(budget.can_receive());
    }

    #[test]
    fn fatal_state_stops_even_when_rx_remains() {
        let mut budget = RxPollBudget::default();
        budget.record_received();
        assert_eq!(budget.finish(true, true), PollEndAction::Stop);
        assert!(budget.can_receive());
    }
}
