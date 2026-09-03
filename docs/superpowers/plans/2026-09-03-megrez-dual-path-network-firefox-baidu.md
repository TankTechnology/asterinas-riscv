# Megrez Dual-Path Network and Firefox Baidu Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove proxy and direct web access on the physical Milk-V Megrez with the same layered network and Firefox/Baidu acceptance contract.

**Architecture:** Extend the existing Debian M5 evidence and Megrez physical gate with an explicit `proxy` or `direct` network mode, but preserve the browser-free desktop and legacy NetSurf targets. Reuse the current deterministic fixture and Firefox/Marionette evidence, add one owned host-to-Clash proxy bridge, and publish mode-qualified serial, JSON, packet, and screenshot evidence so one mode can never satisfy the other.

**Tech Stack:** Python 3 `unittest`, Bash guest evidence, systemd guest services, curl/ip/getent, Firefox 143 RISC-V with Marionette, QEMU virt SMP=4 with SLIRP, Asterinas EIC7700 GMAC, U-Boot serial boot, Clash HTTP proxy, socat, tcpdump/tshark.

---

## File structure

- `tools/riscv/debian/rootfs/desktop_m5_network_gate.py`: shared mode and ordered network-layer classifier; this remains independent of board orchestration.
- `tools/riscv/debian/rootfs/desktop_m5_network_evidence.sh`: guest-side proxy/direct probes and bounded failure markers.
- `tools/riscv/megrez_proxy_bridge.py`: owned, bounded host listener that forwards the board-visible endpoint to Clash and exposes lifecycle evidence.
- `tools/riscv/megrez_gmac_gate.py`: physical boot arguments, mode separation, artifact identity, serial classification, proxy/fixture lifecycle, and result publication.
- `tools/riscv/debian/rootfs/browser_web_firefox.sh`: mode-specific Firefox proxy preferences without changing the desktop baseline.
- `tools/riscv/debian/rootfs/browser_web_evidence.sh`: process, input, stability, and network-mode evidence.
- `tools/riscv/debian/rootfs/browser_web_marionette_gate.py`: the single Baidu DOM, logo, search, result, and screenshot acceptance implementation.
- `tools/riscv/debian/rootfs/browser_web_qemu_gate.py`: SMP=4 proxy/direct QEMU orchestration and negative-case classification.
- `tools/riscv/debian/rootfs/build_rootfs.sh`: install the updated scripts and service environment into the existing `browser-web` image.
- `Makefile`: explicit proxy/direct QEMU entry points and focused unit suite wiring.
- `tools/riscv/README.md`: operator commands, evidence layout, recovery contract, and failure taxonomy.

### Task 1: Define one mode-qualified network evidence contract

**Files:**
- Modify: `tools/riscv/debian/rootfs/desktop_m5_network_gate.py`
- Modify: `tools/riscv/tests/test_debian_m5_network.py`

- [ ] **Step 1: Write RED tests for modes, ordered layers, and isolation**

Add imports and fixtures that construct one transcript per mode:

```python
from tools.riscv.debian.rootfs.desktop_m5_network_gate import (
    NETWORK_LAYERS,
    NetworkMode,
    classify_web_network,
)


def web_network_transcript(mode: NetworkMode) -> bytes:
    records = [
        f"DEBIAN_WEB_NETWORK_LAYER mode={mode.value} layer={layer} status=pass"
        for layer in NETWORK_LAYERS
    ]
    records.append(
        f"DEBIAN_WEB_NETWORK_READY mode={mode.value} layers={len(NETWORK_LAYERS)}"
    )
    return ("\n".join(records) + "\n").encode()
```

Require `proxy` and `direct` to pass only their matching classifier. Delete one layer, duplicate one layer, swap two layers, append a foreign-mode ready marker, and add `DEBIAN_WEB_NETWORK_FAIL`; each mutation must fail with a stable reason.

- [ ] **Step 2: Run the focused RED tests**

Run:

```bash
python3 -m unittest \
  tools.riscv.tests.test_debian_m5_network.DebianDesktopM5NetworkTests.test_web_network_modes_are_isolated \
  tools.riscv.tests.test_debian_m5_network.DebianDesktopM5NetworkTests.test_web_network_layers_are_unique_and_ordered \
  tools.riscv.tests.test_debian_m5_network.DebianDesktopM5NetworkTests.test_web_network_failure_is_layer_qualified -v
```

Expected: `ERROR` because `NetworkMode` and `classify_web_network` do not exist.

- [ ] **Step 3: Implement the minimal typed classifier**

Add this contract without removing the legacy M5 constants:

```python
from enum import Enum


class NetworkMode(str, Enum):
    PROXY = "proxy"
    DIRECT = "direct"

    def __str__(self) -> str:
        return self.value


NETWORK_LAYERS = (
    "link", "address", "neighbor", "reachability", "dns",
    "http", "https", "baidu-asset", "repeat", "medium",
)


def classify_web_network(transcript: bytes, *, mode: NetworkMode) -> GateResult:
    if not isinstance(transcript, bytes):
        return GateResult(False, "web network transcript must be bytes", None)
    failure = re.search(
        rb"DEBIAN_WEB_NETWORK_FAIL mode=([a-z]+) layer=([a-z-]+) reason=([^\r\n ]+)",
        transcript,
    )
    if failure is not None:
        return GateResult(
            False,
            f"web network {failure.group(2).decode()} failure: "
            f"{failure.group(3).decode()}",
            None,
        )
    positions = []
    for layer in NETWORK_LAYERS:
        marker = (
            f"DEBIAN_WEB_NETWORK_LAYER mode={mode.value} "
            f"layer={layer} status=pass"
        ).encode()
        if transcript.count(marker) != 1:
            return GateResult(False, f"missing or duplicate {layer} layer", None)
        positions.append(transcript.find(marker))
    ready = (
        f"DEBIAN_WEB_NETWORK_READY mode={mode.value} layers={len(NETWORK_LAYERS)}"
    ).encode()
    if transcript.count(ready) != 1:
        return GateResult(False, "missing or duplicate mode-qualified ready", None)
    positions.append(transcript.find(ready))
    if positions != sorted(positions):
        return GateResult(False, "web network layers out of order", None)
    foreign = NetworkMode.DIRECT if mode is NetworkMode.PROXY else NetworkMode.PROXY
    if f"DEBIAN_WEB_NETWORK_READY mode={foreign.value} ".encode() in transcript:
        return GateResult(False, "mixed web network modes", None)
    return GateResult(True, "pass", None)
```

