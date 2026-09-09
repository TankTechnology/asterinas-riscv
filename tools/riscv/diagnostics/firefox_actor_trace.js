// SPDX-License-Identifier: MPL-2.0
// Injected only into an explicitly derived diagnostic Firefox archive.
const aFfEnabled = (() => {
  try {
    return Services.env.get("ASTERINAS_FIREFOX_ACTOR_DIAGNOSTICS") === "1";
  } catch (_) {
    return false;
  }
})();
let aFfRemaining = 128;
function aFfTrace(stage, request = 0, contextFn = null) {
  if (!aFfEnabled || aFfRemaining <= 0) {
    return;
  }
  --aFfRemaining;
  try {
    const scalar = value => Number.isSafeInteger(value) ? value : 0;
    const context = contextFn?.();
    dump("A_FF_ACTOR " + JSON.stringify({
      version: 1,
      stage,
      pid: scalar(Services.appinfo.processID),
      request: scalar(request),
      context: scalar(context?.id),
      target_pid: scalar(context?.currentWindowGlobal?.osPid),
    }) + "\n");
  } catch (_) {
    // Diagnostics must not replace the command's return value or exception.
  }
}
function aFfRequestTrace(stage, request, contextFn = null) {
  if (Number.isSafeInteger(request)) {
    aFfTrace(stage, request, contextFn);
  }
}
