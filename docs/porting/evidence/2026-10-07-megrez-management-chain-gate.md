# Megrez management-chain gate, 2026-10-07

Status: **RockOS reachable; Asterinas cycle blocked before U-Boot handoff**

This gate was attempted before any new Asterinas boot. It records the
authentication boundary so a control failure is not mistaken for a board
hang.

## Observed state

- The board answered the existing-key SSH check at `10.100.19.200` with UID
  1000, RockOS kernel `6.6.87`, boot ID
  `147da1ab-ae1c-4ff6-b5f9-95f84e7a5f05`, and about ten minutes of uptime.
- The stable serial path was acquired exclusively for each attempt and was
  released afterward; no competing serial owner was observed.
- The deployed read-only menu candidate was audited locally and over SSH as
  `asterinas-menu-6e3ad5a9c0ae.conf`, SHA-256
  `6e3ad5a9c0ae0d081e576e17d3164e93ae489b2e87532d02e7e36ea75908099d`.
- No U-Boot command, Asterinas kernel, watchdog arm, device Probe, DMA, or
  network test was executed in this gate.

## Reboot-control boundary

The SSH user-level `systemctl reboot` request was rejected by polkit. The
documented serial `sudo -k -s` root-shell handshake also did not complete, so
the runner did not issue `reboot`. A final serial `systemctl reboot` request
entered an authentication prompt and was stopped by the bounded timeout. These
attempts produced no board reset and no persistent configuration change.

The historical RockOS recovery record remains valid as a procedure, but its
root authentication receipt is from an earlier epoch. The current epoch has
only a verified UID-1000 SSH identity; it does not yet have a fresh UID-0
control receipt. The Basic cycle therefore remains **not run**, and DWC3,
GMAC-0, and GMAC-1 must not be started from this state.

## Required unblock

Provide or restore the current RockOS authenticated root path through the
existing password-input workflow, without recording the credential in source
or evidence. Then one serial owner must hold the entire sequence: authenticated
RockOS reboot, fresh OpenSBI/U-Boot observation, menu selection, watchdog
arming, guest result, recovery, and RockOS boot-id verification. No temporary
listener may be allowed to leave U-Boot unattended.