Also add `import re` and reject unknown failure modes/layers before returning the layer-specific reason.

- [ ] **Step 4: Run GREEN and the legacy classifier tests**

Run:

```bash
python3 -m unittest tools.riscv.tests.test_debian_m5_network -v
```

Expected: all tests pass; existing `DESKTOP_M5_MEGREZ_MILESTONES` and QEMU M5 tests remain unchanged.

- [ ] **Step 5: Commit the contract**

```bash
git add tools/riscv/debian/rootfs/desktop_m5_network_gate.py \
  tools/riscv/tests/test_debian_m5_network.py
git commit -m "test(riscv): define dual-path web network contract"
```

### Task 2: Make the guest network evidence mode-aware and diagnosable

**Files:**
- Modify: `tools/riscv/debian/rootfs/desktop_m5_network_evidence.sh`
- Modify: `tools/riscv/tests/test_debian_m5_network.py`

- [ ] **Step 1: Write RED script-harness tests for both modes**

Extend the existing fake `ip`, `curl`, `getent`, `ping`, `stat`, and `sha256sum` harness. Invoke the script with `ASTERINAS_WEB_NETWORK_MODE=proxy` and assert all ten markers plus `READY mode=proxy`. Invoke it with `direct`, no proxy variables, and assert `curl` never receives `--proxy` and the output ends in `READY mode=direct`.

Add table-driven command failures:

```python
cases = {
    "ip-link": ("link", "carrier"),
    "ip-address": ("address", "static-address"),
    "ip-neighbor": ("neighbor", "neighbor-unusable"),
    "ping": ("reachability", "icmp-timeout"),
    "getent": ("dns", "resolve"),
    "curl-http": ("http", "connect"),
    "curl-https-connect": ("https", "tcp-connect"),
    "curl-https-tls": ("https", "tls"),
    "curl-asset": ("baidu-asset", "content"),
    "curl-repeat": ("repeat", "digest"),
    "curl-medium": ("medium", "length"),
}
```

For each case require exactly one record:

```text
DEBIAN_WEB_NETWORK_FAIL mode=<mode> layer=<layer> reason=<reason>
```

- [ ] **Step 2: Run RED**

Run:

```bash
python3 -m unittest \
  tools.riscv.tests.test_debian_m5_network.DebianDesktopM5NetworkTests.test_proxy_web_network_evidence \
  tools.riscv.tests.test_debian_m5_network.DebianDesktopM5NetworkTests.test_direct_web_network_has_no_proxy_configuration \
  tools.riscv.tests.test_debian_m5_network.DebianDesktopM5NetworkTests.test_web_network_failure_taxonomy -v
```

Expected: failures because the script does not read `ASTERINAS_WEB_NETWORK_MODE` and does not emit the new records.

- [ ] **Step 3: Validate mode and configuration before touching the network**

Add these variables and fail-closed rules:

```bash
readonly NETWORK_MODE="${ASTERINAS_WEB_NETWORK_MODE:-}"
readonly GATEWAY="${ASTERINAS_WEB_NETWORK_GATEWAY:-10.100.16.1}"
readonly RESOLVER="${ASTERINAS_WEB_NETWORK_RESOLVER:-}"
readonly MEDIUM_URL="${ASTERINAS_WEB_NETWORK_MEDIUM_URL:-}"
readonly MEDIUM_SIZE="${ASTERINAS_WEB_NETWORK_MEDIUM_SIZE:-262144}"
readonly MEDIUM_SHA256="${ASTERINAS_WEB_NETWORK_MEDIUM_SHA256:-}"

case "$NETWORK_MODE" in
    proxy)
        [[ -n "$PROXY_URL" && -n "$PROXY_HOST" && -n "$PROXY_PORT" ]] ||
            fail_web config missing-proxy
        ;;
    direct)
        [[ -z "$PROXY_URL" && -z "$PROXY_HOST" && -z "$PROXY_PORT" ]] ||
            fail_web config proxy-present
        [[ "$RESOLVER" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] ||
            fail_web config invalid-resolver
        ;;
    *) fail_web config invalid-mode ;;
esac
```

Implement `emit_layer` and `fail_web` so every new record includes the selected mode. Keep the existing legacy M5 marker path behind its current environment contract so old tests and existing images remain valid.

- [ ] **Step 4: Implement the ten bounded probes**

Use a single `deadline=$((SECONDS + TIMEOUT_SECONDS))`. Each external command must be wrapped by the smaller of the remaining deadline and `COMMAND_TIMEOUT_SECONDS`. Run these operations in order:

