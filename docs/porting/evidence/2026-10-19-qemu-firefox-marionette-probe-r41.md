# Firefox Marionette independent probe r41

r41 is the single bounded runtime probe using the explicitly overlaid
select-based Marionette client. The overlay binding was verified before the
run, so this result is a runtime test of that client rather than r38's stale
guest copy.

## Result

The guest TCP path still works: the Marionette greeting is delivered and the
complete 66-byte NewSession request reaches the Firefox listener. No response
header is generated. The client exits at the same short response boundary
after the select-based wait, so replacing `SO_RCVTIMEO` alone does not resolve
the stall. The Firefox parent remains alive with 50 threads; its child
processes continue to poll/futex while the NewSession handler does not
complete.

Markers:

```text
FIREFOX_DRM_PROBE_BOUNDARY name=greeting status=pass
ASTERINAS_TCP_TRACE seq=17 stage=segment-generated local_port=33690 remote_port=2828 value0=66
ASTERINAS_TCP_TRACE seq=18 stage=peer-process local_port=2828 remote_port=33690 value0=66 value2=66
A_WEB_MARIONETTE_TRANSPORT {"event":"send_complete","command":"WebDriver:NewSession","request_id":1}
A_WEB_MARIONETTE_TRANSPORT {"event":"failure","command":"WebDriver:NewSession","stage":"response_header","error_type":"TimeoutError"}
```

No scene, screenshot, WebRender, frame-rate, or recovery phase was run.

## Artifacts

| Artifact | SHA-256 |
|---|---|
| serial log | `be2e5ef7c0f2de79593d6a4dea46f21349cb4328fb2127cba910b32eb75bfd42` |
| result | `ac4d1c2a5e424ee4b4b73856c4e388d70d2f395468ff4c6fc784889d428b69dd` |
| lifecycle | `59067c57c9b758f7e81057f20cb9a763f5c1dd57355e3e58de2d8f1733c7d9dd` |
| root image | `c8bd5b2508f453bb4ac630d215738832a062f9158e55d9b9f6f2e801a44e8550` |
| root manifest | `d2b6c2061f224f7b1b832a86c83eecbc6e3f954f98e12b27d9364abb54eac0c9` |
| kernel | `829f9fa348d15bc3826c5543aea0ade0408c241d816e4d48db53de4007249297` |

Output group: `/mnt/mydisk/qemu-firefox-marionette-probe-r41-container/`

Gate: `firefox-marionette-probe`; claim: `E3`; status: `blocked`.
