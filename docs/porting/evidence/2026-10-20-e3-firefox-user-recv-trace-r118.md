# Firefox NewSession user-recv trace instrumentation r118

- gate: `e3-firefox-user-recv-trace`
- claim: E3 supplemental kernel diagnostic
- status: `pass` for instrumentation build/contract; no runtime claim
- scope: add one opt-in BigTCP boundary at `TcpConnection::recv`

The existing r117 evidence showed that the Marionette client completed the
NewSession write but the TCP trace did not identify whether the Firefox-side
socket consumed the request.  This change adds
`ASTERINAS_TCP_TRACE stage=user-recv` at the userspace `recv` return boundary.
It records only the connection identity, bytes consumed, and remaining receive
queue, and is emitted only when `asterinas.tcp_diagnostic_port` is enabled.
No networking behavior, scheduling, or packet contents change.

## Verification

- standalone policy test: **3 passed** (including the stable `user-recv` stage name)
- rustfmt on both changed Rust files: passed
- `git diff --check`: passed
- full `cargo test -p aster-bigtcp` was attempted in the persistent container but
  remains an environment/configuration failure before this crate's tests: the
  default host build lacks the workspace's `acpi`, `x86_64`, `x86`, `tdx_guest`,
  and `unwinding` target dependencies. No test failure was attributed to this
  change.

The next bounded runtime can now distinguish “request reached Firefox's TCP
receive queue and was consumed” from “request never reached the user socket”.
No QEMU or Firefox run was started in r118.

