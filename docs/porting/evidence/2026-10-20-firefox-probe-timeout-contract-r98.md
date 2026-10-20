# Firefox probe timeout contract r98

Date: 2026-10-19T22:20:00Z  
Gate: `firefox-probe-timeout-contract`  
Claim: E3 (supplemental host-side contract; it does not claim Firefox scene success)

## Root cause isolated

The r97 short run used `--boot-timeout=180`.  Its guest probe service allows
300 seconds, and r96 shows the Marionette listener becoming connectable only
around the 165-second mark.  The host therefore terminated r97 before the
guest could publish its terminal probe receipt.  This was a host deadline
contract violation, not evidence that the listener or ppoll permanently
failed.

## Change

`desktop_m4_gate.py` now rejects Firefox probe runs with a host boot deadline
below 360 seconds (300 seconds guest probe budget plus a 60-second publication
margin). The rejection occurs before the serial wait or guest run. The normal
desktop and full-scene paths are unchanged.

## Verification

* A new unit test first failed with an import error because the contract was
  absent, then passed after the minimal helper and call site were added.
* Firefox/DRM and browser regression suite: 229 tests, all passed.
* `python3 -m compileall -q tools/riscv`, `bash -n` for RISC-V shell tools,
  and `git diff --check`: passed.
* No QEMU guest, serial, network, or hardware run was performed for r98.
