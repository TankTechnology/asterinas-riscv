// SPDX-License-Identifier: MPL-2.0
// Injected only into the explicitly derived diagnostic transport module.
let aFfTransportRemaining = 128;
let aFfTransportSequence = 0;
function aFfTransportTrace(
  stage,
  available = 0,
  header = 0,
  expected = 0,
  received = 0,
  request = 0
) {
  if (!aFfEnabled || aFfTransportRemaining <= 0) {
    return;
  }
  --aFfTransportRemaining;
  try {
    const scalar = value => (Number.isSafeInteger(value) ? value : 0);
    dump(
      "A_FF_TRANSPORT " +
        JSON.stringify({
          version: 1,
          stage,
          pid: scalar(Services.appinfo.processID),
          sequence: ++aFfTransportSequence,
          available: scalar(available),
          header: scalar(header),
          expected: scalar(expected),
          received: scalar(received),
          request: scalar(request),
        }) +
        "\n"
    );
  } catch (_) {
    // Diagnostics must not alter transport state or exception propagation.
  }
}
