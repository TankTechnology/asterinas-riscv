// SPDX-License-Identifier: MPL-2.0

pub(crate) const RX_POLL_BUDGET: usize = 32;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum PollEndAction {
    Rearm,
    Reschedule,
    Stop,
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub(crate) struct RxPollStats {
    pub(crate) received: u64,
    pub(crate) budget_exhaustions: u64,
    pub(crate) reschedules: u64,
    pub(crate) plic_rearms: u64,
}

#[derive(Debug, Default)]
pub(crate) struct RxPollBudget {
    processed: usize,
    stats: RxPollStats,
    reported_received: u64,
    reported_reschedules: u64,
}

impl RxPollBudget {
    pub(crate) fn can_receive(&self) -> bool {
        self.processed < RX_POLL_BUDGET
    }

    pub(crate) fn record_received(&mut self) {
        debug_assert!(self.can_receive());
        self.processed += 1;
        self.stats.received = self.stats.received.saturating_add(1);
    }

    pub(crate) fn finish(&mut self, fatal: bool, more_rx: bool) -> PollEndAction {
        if self.processed == RX_POLL_BUDGET {
            self.stats.budget_exhaustions = self.stats.budget_exhaustions.saturating_add(1);
        }
        self.processed = 0;
        if fatal {
            PollEndAction::Stop
        } else if more_rx {
            self.stats.reschedules = self.stats.reschedules.saturating_add(1);
            PollEndAction::Reschedule
        } else {
            PollEndAction::Rearm
        }
    }

    pub(crate) fn record_rearmed(&mut self) -> Option<RxPollStats> {
        self.stats.plic_rearms = self.stats.plic_rearms.saturating_add(1);
        if self.reported_reschedules == self.stats.reschedules {
            return None;
        }
        self.reported_reschedules = self.stats.reschedules;
        Some(self.stats)
    }

    pub(crate) fn take_progress_report(&mut self) -> Option<RxPollStats> {
        if !should_report_progress(self.reported_received, self.stats.received) {
            return None;
        }
        self.reported_received = self.stats.received;
        Some(self.stats)
    }

    #[cfg(test)]
    pub(crate) fn stats(&self) -> RxPollStats {
        self.stats
    }
}

fn should_report_progress(previous: u64, current: u64) -> bool {
    if current <= previous || previous == u64::MAX {
        return false;
    }
    let Some(threshold) = (previous + 1).checked_next_power_of_two() else {
        return false;
    };
    current >= threshold
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

    #[test]
    fn exhausted_poll_counts_reschedule_and_reports_after_rearm() {
        let mut budget = RxPollBudget::default();
        for _ in 0..RX_POLL_BUDGET {
            budget.record_received();
        }

        assert_eq!(budget.finish(false, true), PollEndAction::Reschedule);
        assert_eq!(
            budget.record_rearmed().unwrap(),
            RxPollStats {
                received: RX_POLL_BUDGET as u64,
                budget_exhaustions: 1,
                reschedules: 1,
                plic_rearms: 1,
            }
        );
    }

    #[test]
    fn ordinary_rearms_do_not_emit_redundant_reports() {
        let mut budget = RxPollBudget::default();
        budget.record_received();
        assert_eq!(budget.finish(false, false), PollEndAction::Rearm);
        assert_eq!(budget.record_rearmed(), None);
        assert_eq!(budget.stats().plic_rearms, 1);
    }

    #[test]
    fn progress_reports_only_after_crossing_power_of_two_thresholds() {
        assert!(!should_report_progress(0, 0));
        assert!(should_report_progress(0, 1));
        assert!(!should_report_progress(4, 7));
        assert!(should_report_progress(4, 8));
        assert!(should_report_progress(7, 9));
    }

    #[test]
    fn progress_cadence_rejects_duplicate_regression_and_saturation() {
        assert!(!should_report_progress(8, 8));
        assert!(!should_report_progress(8, 7));
        assert!(!should_report_progress(u64::MAX, u64::MAX));
        assert!(!should_report_progress(u64::MAX - 1, u64::MAX));
    }

    #[test]
    fn progress_snapshot_is_consumed_once_per_threshold_crossing() {
        let mut budget = RxPollBudget::default();
        for _ in 0..7 {
            budget.record_received();
        }
        assert_eq!(budget.take_progress_report().unwrap().received, 7);
        assert_eq!(budget.take_progress_report(), None);
        budget.record_received();
        assert_eq!(budget.take_progress_report().unwrap().received, 8);
        assert_eq!(budget.take_progress_report(), None);
    }
}