1. `ip -o link show dev eth0` and require `UP,LOWER_UP`.
2. `ip -o -4 addr show dev eth0 scope global` and require `10.100.19.200/21` on Megrez or the explicit QEMU address.
3. `ip neigh show to "$peer" dev eth0` and reject `FAILED`, `INCOMPLETE`, or missing lladdr.
4. `ping -4 -c 1 -W 3 "$peer"`.
5. Direct mode writes the validated resolver and runs `getent ahostsv4 www.baidu.com`; proxy mode emits `delegation=proxy` after the proxy socket probe.
6. Curl the deterministic fixture over plain HTTP with `--noproxy '*'`.
7. Curl `https://www.baidu.com/`, with `--proxy "$PROXY_URL"` only in proxy mode, and capture `%{http_code}`, `%{time_connect}`, and `%{time_appconnect}`.
8. Download the Baidu logo, require the PNG signature `89504e470d0a1a0a`, and reject an empty payload.
9. Reuse `stress_fixture` for 20 exact 65,536-byte SHA-256-verified responses.
10. Download the 262,144-byte fixture asset once and verify length and SHA-256.

On curl failure, map exit code `5/6` to `dns`, `7` to `tcp-connect`, `28` to `timeout`, `35/51/58/59/60/77/80/82/83/90/91` to `tls`, `22` to `http-status`, and every content mismatch to `content`.

- [ ] **Step 5: Run GREEN, Bash syntax, and shellcheck if installed**

```bash
python3 -m unittest tools.riscv.tests.test_debian_m5_network -v
bash -n tools/riscv/debian/rootfs/desktop_m5_network_evidence.sh
if command -v shellcheck >/dev/null; then
  shellcheck tools/riscv/debian/rootfs/desktop_m5_network_evidence.sh
fi
```

Expected: all unit tests pass and both static checks exit zero.

- [ ] **Step 6: Commit the guest evidence**

```bash
git add tools/riscv/debian/rootfs/desktop_m5_network_evidence.sh \
  tools/riscv/tests/test_debian_m5_network.py
git commit -m "feat(riscv): probe proxy and direct web paths"
```

### Task 3: Own the host-to-Clash proxy bridge lifecycle

**Files:**
- Create: `tools/riscv/megrez_proxy_bridge.py`
- Create: `tools/riscv/tests/test_megrez_proxy_bridge.py`
- Modify: `Makefile`

- [ ] **Step 1: Write RED lifecycle and negative-path tests**

Test an injected subprocess factory and socket probe. Require:

- upstream `127.0.0.1:17892` is reachable before any listener starts;
- board listener `10.100.19.216:17893` must be unused;
- argv is exactly `socat TCP-LISTEN:17893,bind=10.100.19.216,reuseaddr,fork TCP:127.0.0.1:17892`;
- readiness waits are bounded;
- `close()` sends terminate, waits, then kills only the owned child if necessary;
- startup failure, signal, context-body failure, and repeated close leave no child;
- an absent upstream returns stable reason `proxy-upstream-unavailable`;
- an occupied board listener returns stable reason `proxy-listener-in-use`.

- [ ] **Step 2: Run RED**

```bash
python3 -m unittest tools.riscv.tests.test_megrez_proxy_bridge -v
```

Expected: import failure because the module does not exist.

- [ ] **Step 3: Implement the context-managed bridge**

Create immutable configuration and one owner:

```python
@dataclass(frozen=True)
class ProxyBridgeConfig:
    listen_address: str = "10.100.19.216"
    listen_port: int = 17893
    upstream_address: str = "127.0.0.1"
    upstream_port: int = 17892
    startup_timeout: float = 5.0
    shutdown_timeout: float = 2.0


class ProxyBridge:
    def __enter__(self) -> "ProxyBridge":
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
```

Validate both addresses with `ipaddress.ip_address`, both ports in `1..65535`, and finite positive timeouts. Use `subprocess.Popen` without a shell, `start_new_session=True`, and pipe stderr into a bounded 64 KiB diagnostic buffer. `summary()` returns listener/upstream identities, child PID, readiness, and exit status; it never exposes unrelated environment variables.

- [ ] **Step 4: Run GREEN and residue checks**

```bash
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_megrez_proxy_bridge -v
pgrep -af 'socat.*17893' && exit 1 || true
```

Expected: tests pass and no test-owned socat remains.

- [ ] **Step 5: Wire the unit target and commit**

Add `tools.riscv.tests.test_megrez_proxy_bridge` to `test_riscv_megrez_gmac_unit`, then run it and commit:

```bash
make test_riscv_megrez_gmac_unit
git add Makefile tools/riscv/megrez_proxy_bridge.py \
  tools/riscv/tests/test_megrez_proxy_bridge.py
git commit -m "test(riscv): own the Megrez Clash proxy bridge"
```

### Task 4: Add explicit network mode to the protected physical gate

**Files:**
- Modify: `tools/riscv/megrez_gmac_gate.py`
- Modify: `tools/riscv/tests/test_megrez_gmac_gate.py`

- [ ] **Step 1: Write RED argument, bootarg, result, and cleanup tests**

Require `--network-mode proxy|direct` for `--target network` and the new `--target firefox`. Assert:

```python
proxy = physical_bootargs(
    reboot_after=600,
    target=GateTarget.FIREFOX,
    network_mode=NetworkMode.PROXY,
)
direct = physical_bootargs(
    reboot_after=600,
    target=GateTarget.FIREFOX,
    network_mode=NetworkMode.DIRECT,
)
self.assertIn("ASTERINAS_WEB_NETWORK_MODE=proxy", proxy)
self.assertIn("ASTERINAS_DESKTOP_PROXY_URL=", proxy)
self.assertIn("ASTERINAS_WEB_NETWORK_MODE=direct", direct)
self.assertNotIn("ASTERINAS_DESKTOP_PROXY_", direct)
self.assertLess(len(f'setenv bootargs "{proxy}"'.encode()), 1024)
self.assertLess(len(f'setenv bootargs "{direct}"'.encode()), 1024)
```

