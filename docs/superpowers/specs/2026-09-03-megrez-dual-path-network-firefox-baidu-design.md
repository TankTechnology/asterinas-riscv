# Megrez Dual-Path Network and Firefox Baidu Design

## Purpose

Make the Debian desktop on the Milk-V Megrez usable for ordinary web access
without weakening the fast, browser-free desktop baseline. The work proceeds
through two independently diagnosable network paths: a host/Clash proxy path
first, followed by direct Internet egress. Both paths converge on the same
Firefox and Baidu acceptance contract.

## Scope and ordering

The work is split into four milestones that must be completed in order:

1. **M1 — proxied network:** the physical GMAC path passes link, address,
   neighbor, transport, DNS, HTTP, HTTPS, asset, repeated-request, and download
   checks through the development host.
2. **M2 — proxied Firefox:** Firefox starts on the physical desktop and loads
   and searches Baidu through the verified proxy path.
3. **M3 — direct network:** the same physical network contract passes without
   an HTTP or SOCKS proxy.
4. **M4 — direct Firefox:** the M2 browser contract passes without a proxy.

M1 and M2 are not evidence for M3 or M4. Direct egress remains a separate
deliverable even after the proxied browser becomes usable.

## Existing baseline

The preserved baseline is the browser-free desktop target introduced by the
Megrez desktop-only work. It boots Xorg with the firmware framebuffer and
starts Openbox, PCManFM, LXPanel, and xterm only after both evdev devices are
available. Network and browser work must not add synchronous dependencies to
this target or increase its acceptance surface.

The current physical network implementation already provides an EIC7700 GMAC
device, a static IPv4 boot contract, pinned neighbor entries, a host proxy and
fixture server, and target-specific serial evidence. These are reused rather
than replaced.

## Architecture

### Network modes

One network test definition supports two explicit modes:

- `proxy`: static RJ45 plus pinned host neighbor; external HTTP and HTTPS use
  the host proxy, while a host fixture supplies deterministic repeated and
  medium-size transfers.
- `direct`: static RJ45 plus gateway neighbor and resolver; HTTP and HTTPS
  connect directly to public endpoints. No proxy variables or browser
  preferences may be present.

Mode selection is explicit in boot arguments, guest evidence, result JSON, and
output-directory identity. A transcript from one mode cannot satisfy the
other mode's classifier.

### Layered network evidence

The network gate reports each boundary separately and fails at the first
unmet boundary:

1. GMAC selected, carrier present, and RX/TX DMA free of fatal status.
2. Static IPv4 address and route match the boot contract.
3. Required neighbor entries are installed and usable.
4. Gateway or host reachability succeeds with a bounded probe.
5. DNS resolves `www.baidu.com` in direct mode; proxy mode records whether
   name resolution is local or delegated to the proxy.
6. Plain HTTP reaches a deterministic endpoint.
7. HTTPS completes TCP and TLS and returns an accepted status.
8. The Baidu logo PNG is downloaded and validated as a non-empty PNG.
9. Repeated fixture requests preserve byte count and SHA-256.
10. A medium-size transfer completes within a bounded time and verifies its
    length and digest.

Failure evidence distinguishes link, route, neighbor, ICMP, DNS, TCP connect,
TLS, HTTP status, content, timeout, and proxy-unavailable failures. Kernel
diagnostics retain GMAC descriptor, RX/TX, MTL-loss, and packet-class counters
for physical failures.

### Proxy ownership

The development host owns the proxy and deterministic fixture. The gate
starts them before boot, proves their listening sockets are bound only to the
selected host address, and tears them down on success, failure, signal, or
timeout. The guest receives only validated IP addresses, ports, URLs, sizes,
and digests.

The proxy-unavailable negative test is host-side and QEMU-based: the gate must
classify a refused or absent proxy distinctly. It does not justify an extra
physical reboot.

### Firefox and Baidu evidence

Firefox uses the existing Debian RISC-V package and Marionette-based evidence
path. Browser validation is identical in proxy and direct modes except for
the network preference:

1. the Firefox parent and at least one content process remain alive;
2. Marionette accepts a bounded connection;
3. `https://www.baidu.com/` reaches a completed document state;
4. the final URL and title belong to Baidu;
5. the page contains the Baidu logo or an equivalent stable landmark;
6. entering a fixed query and submitting it reaches a Baidu result URL;
7. the result page contains the fixed query;
8. keyboard and pointer remain attached to Xorg;
9. Firefox remains alive for a bounded post-load observation window;
10. the gate emits one mode-qualified ready marker and records a screenshot.

The first QEMU pass uses the same Debian root image and guest scripts. Physical
execution begins only after the QEMU classifier and negative cases pass.

## Test strategy

### Static and unit tests

- Parse and validate mode-specific boot arguments.
- Reject proxy settings in direct mode and missing proxy settings in proxy
  mode.
- Verify ordered milestone classification and mode separation.
- Verify every failure reason with synthetic transcripts.
- Verify cleanup and signal handling for proxy and fixture processes.
- Verify the browser evidence parser for home, logo, query, result, crash, and
  timeout outcomes.

### QEMU tests

- Run SMP=4 only.
- Pass the proxy network gate and Firefox/Baidu gate.
- Pass the direct network gate and Firefox/Baidu gate through QEMU SLIRP.
- Run proxy-unavailable, DNS-failure, TLS-failure, and browser-timeout negative
  cases without booting the board.

### Physical tests

Each physical experiment uses pinned kernel, DTB, initramfs, and rootfs
identities, serial capture, a bounded boot timeout, and an automatic software
reboot. Before the first physical run, the gate checks that the board IPv4
address is not already in use and that the selected host interface and proxy
listeners match the contract.

One physical boot is used per milestone unless new evidence identifies a
specific layer that requires another run. Physical results include the serial
transcript, structured result JSON, artifact hashes, network mode, proxy mode,
and screenshot path.

## Acceptance criteria

### M1 — proxied network

- All ten layered network checks pass on the Megrez board.
- Twenty deterministic fixture requests and one medium-size transfer pass
  length and SHA-256 verification.
- Baidu HTTPS and logo PNG pass through the host/Clash proxy.
- The result identifies `mode=proxy` and contains no fatal GMAC diagnostic.

### M2 — proxied Firefox

- M1 evidence is from the same artifact set.
- Firefox satisfies all ten browser checks through the proxy.
- The captured framebuffer shows the Baidu page or result page.

### M3 — direct network

- All ten network checks pass with no proxy settings in boot arguments,
  environment, curl options, or browser profile.
- DNS, Baidu HTTPS, logo PNG, repeated requests, and medium transfer use direct
  sockets.

### M4 — direct Firefox

- M3 evidence is from the same artifact set.
- Firefox satisfies the identical browser contract without a proxy.
- The captured framebuffer shows the Baidu page or result page.

The overall work is complete only when M1 through M4 all have current physical
evidence. A proxy-only success is intentionally not the terminal state.

## Repository and integration policy

Implementation stays in `asterinas-riscv`. Changes are developed and verified
on an isolated `codex/` branch. They are integrated into that repository's
`main` only after the relevant local tests and physical milestone pass. No
interaction with the upstream Asterinas repository or remote CI is required.
