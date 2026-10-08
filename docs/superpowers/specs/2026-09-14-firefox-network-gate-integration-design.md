# Firefox Network Gate Integration Design

## Goal

Integrate the six Firefox/QEMU follow-up changes from
`origin/feat/ipv6-dual-stack-sockets` onto the current RISC-V mainline without
merging obsolete branch history, then prove that Asterinas can boot a desktop,
launch Firefox, and load deterministic text content plus selected public HTTPS
pages through both bounded network modes.

## Scope

The network kernel and IPv6 dual-stack socket work is already present on
`main`. The remaining candidate commits are:

- `7aa97a485` — budget the layered QEMU network gate;
- `9d869a607` — serialize Firefox after the network gate;
- `9c194292c` — complete the owned fixture before Firefox starts;
- `cd6c4593a` — enlarge the Firefox page budget under TCG;
- `c80ebb9c6` — reduce Firefox content-process pressure; and
- `4e2229d11` — retain asynchronous public-page navigation.

Each change will be evaluated against the current code and replayed as a
focused change. No merge commit or unrelated history from the remote branch
will be imported. The existing Stage-1 debug-console `SIGTERM` race is a
separate follow-up commit because it does not participate in the browser data
path.

## Architecture

The browser boot path remains a sequence of independently observable stages:

1. boot the kernel and mount the immutable browser root;
2. qualify the selected proxy or direct network path with the owned fixture;
3. start Xorg and the lightweight desktop;
4. launch one bounded Firefox workload;
5. validate deterministic local text/search/download behavior; and
6. validate selected live HTTPS pages without weakening TLS or anti-bot
   classification.

Firefox will not compete with the 20-request network fixture on constrained
RISC-V TCG. The browser service will start only after the bounded network gate
has completed successfully. Firefox will retain asynchronous navigation, but
every readiness probe will have its own monotonic deadline.

## Timeout and observability contract

The previous remote proposal of merely increasing the page timeout to 1200
seconds is not sufficient. The final implementation will keep one outer
safety deadline while exposing smaller named phase deadlines for network,
Firefox launch, Marionette connection, deterministic fixture interaction, and
each public-site interaction.

The host QEMU runner will publish serial progress while the VM is running and
will retain the transcript and a structured failure result even when a phase
times out. A timeout result must identify the last completed marker and the
active phase. Direct mode must terminate within its declared bound instead of
waiting for the generic 7200-second boot timeout.

## Browser resource policy

The low-memory preferences from the remote branch will be admitted only if
the evidence gate continues to prove a separate sandboxed content process.
The configuration may limit prelaunch and optional RDD/Fission processes, but
must not disable the content-process boundary, TLS verification, or normal
cookie/storage behavior.

## Acceptance criteria

The implementation is complete when all of the following hold:

- the existing host browser/network regression suites pass;
- new tests fail before and pass after each timeout, sequencing, and progress
  publication change;
- the frozen Firefox JIT root builds and passes its schema-seven contract;
- proxy-mode QEMU reaches the deterministic text page, Firefox capability
  checks, and the live-page platform-ready marker;
- direct-mode QEMU either reaches the same browser boundary or exits within
  its declared deadline with a phase-specific retained result;
- third-party CAPTCHA or anti-automation behavior stays classified separately
  and never becomes a fabricated browser pass; and
- no Docker image or persistent package/Cargo cache is deleted during the
  work.

## Physical result

After the resulting immutable root and kernel are deployed, the Megrez board
should show the existing lightweight desktop and Firefox. A deterministic
local text page must work without external connectivity. Public text/HTTPS
pages should work through the qualified proxy path; direct access additionally
depends on the board receiving a usable address, route, and DNS configuration.
Those dependencies will be reported separately from browser rendering.

## Commit structure

The browser sequencing/resource changes, the bounded host-runner observability
changes, and the unrelated debug-console signal fix will remain separate
commits. After their targeted and integration tests pass, the commits will be
pushed linearly to `origin/main`.