Add transcript tests proving proxy output cannot pass a direct `GateConfig`, and direct output cannot pass proxy. Assert `result.json` contains `network_mode`, artifact CRCs, fixture summary, proxy summary only in proxy mode, and `recovery_observed`.

- [ ] **Step 2: Run RED**

```bash
python3 -m unittest tools.riscv.tests.test_megrez_gmac_gate -v
```

Expected: failures for missing `FIREFOX`, `network_mode`, and mode-aware classifier.

- [ ] **Step 3: Extend the state machine without changing legacy targets**

Add `GateTarget.FIREFOX = "firefox"` and `network_mode: NetworkMode | None` to `GateConfig`. Enforce:

```python
if self.target in (GateTarget.NETWORK, GateTarget.FIREFOX):
    if not isinstance(self.network_mode, NetworkMode):
        raise ValueError("network target requires a NetworkMode")
elif self.network_mode is not None:
    raise ValueError("network mode requires network or firefox target")
```

For proxy mode include proxy variables and both host/gateway pinned neighbors. For direct mode omit every proxy variable, require the CLI's validated `--resolver-address`, include gateway neighbor and resolver, and pass `ASTERINAS_WEB_NETWORK_MODE=direct`. Do not assume the gateway is a DNS server. Preserve `target=desktop` and legacy `target=browser` behavior byte-for-byte.

- [ ] **Step 4: Use mode-qualified classifiers and lifecycle**

For network target, stop at `DEBIAN_WEB_NETWORK_READY mode=<mode> layers=10`. For Firefox target, require the matching network ready marker followed by the Firefox marker defined in Task 7. Create `ProxyBridge` only for proxy mode, but create `FixtureServer` for both modes. Close both before publishing; include summaries in JSON even on failures.

After the target ready marker, retain serial ownership until either a fresh U-Boot recovery epoch is observed or the configured recovery deadline expires. A passing web marker without recovery evidence must yield `passed=false`, `reason=automatic recovery not observed`; it must never request physical reset implicitly.

- [ ] **Step 5: Run GREEN and commit**

```bash
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_megrez_gmac_gate \
  tools.riscv.tests.test_megrez_board_session -v
git add tools/riscv/megrez_gmac_gate.py \
  tools/riscv/tests/test_megrez_gmac_gate.py
git commit -m "feat(riscv): separate Megrez proxy and direct gates"
```

### Task 5: Add QEMU proxy/direct and negative network gates

**Files:**
- Modify: `tools/riscv/debian/rootfs/desktop_m5_qemu_gate.py`
- Modify: `tools/riscv/tests/test_debian_m5_network.py`
- Modify: `Makefile`

- [ ] **Step 1: Write RED argv and negative-classifier tests**

Require `--network-mode proxy|direct` and `--expect-failure none|proxy-unavailable|dns|tls`. Both modes must use `--smp 4`. Proxy bootargs point to `10.0.2.2:17893`; direct bootargs contain no `PROXY` or curl proxy arguments. Negative transcripts pass only when they contain the exact expected layer/reason and no ready marker.

- [ ] **Step 2: Run RED**

```bash
python3 -m unittest \
  tools.riscv.tests.test_debian_m5_network.DebianDesktopM5NetworkTests.test_qemu_web_network_mode_argv \
  tools.riscv.tests.test_debian_m5_network.DebianDesktopM5NetworkTests.test_qemu_web_network_negative_cases -v
```

Expected: argument parsing and classifier failures.

- [ ] **Step 3: Implement mode-specific QEMU orchestration**

Reuse `DesktopM5QemuOperations`, `FixtureServer`, and `ProxyBridge`; configure the proxy bridge listener for QEMU as `0.0.0.0:17893` and Clash upstream as `127.0.0.1:17892`. For `proxy-unavailable`, do not start the bridge and require `layer=https reason=proxy-unavailable`. For `dns`, inject an unreachable resolver. For `tls`, use the repository TLS fixture with an untrusted certificate. Every negative run remains bounded and publishes `passed=true`, `expected_failure=<case>`, and the classified guest failure.

- [ ] **Step 4: Add explicit Make targets**

Add:

```make
.PHONY: test_riscv_debian_web_network_proxy_qemu
test_riscv_debian_web_network_proxy_qemu:
	@$(MAKE) --no-print-directory test_riscv_debian_desktop_m5_qemu_gate \
		DEBIAN_DESKTOP_M5_QEMU_GATE_TARGET=network \
		DEBIAN_WEB_NETWORK_MODE=proxy

.PHONY: test_riscv_debian_web_network_direct_qemu
test_riscv_debian_web_network_direct_qemu:
	@$(MAKE) --no-print-directory test_riscv_debian_desktop_m5_qemu_gate \
		DEBIAN_DESKTOP_M5_QEMU_GATE_TARGET=network \
		DEBIAN_WEB_NETWORK_MODE=direct
```

Pass `--network-mode "$(DEBIAN_WEB_NETWORK_MODE)"` in the underlying command and keep `--smp 4` fixed.

- [ ] **Step 5: Run GREEN and commit**

