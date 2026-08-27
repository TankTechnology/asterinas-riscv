# Debian M7 Baidu Page Evidence Design

## Goal

Extend the proven Debian M6 browser baseline into an evidence-backed basic
online browsing milestone. Asterinas must boot the signed Debian RISC-V
desktop in QEMU, NetSurf must load the real `https://www.baidu.com/` document,
and the same window must accept and submit a basic search.

M7 does not claim Chromium compatibility. Login, dynamic hot-search content,
complex Web APIs, and complete modern JavaScript behavior remain outside this
milestone.

## Preserved baseline

The M6 evidence remains a prerequisite rather than being replaced:

- Debian 13.6 `desktop-m5-network` identity and all signed rootfs inputs;
- Asterinas generic-Sv39, four-hart, 2 GiB QEMU contract;
- QEMU slirp DNS and certificate-validated HTTPS to `www.baidu.com`;
- the foreground Baidu-hosted PNG screenshot;
- the separate NetSurf local-JavaScript `limited-pass` classification.

The known-good M6 run produced both 1280x1024 screenshots and a passing result.
Its root image SHA-256 is
`86967a2b48fa2164cfda9c3769b4de98b2f588654baf5d69187aa12137384851` and
its kernel SHA-256 is
`9dc0a35ad33c4d45fce786f012f4b2aa36c465624b9966e7fd8a1a44d98673b2`.

## First foundation fix: bounded HMP socket paths

The current gate creates its QEMU session directory below the published output
directory. A documented output path below the repository can therefore exceed
the Unix-domain socket path limit before QEMU starts. The same artifacts pass
when the output is shortened under `/tmp`, proving this is a gate lifecycle
defect rather than a guest or kernel defect.

Each QEMU session will instead use a private `0700` directory created directly
under `/tmp`. The directory contains only run-private U-Boot material, hard
links to the pinned boot/root images, the monitor socket, and screenshots. The
published output directory remains descriptor-pinned and retains all durable
evidence. Teardown removes the private runtime directory on every path.

This makes the monitor path bounded independently of the user-visible output
path without weakening artifact identity or publication guarantees.

## Guest evidence sequence

After the existing M6 ready marker, the root-owned browser evidence service:

1. revalidates that exactly one NetSurf process and one visible NetSurf window
   exist for UID 1000;
2. activates that window;
3. navigates through the address bar to exactly `https://www.baidu.com/`;
4. waits for a bounded non-asset Baidu window title;
5. emits an exact homepage-ready marker and leaves a bounded capture interval;
6. focuses the visible search field at a frozen 1280x1024 page coordinate,
   enters a deterministic ASCII query, and submits it;
7. waits for a bounded search-result title containing the query or the Baidu
   search title, then emits the exact search-ready and final markers.

All external commands retain explicit deadlines and bounded output. Any
ambiguous process/window, navigation timeout, or input failure emits a stable
failure marker.

## Host evidence and classification

A dedicated M7 gate extends the M6 operation rather than weakening it. It
captures:

- `desktop-m7-baidu-home.ppm` during the homepage capture interval;
- `desktop-m7-baidu-search.ppm` after the search-ready marker;
- the fully drained serial transcript;
- input identities, final writable-root hash, QEMU argv, title diagnostics,
  and screenshot metadata in `result.json`.

The classifier requires all M5, M4, M6, and M7 markers exactly once and in
order, rejects the complete fatal-marker set, and rejects missing, duplicated,
or reordered evidence.

The automated gate proves that the requested live page and search interaction
occurred in NetSurf. Visual acceptance separately inspects the homepage frame
for the Baidu logo, a recognizable search box, and basic page text. Pixel
statistics alone are never described as semantic DOM evidence.

## Failure attribution

Failures are separated before any kernel change:

- DNS, TLS, or HTTP failure: network/user-space certificate path;
- NetSurf title or rendering failure with HTTPS success: browser/upstream or
  Debian user-space compatibility;
- stable syscall error in the drained transcript: a focused Asterinas kernel
  compatibility slice with a minimal reproducer;
- blank/corrupt frame with healthy browser evidence: display pipeline;
- launch failure on a long output path: host gate lifecycle regression.

The existing `systemd-sysusers` failure and `fs.nr_open` warning are recorded
as separate compatibility work. They do not invalidate a passing M7 browser
gate and are not mixed into speculative browser fixes.

## Acceptance

M7 is complete when:

- the long repository output path launches successfully;
- the unchanged M6 gate still passes;
- the M7 classifier and lifecycle tests pass;
- one real QEMU run publishes a passing M7 result and both screenshots;
- visual inspection confirms the real homepage has recognizable basic content
  and the second screenshot shows the submitted search result;
- no Docker, QEMU, Unix socket, or temporary runtime directory is left behind.
