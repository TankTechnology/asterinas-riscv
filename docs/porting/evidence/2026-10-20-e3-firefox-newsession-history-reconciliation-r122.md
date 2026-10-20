# E3 Firefox NewSession history reconciliation r122

This is a read-only audit of retained QEMU serial logs.  It does not start
QEMU, Firefox, a guest, or the physical board.

## Findings

1. The retained r34 serial log contains a complete Marionette
   `WebDriver:NewSession` response.  The boundary is explicit:

   ```text
   line 1064: WebDriver:NewSession stage=send send_complete=false
   line 1065: WebDriver:NewSession stage=send send_complete=true
   line 1066: WebDriver:NewSession stage=response_body header_bytes=4 body_expected=745
   line 1067: WebDriver:NewSession stage=complete body_expected=745 body_received=745
   ```

   The recorded monotonic interval is 190.793654200 s to 293.537135300 s.
   This proves a NewSession response boundary in that historical run.

2. r34 is not an E3 pass.  Its structured result is
   `passed=false`, `reason=webgl_context_unavailable_force_enabled_no_effect`,
   with `webgl_context=0`, an empty renderer, and no screenshot or recovery
   receipt.  Therefore the NewSession success is an intermediate boundary,
   not proof of Firefox acceleration.

3. The retained r101 minimal/legacy A/B and r107 envelope receipts report no
   NewSession response within their bounded windows.  r119 additionally
   records the 66-byte request arriving at the guest Marionette listener, but
   no response frame.  These later failures do not contradict r34; they show
   that the runs differ in an input or runtime condition.

4. r34's serial log does not record the JSON capability request body.  Its
   source metadata records the temporary `webgl.force-enabled` gate variant,
   but does not prove that the successful request used the three-field
   capability shape.  The claim that the three fields caused the success is
   therefore still a hypothesis.

## Provenance

| Artifact | SHA-256 |
|---|---|
| `2026-10-16-qemu-firefox-drm-scene-r34/serial.log` | `8ae91e09404922c1571a37b93a01504ef12ad95a82ecdad12c1aa8a486b6a45c` |
| `2026-10-16-qemu-firefox-drm-scene-r34/result.json` | `475c7333fdf848db668c59fc9440967c471965d13e91e8591bd449cf965f12bc` |
| `2026-10-16-qemu-firefox-drm-scene-r34.md` | `a7d1dda0f5f588092bddb246aef00849ae300c8ec68d22453557753d7ff16b1d` |
| `2026-10-20-e3-firefox-newsession-capability-ab-r101.md` | retained A/B boundary record |
| `2026-10-20-e3-firefox-newsession-envelope-r107.md` | retained envelope boundary record |
| `2026-10-20-e3-firefox-user-recv-runtime-r119.md` | retained user-recv boundary record |

## Decision

The historical contradiction is reconciled as **NewSession success in r34,
but capability causality unknown**.  E3 remains failed because no retained
run satisfies the full Firefox scene, renderer, pixel, and recovery criteria.
The next experiment, if approved, should be one bounded A/B probe that records
the exact JSON body and changes only capability shape; it must not be a blind
900-second scene rerun.