```bash
python3 -m unittest tools.riscv.tests.test_debian_m5_network -v
make -n test_riscv_debian_web_network_proxy_qemu \
  DEBIAN_KERNEL=/k DEBIAN_UBOOT=/u DEBIAN_DTB=/d \
  DEBIAN_STAGE1_INITRAMFS=/i DEBIAN_ROOT_IMAGE=/r \
  DEBIAN_ROOT_MANIFEST=/m DEBIAN_PACKAGES_LOCK=/l \
  DEBIAN_PACKAGE_CHECKSUMS=/c DEBIAN_DESKTOP_M5_QEMU_GATE_OUTPUT=/o
git add Makefile tools/riscv/debian/rootfs/desktop_m5_qemu_gate.py \
  tools/riscv/tests/test_debian_m5_network.py
git commit -m "test(riscv): gate both web paths in QEMU"
```

### Task 6: Configure Firefox proxy and direct modes without profile leakage

**Files:**
- Modify: `tools/riscv/debian/rootfs/browser_web_firefox.sh`
- Modify: `tools/riscv/tests/test_debian_browser_web.py`

- [ ] **Step 1: Write RED launch-profile tests**

Run the wrapper with fake Firefox and `ASTERINAS_WEB_NETWORK_MODE=proxy`; require generated `user.js` to contain:

```javascript
user_pref("network.proxy.type", 1);
user_pref("network.proxy.http", "10.100.19.216");
user_pref("network.proxy.http_port", 17893);
user_pref("network.proxy.ssl", "10.100.19.216");
user_pref("network.proxy.ssl_port", 17893);
user_pref("network.proxy.no_proxies_on", "localhost, 127.0.0.1");
```

Run direct mode after proxy mode against the same test directory and require all `network.proxy.*` preferences to be absent except `network.proxy.type=0`. Invalid mode, missing proxy host, non-numeric port, and port above 65535 must fail before Firefox exec.

- [ ] **Step 2: Run RED**

```bash
python3 -m unittest \
  tools.riscv.tests.test_debian_browser_web.BrowserWebContractTests.test_firefox_proxy_profile_is_explicit \
  tools.riscv.tests.test_debian_browser_web.BrowserWebContractTests.test_firefox_direct_profile_removes_proxy_state -v
```

Expected: proxy preferences are absent and the direct cleanup assertion fails.

- [ ] **Step 3: Generate mode-specific preferences atomically**

Validate mode and scalar values, create `user.js.tmp` with mode-independent preferences, append the six proxy preferences only in proxy mode, append `network.proxy.type=0` in direct mode, set mode `0600`, then `mv -T` over `user.js`. Do not inherit `http_proxy`, `https_proxy`, `all_proxy`, or uppercase variants into Firefox; use only the validated profile values.

Export `ASTERINAS_FIREFOX_WEB_NETWORK_MODE=$NETWORK_MODE` to the child so the evidence process can prove the selected mode from `/proc/<pid>/environ`.

- [ ] **Step 4: Run GREEN and commit**

```bash
python3 -m unittest tools.riscv.tests.test_debian_browser_web -v
bash -n tools/riscv/debian/rootfs/browser_web_firefox.sh
git add tools/riscv/debian/rootfs/browser_web_firefox.sh \
  tools/riscv/tests/test_debian_browser_web.py
git commit -m "feat(riscv): isolate Firefox network profiles"
```

### Task 7: Use one Firefox/Baidu acceptance contract for both modes

**Files:**
- Modify: `tools/riscv/debian/rootfs/browser_web_marionette_gate.py`
- Modify: `tools/riscv/debian/rootfs/browser_web_evidence.sh`
- Modify: `tools/riscv/debian/rootfs/browser_web_qemu_gate.py`
- Modify: `tools/riscv/tests/test_debian_browser_web.py`

- [ ] **Step 1: Write RED DOM, process, input, stability, and mode tests**

Extend the Baidu snapshot fixture with a stable logo signal:

```python
"baiduLogo": True,
```

Require `validate_baidu_home` to accept `#lg img`, `img[src*="baidu"]`, or a completed image whose accessible name contains `百度`/`Baidu`. Require the search result snapshot URL to be under `https://www.baidu.com/s`, contain query `Asterinas`, expose at least one result container, and include the query in title or body text.

Add transcript tests for the exact final marker:

```text
DEBIAN_FIREFOX_BAIDU_READY mode=proxy home=pass logo=pass search=pass input=pass stable=pass screenshot=baidu-search.png
```

and its `mode=direct` counterpart. Mixed modes, missing content process, missing logo, detached keyboard/pointer, Firefox exit, service restart, and observation timeout must fail.

- [ ] **Step 2: Run RED**

```bash
python3 -m unittest \
  tools.riscv.tests.test_debian_browser_web.BrowserWebContractTests.test_baidu_home_requires_logo \
  tools.riscv.tests.test_debian_browser_web.BrowserWebContractTests.test_baidu_search_contains_fixed_query \
  tools.riscv.tests.test_debian_browser_web.BrowserWebContractTests.test_mode_qualified_firefox_ready_marker -v
```

Expected: missing logo field and ready-marker support.

- [ ] **Step 3: Strengthen the bounded Marionette probe**

Add this DOM field to `_PROBE_SCRIPT` and `_SNAPSHOT_SCRIPT`:

```javascript
baiduLogo: (() => {
  const candidates = Array.from(document.querySelectorAll(
    '#lg img, img[src*="baidu" i], img[alt*="Baidu" i], img[alt*="百度"]'
  )).slice(0, 16);
  return candidates.some((image) => image.complete && image.naturalWidth > 0);
})(),
```

