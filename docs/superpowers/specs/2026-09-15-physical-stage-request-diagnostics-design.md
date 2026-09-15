# Physical Stage Request Diagnostics Design

## Decision

Keep the existing human interaction contract: after the local Firefox page is
ready, the operator types one four-digit code and clicks the amber button.
The 180-second interaction bound and fail-closed U-Boot recovery do not change.
This change adds only bounded diagnostics at the browser-to-loopback-HTTP
boundary implicated by the `physical-graphics-stage-regression` run.

## Evidence

The stage server emits one structured UART line for each `/stage` request:
cycle, received stage, previous accepted stage, HTTP status, and a bounded
reason. It never prints the raw nonce or untrusted query string; it records
whether the received nonce matched the expected one. A missing `key` request
therefore becomes distinguishable from a rejected `key` request. The page
retains a bounded list of stage-fetch outcomes and error names in a separate
DOM element, without changing the trusted-input snapshot or ordered stage
contract. On a stage-server error, the guest witness makes one bounded,
read-only Marionette query for that element and emits its sanitized value
alongside raw evdev counts before preserving the original failure.

## Verification

Tests cover accepted, malformed, nonce-mismatched, and out-of-order HTTP
requests; browser first-fetch failure replay; read-only post-READY access;
and preservation of the original error when the diagnostic query fails.
Rebuild Stage1 deterministically, run the three-cycle QEMU gate, then perform
one physical cycle with a fresh output directory. Do not claim a kernel fix
from instrumentation alone.
