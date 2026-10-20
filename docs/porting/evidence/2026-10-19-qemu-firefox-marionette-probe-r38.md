# Firefox Marionette independent probe r38

This is the single bounded NewSession probe after the TCP diagnostic policy was
changed to suppress repetitive `pending-pop` records. It does not run the
scene, screenshot, WebRender, or recovery gates.

## Result

The guest TCP path is active: the probe reaches the Marionette greeting, the
guest generates and delivers both greeting and NewSession response bytes, and
the client sends the complete NewSession request. The request still times out
waiting for its response header, so E3 remains blocked at Firefox's
NewSession/Firefox-process boundary rather than at TCP delivery.

Relevant serial markers:

```text
ASTERINAS_TCP_TRACE seq=11 stage=segment-generated local_port=2828 remote_port=33690 value0=53
ASTERINAS_TCP_TRACE seq=12 stage=peer-process local_port=33690 remote_port=2828 value0=53 value2=53
ASTERINAS_TCP_TRACE seq=17 stage=segment-generated local_port=33690 remote_port=2828 value0=66
ASTERINAS_TCP_TRACE seq=18 stage=peer-process local_port=2828 remote_port=33690 value0=66 value2=66
FIREFOX_DRM_PROBE_BOUNDARY name=greeting status=pass
A_WEB_MARIONETTE_TRANSPORT {"event":"send_complete","command":"WebDriver:NewSession","request_id":1}
A_WEB_MARIONETTE_TRANSPORT {"event":"failure","command":"WebDriver:NewSession","stage":"response_header","error_type":"TimeoutError"}
```

At the terminal sample the Firefox parent remains alive with 52 threads and
the sampled parent thread is in `futex(98)`; the Socket Process remains in
`ppoll(73)`. This is a userspace/NewSession completion boundary. It is not
evidence of a failed TCP send, a missing listener, or a global futex wake
failure.

## Artifacts

| Artifact | SHA-256 |
|---|---|
| serial log | `8c0448b92d74e876a34a30c3d2e2410ecffbb0becbcad5d272023f258bebf021` |
| result | `7cf6fb1f9c3a15cf271c832b3eb73c21d885c2b3217a4d4bb41fa950e2566ddd` |
| lifecycle | `63de6174ab82f5327493fd4548a3094ce803f906936725f4dd09e39b29781599` |
| root image | `c7625d0c104205f37898773372465ac7a5e754a26930c3f490e11e8d2306fd28` |
| kernel input | `829f9fa348d15bc3826c5543aea0ade0408c241d816e4d48db53de4007249297` |

Output group: `/mnt/mydisk/qemu-firefox-marionette-probe-r38-container/`

Gate: `firefox-marionette-probe`; claim: `E3`; status: `blocked`.