Keep the node count bounded at 16. Make `validate_baidu_home` require keyword input, submit control, and logo. Keep `Asterinas` as the single fixed query and use the existing `_submit_baidu_search` DOM path rather than typing a search URL directly.

- [ ] **Step 4: Add guest process/input/stability evidence**

In `browser_web_evidence.sh`, after Marionette succeeds:

- confirm the parent and at least one `-contentproc` child with `kill -0`;
- require both `/dev/input/event0` and `/dev/input/event1` and the existing Xorg evdev log markers;
- require systemd `NRestarts=0` and active service state;
- observe the same parent PID for 60 seconds with five-second bounded polls;
- require `baidu-search.png` is a regular non-symlink file and begins with the PNG signature;
- upload `baidu-search.png` with a bounded `curl --noproxy '*' --data-binary` request to the fixture's dedicated `/browser-quality/capture.png` endpoint (kept separate from the existing XWD capture endpoint);
- emit the one mode-qualified ready marker only after all checks pass.

Keep the existing security, TLS, fixture, and Bilibili evidence as additional coverage; they do not replace the Baidu acceptance marker.

- [ ] **Step 5: Make the host/QEMU validator mode-aware**

Add `network_mode` to `validate_web_evidence` and `classify_browser_web_qemu`. Require the final marker to match the requested mode and require the Firefox environment/profile evidence to show proxy enabled only for proxy mode. Include `network_mode` and copied `baidu-search.png` in `result.json`. For the physical gate, require the fixture summary to contain exactly one bounded capture and write those immutable bytes as `baidu-search.png` through `PinnedOutputDirectory`; never obtain the screenshot by mounting or mutating the board root filesystem.

- [ ] **Step 6: Run GREEN and commit**

```bash
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_debian_browser_web -v
bash -n tools/riscv/debian/rootfs/browser_web_evidence.sh
git add tools/riscv/debian/rootfs/browser_web_marionette_gate.py \
  tools/riscv/debian/rootfs/browser_web_evidence.sh \
  tools/riscv/debian/rootfs/browser_web_qemu_gate.py \
  tools/riscv/tests/test_debian_browser_web.py
git commit -m "test(riscv): share Firefox Baidu acceptance across modes"
```

### Task 8: Install the new contract into one reproducible browser root image

**Files:**
- Modify: `tools/riscv/debian/rootfs/build_rootfs.sh`
- Modify: `tools/riscv/debian/rootfs/contract.py`
- Modify: `tools/riscv/debian/rootfs/profiles.py`
- Modify: `tools/riscv/debian/rootfs/browser_web_evidence.service`
- Modify: `tools/riscv/tests/test_debian_rootfs.py`
- Modify: `tools/riscv/tests/test_debian_browser_web.py`

- [x] **Step 1: Write RED image-manifest tests**

Require the `browser-web` profile to install its executable network/Firefox gates, the evidence service, Firefox, CA bundle, curl, iproute2, iputils-ping, and xdotool. Bind the host-only network classifier and every installed mode-aware gate input into one manifest runtime digest. Require the evidence service to start after both `network-online.target` and the online `asterinas-desktop-m5.service` provider, with no hard dependency added to the desktop-only target. The older plan name `asterinas-desktop-m4-core-evidence.service` does not exist, and its actual browser-free M4 evidence payload is not valid for this online M5 image.

- [x] **Step 2: Run RED**

```bash
python3 -m unittest \
  tools.riscv.tests.test_debian_rootfs \
  tools.riscv.tests.test_debian_browser_web -v
```

Expected: image contract failure for the new classifier/version inputs.

- [x] **Step 3: Install and version all mode-aware inputs**

Keep `desktop_m5_network_gate.py` on the host, where its QEMU orchestration dependency closure exists, and bind it into `browser-web-runtime` together with `desktop_m5_network_evidence.sh`, the Firefox wrapper, Marionette gates, browser evidence, and their units. Require that SHA-256 in schema 7 manifests and add only the missing runtime package. Keep finite service timeouts without setting a default network mode or proxy endpoint in the image.

- [x] **Step 4: Run GREEN and commit**

```bash
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_debian_rootfs \
  tools.riscv.tests.test_debian_browser_web -v
bash -n tools/riscv/debian/rootfs/build_rootfs.sh
git add tools/riscv/debian/rootfs/build_rootfs.sh \
  tools/riscv/debian/rootfs/contract.py \
  tools/riscv/debian/rootfs/profiles.py \
  tools/riscv/debian/rootfs/browser_web_evidence.service \
  tools/riscv/tests/test_debian_rootfs.py \
  tools/riscv/tests/test_debian_browser_web.py
git commit -m "build(riscv): package dual-path Firefox evidence"
```

### Task 9: Run the complete local static and unit funnel once

**Files:**
- Runtime output only: `target/megrez-web/2026-09-03/local/`

- [ ] **Step 1: Record source and tool identity**

```bash
mkdir -p target/megrez-web/2026-09-03/local
git rev-parse HEAD | tee target/megrez-web/2026-09-03/local/head.txt
python3 --version | tee target/megrez-web/2026-09-03/local/python.txt
qemu-system-riscv64 --version | head -1 | \
  tee target/megrez-web/2026-09-03/local/qemu.txt
```

- [ ] **Step 2: Run focused suites with warnings promoted**

```bash
python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_megrez_proxy_bridge \
  tools.riscv.tests.test_megrez_gmac_gate \
  tools.riscv.tests.test_debian_m5_network \
  tools.riscv.tests.test_debian_browser_web -v 2>&1 | \
  tee target/megrez-web/2026-09-03/local/focused.log
```

Expected: zero failures/errors and no resource warnings.

- [ ] **Step 3: Run repository-level RISC-V unit targets**

```bash
make test_riscv_megrez_gmac_unit 2>&1 | \
  tee target/megrez-web/2026-09-03/local/gmac-unit.log
make test_riscv_debian_rootfs_unit 2>&1 | \
  tee target/megrez-web/2026-09-03/local/rootfs-unit.log
```

Expected: both targets exit zero. Do not rerun a target whose complete log and HEAD identity already prove the current commit passed.

- [ ] **Step 4: Run formatting/static checks**

```bash
python3 -m ruff check \
  tools/riscv/megrez_proxy_bridge.py \
  tools/riscv/megrez_gmac_gate.py \
  tools/riscv/debian/rootfs/desktop_m5_network_gate.py \
  tools/riscv/debian/rootfs/browser_web_qemu_gate.py \
  tools/riscv/debian/rootfs/browser_web_marionette_gate.py \
  tools/riscv/tests/test_megrez_proxy_bridge.py \
  tools/riscv/tests/test_megrez_gmac_gate.py \
  tools/riscv/tests/test_debian_m5_network.py \
  tools/riscv/tests/test_debian_browser_web.py
git diff --check
```

Expected: all commands exit zero.

### Task 10: Build once and complete the SMP=4 QEMU funnel

**Files:**
- Produce: `target/debian-riscv/browser-web/rootfs/`
- Produce: `target/megrez-web/2026-09-03/qemu/`

- [ ] **Step 1: Reuse or rebuild one browser-web image**

First verify the existing manifest against current build inputs. If verification succeeds, preserve it. If it fails because source inputs changed, build exactly once in the pinned development container using the already-audited binfmt boundary and Clash endpoint `127.0.0.1:17892`:

```bash
tools/riscv/debian/rootfs/build_rootfs.sh --profile browser-web
python3 -m tools.riscv.debian.rootfs.contract verify \
  --image target/debian-riscv/browser-web/rootfs/debian-root.ext2 \
  --manifest target/debian-riscv/browser-web/rootfs/rootfs-manifest.json \
  --packages-lock target/debian-riscv/browser-web/rootfs/packages.lock
```

Expected: signature/package/image verification passes. Never register binfmt on the host.

- [ ] **Step 2: Freeze artifact identities**

Write SHA-256 and size for kernel, U-Boot, Sv39 4-hart DTB, Stage1 initramfs, root image, manifest, package lock, and checksums into `target/megrez-web/2026-09-03/qemu/artifacts.sha256`. Reject Sv48 or one-hart artifacts.

- [ ] **Step 3: Run network proxy and its unavailable negative case**

Use `make test_riscv_debian_web_network_proxy_qemu` with a fresh `proxy-network` output. Then run the same gate with `--expect-failure proxy-unavailable` and no bridge. Expected: the positive result has `passed=true`, `network_mode=proxy`, ten layers; the negative result has `passed=true`, `expected_failure=proxy-unavailable`, and guest failure `layer=https reason=proxy-unavailable`.

- [ ] **Step 4: Run Firefox/Baidu through the proxy**

Run `browser_web_qemu_gate.py --network-mode proxy --smp 4` against the same frozen root image. Expected: mode-qualified network ready, Firefox parent/content processes, Marionette, Baidu logo, fixed search result, input devices, 60-second stability, strict TLS, and valid `baidu-search.png`.

- [ ] **Step 5: Run direct network and negative DNS/TLS cases**

Run the direct gate through QEMU SLIRP, then the DNS and TLS negative cases. Expected: direct positive has no proxy settings in cmdline, environment, curl records, or Firefox profile; negative results name their exact failing layer without a ready marker.

- [ ] **Step 6: Run Firefox/Baidu directly**

Run `browser_web_qemu_gate.py --network-mode direct --smp 4`. Expected: the identical Firefox/Baidu contract passes and evidence shows no proxy state.

- [ ] **Step 7: Audit process and listener cleanup**

```bash
python3 -m tools.riscv.qemu_process_cleanup --check-only
pgrep -af 'qemu-system-riscv64|socat.*17893' && exit 1 || true
```

Expected: no owned QEMU or bridge process remains. Preserve all passing evidence; do not repeat a passing QEMU run.

### Task 11: Complete M1 with one protected physical proxy-network boot

**Files:**
- Produce: `target/megrez-web/2026-09-03/physical/m1-proxy-network/`

- [ ] **Step 1: Run read-only physical preflight**

Require the stable serial by-id device, host RJ45 carrier, Clash `127.0.0.1:17892`, free board proxy/fixture ports, unused board IP, frozen artifact hashes matching Task 10, at least 2 GiB free output space, and no serial owner. Resolve the direct-mode DNS server from the host's active link using `resolvectl dns <interface>` or an explicitly supplied IPv4 resolver; reject loopback, link-local, multicast, and unspecified addresses, and record the selected value without changing host DNS. Start bounded tcpdump with BPF `host 10.100.19.200` and a fixed capture size. If any check fails, do not open serial or touch U-Boot.

- [ ] **Step 2: Execute exactly one recovery-armed network boot**

Run `megrez_gmac_gate.py` with `--target network --network-mode proxy --reboot-after 300`, a 240-second acceptance timeout, and a 360-second recovery deadline. Use volatile U-Boot commands only; first set `stdin=serial` for the current session so USB keyboard bytes cannot split an FDT command. Do not save the environment.

- [ ] **Step 3: Classify M1 evidence**

Require all ten proxy layers, 20 fixture responses, medium transfer length/digest, Baidu HTTPS/logo, no fatal GMAC marker, and a fresh recovery epoch. Parse the latest `ASTERINAS_GMAC_DATAPATH`, `ASTERINAS_GMAC_MTL_RX_LOSS`, `ASTERINAS_GMAC_RX_CLASS`, and `ASTERINAS_GMAC_TX_CLASS` records into `result.json`; non-zero MTL FIFO overflow, receive-buffer-unavailable growth without RX progress, or fatal bus error fails M1.

- [ ] **Step 4: Inspect packet evidence once**

Use tshark to summarize ARP, TCP handshakes, resets, retransmissions, zero windows, proxy CONNECT/HTTP flows, and fixture request count. Attach the summary to the M1 directory. A failed M1 selects the named layer for a code/QEMU fix; it does not trigger an immediate identical board reboot.

### Task 12: Complete M2 with one protected physical proxy-Firefox boot

**Files:**
- Produce: `target/megrez-web/2026-09-03/physical/m2-proxy-firefox/`

- [ ] **Step 1: Check the M1 dependency and artifact identity**

Require M1 `passed=true` and exact kernel/DTB/initramfs/rootfs hashes. Refuse the run if artifacts differ or if the board address is already in use.

- [ ] **Step 2: Execute one Firefox target boot**

Run the physical gate with `--target firefox --network-mode proxy --reboot-after 900`, a 780-second acceptance timeout, and a 960-second recovery deadline. Keep serial and tcpdump capture active while the framebuffer screenshot is copied into the output directory.

- [ ] **Step 3: Classify M2 evidence**

Require M1 network records from this artifact set, Firefox parent/content processes, Marionette session, Baidu final URL/title/logo, fixed `Asterinas` search result, keyboard/pointer attachment, 60-second stable PID/no restart, strict TLS, valid screenshot, and automatic recovery. M2 passes only with `DEBIAN_FIREFOX_BAIDU_READY mode=proxy`.

### Task 13: Complete M3 and M4 with separate direct physical boots

**Files:**
- Produce: `target/megrez-web/2026-09-03/physical/m3-direct-network/`
- Produce: `target/megrez-web/2026-09-03/physical/m4-direct-firefox/`

- [ ] **Step 1: Prove direct preflight contains no proxy state**

Generate bootargs and result metadata before opening serial. Reject any occurrence of `proxy`, port `17893`, `http_proxy`, `https_proxy`, or Firefox proxy preferences in the direct configuration. Keep the fixture listener because repeated and medium deterministic transfers are part of both network contracts.

- [ ] **Step 2: Execute and classify M3 once**

Run `--target network --network-mode direct --reboot-after 300`. Require gateway neighbor/reachability, direct DNS, public HTTP/HTTPS, Baidu logo, repeat/medium checks, GMAC diagnostics, and automatic recovery. Packet evidence must show direct destination sockets rather than board-to-host proxy traffic.

- [ ] **Step 3: Execute and classify M4 once**

Only after M3 passes with the frozen artifacts, run `--target firefox --network-mode direct --reboot-after 900`. Require the identical M2 browser contract, the direct ready marker, no proxy state in Firefox profile/environment, screenshot, and automatic recovery.

- [ ] **Step 4: Run the requirement-by-requirement completion audit**

Create `target/megrez-web/2026-09-03/completion.json` containing M1–M4 result paths, SHA-256 identities, mode, network layer list, browser acceptance fields, screenshot path/hash, recovery evidence, and `passed=true` only if all four current physical results pass. Proxy evidence must not fill direct fields and vice versa.

### Task 14: Document operation, run final review, and integrate

**Files:**
- Modify: `tools/riscv/README.md`
- Modify: `docs/superpowers/plans/2026-09-03-megrez-dual-path-network-firefox-baidu.md`

- [ ] **Step 1: Document exact commands and evidence interpretation**

Add proxy/direct QEMU commands, physical M1–M4 commands, Clash endpoint discovery, owned bridge behavior, serial recovery deadlines, `stdin=serial` workaround, output tree, ten-layer failure taxonomy, Firefox marker, packet-summary commands, and the rule that a passing artifact is reused rather than rerun.

- [ ] **Step 2: Mark completed plan checkboxes from authoritative evidence**

Check only tasks whose commands and outputs exist for the current commit. Link each runtime task to its `result.json`; leave no checked item backed only by intent or an older artifact.

- [ ] **Step 3: Run final local verification**

```bash
make test_riscv_megrez_gmac_unit
make test_riscv_debian_rootfs_unit
python3 -m ruff check tools/riscv
git diff --check
git status --short
```

Expected: tests and static checks pass. `git status --short` lists only the intended README/plan changes before the final commit.

- [ ] **Step 4: Commit documentation and inspect the complete branch**

```bash
git add tools/riscv/README.md \
  docs/superpowers/plans/2026-09-03-megrez-dual-path-network-firefox-baidu.md
git commit -m "docs(riscv): document Megrez dual-path web gates"
git log --oneline --decorate origin/main..HEAD
git diff --stat origin/main...HEAD
```

Expected: the branch contains focused commits for the network contract, guest probes, proxy bridge, physical modes, QEMU modes, Firefox modes, shared browser acceptance, rootfs packaging, and documentation.

- [ ] **Step 5: Integrate only into `asterinas-riscv/main`**

After confirming the repository remote points to `tankTechnology/asterinas-riscv`, update local `main` using a non-destructive fast-forward or reviewed merge, rerun the focused unit suite on the integrated commit, and push that `main`. Do not open or update an upstream Asterinas PR and do not wait for remote CI.
