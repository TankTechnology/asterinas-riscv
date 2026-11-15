#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
"""Derive a hash-pinned, opt-in Firefox actor diagnostic archive offline.

This changes a Debian package payload, so the result is experimental, never
an unmodified package or browser acceptance artifact. Source anchors are from
the locally installed Firefox ESR 140.15.0esr-1~deb13u1 omni.ja.
"""

import argparse
import hashlib
import io
import json
from pathlib import Path
import zipfile


SOURCE_SHA256 = "b47a780a07eb4cdc697a9e8b5d3cb7fd82ff9fdec7891dc3ade83463a3988f56"
PREFIX = "chrome/remote/content/marionette/"
SERVER = PREFIX + "server.sys.mjs"
DRIVER = PREFIX + "driver.sys.mjs"
PARENT = PREFIX + "actors/MarionetteCommandsParent.sys.mjs"
CHILD = PREFIX + "actors/MarionetteCommandsChild.sys.mjs"
TRANSPORT = PREFIX + "transport.sys.mjs"
MODULES = (SERVER, DRIVER, PARENT, CHILD, TRANSPORT)


def trace_helper():
    return (
        Path(__file__).with_name("firefox_actor_trace.js").read_text(encoding="utf-8")
    )


def transport_trace_helper():
    return (
        Path(__file__)
        .with_name("firefox_transport_trace.js")
        .read_text(encoding="utf-8")
    )


def replace_once(source, before, after):
    if source.count(before) != 1:
        raise ValueError(f"source anchor must occur exactly once: {before[:80]!r}")
    return source.replace(before, after, 1)


def patch_module(
    name, source, *, helper=None, transport_helper=None, unbuffered_socket=False
):
    """Preserve original awaits, error propagation, payloads and deadlines."""
    if name not in MODULES:
        raise ValueError("module is outside the diagnostic allowlist")
    if helper is None:
        helper = trace_helper()
    if name == TRANSPORT:
        if transport_helper is None:
            transport_helper = transport_trace_helper()
        helper += "\n" + transport_helper
    source = replace_once(source, "const lazy = {};", helper + "\nconst lazy = {};")
    role = {
        SERVER: "server",
        DRIVER: "driver",
        PARENT: "parent",
        CHILD: "child",
        TRANSPORT: "transport",
    }[name]
    source += f'\naFfTrace("{role}.loaded");\n'
    if name == TRANSPORT:
        source = replace_once(
            source,
            "  ready() {\n    this.active = true;\n    this._waitForIncoming();",
            """  ready() {
    this.active = true;
    if (aFfEnabled && !this._aFfProbeTimer) {
      this._aFfProbeTimer = Cc["@mozilla.org/timer;1"].createInstance(
        Ci.nsITimer
      );
      this._aFfProbeTimer.initWithCallback(
        () => {
          try {
            aFfTransportTrace("probe.available", this._input.available());
          } catch (_) {
            aFfTransportTrace("probe.error");
          }
        },
        5000,
        Ci.nsITimer.TYPE_REPEATING_SLACK
      );
    }
    this._waitForIncoming();""",
        )
        source = replace_once(
            source,
            "      this._input.asyncWait(this, 0, 0, threadManager.currentThread);",
            """      this._input.asyncWait(this, 0, 0, threadManager.currentThread);
      aFfTransportTrace("wait.arm");""",
        )
        source = replace_once(
            source,
            "    this.active = false;\n    this._input.close();",
            """    this.active = false;
    if (this._aFfProbeTimer) {
      this._aFfProbeTimer.cancel();
      this._aFfProbeTimer = null;
    }
    this._input.close();""",
        )
        source = replace_once(
            source,
            "  onInputStreamReady(stream) {\n    try {",
            """  onInputStreamReady(stream) {
    aFfTransportTrace("input.ready");
    try {""",
        )
        source = replace_once(
            source,
            '  _processIncoming(stream, count) {\n    dumpv("Data available: " + count);',
            """  _processIncoming(stream, count) {
    aFfTransportTrace("process.enter", count, this._incomingHeader.length,
      this._incoming?.length, this._incoming?._data?.length);
    dumpv("Data available: " + count);""",
        )
        source = replace_once(
            source,
            "        this._incoming = lazy.Packet.fromHeader(this._incomingHeader, this);",
            """        this._incoming = lazy.Packet.fromHeader(this._incomingHeader, this);
        aFfTransportTrace("header.parsed", count, this._incomingHeader.length,
          this._incoming?.length, this._incoming?._data?.length);""",
        )
        source = replace_once(
            source,
            "        this._incoming.read(stream, this._scriptableInput);",
            """        this._incoming.read(stream, this._scriptableInput);
        aFfTransportTrace("process.read", count, this._incomingHeader.length,
          this._incoming?.length, this._incoming?._data?.length);""",
        )
        source = replace_once(
            source,
            "    // Ready for next packet\n    this._flushIncoming();",
            """    // Ready for next packet
    aFfTransportTrace("packet.complete", count, this._incomingHeader.length,
      this._incoming?.length, this._incoming?._data?.length);
    this._flushIncoming();""",
        )
        source = replace_once(
            source,
            "  _onJSONObjectReady(object) {\n    lazy.executeSoon(() => {",
            """  _onJSONObjectReady(object) {
    const aFfPacketRequest = Array.isArray(object) ? object[1] : 0;
    aFfTransportTrace("json.ready", 0, 0, 0, 0, aFfPacketRequest);
    lazy.executeSoon(() => {""",
        )
        source = replace_once(
            source,
            '      if (this.active) {\n        this.emit("packet", object);',
            """      if (this.active) {
        aFfTransportTrace("packet.dispatch", 0, 0, 0, 0, aFfPacketRequest);
        this.emit("packet", object);""",
        )
    elif name == SERVER:
        source = replace_once(
            source,
            "const lazy = {};",
            "const aFfResponses = new WeakSet();\nconst lazy = {};",
        )
        if unbuffered_socket:
            source = replace_once(
                source,
                "clientSocket.openInputStream(0, 0, 0)",
                "clientSocket.openInputStream(Ci.nsITransport.OPEN_UNBUFFERED, 0, 0)"
                ".QueryInterface(Ci.nsIAsyncInputStream)",
            )
            source = replace_once(
                source,
                "clientSocket.openOutputStream(0, 0, 0)",
                "clientSocket.openOutputStream(Ci.nsITransport.OPEN_UNBUFFERED, 0, 0)"
                ".QueryInterface(Ci.nsIAsyncOutputStream)",
            )
        source = replace_once(
            source,
            "    let resp = this.createResponse(cmd.id);",
            """    let resp = this.createResponse(cmd.id);
    if (aFfEnabled && cmd.name === "WebDriver:ExecuteScript") {
      aFfResponses.add(resp);
      aFfRequestTrace("server.command", cmd.id);
    }""",
        )
        source = replace_once(
            source,
            "    let rv = await fn.bind(this.driver)(cmd);",
            """    let rv = await fn.bind(this.driver)(cmd);
    if (aFfResponses.has(resp)) aFfRequestTrace("server.driver_complete", cmd.id);""",
        )
        source = replace_once(
            source,
            "    this.sendRaw(payload);",
            """    if (aFfResponses.has(msg)) aFfRequestTrace("server.response_queue", msg.id);
    this.sendRaw(payload);
    if (aFfResponses.has(msg)) aFfRequestTrace("server.response_queued", msg.id);""",
        )
    elif name == DRIVER:
        start = source.index("GeckoDriver.prototype.executeScript = function (cmd) {")
        end = source.index("\n};", start) + len("\n};")
        method = source[start:end]
        method = replace_once(
            method,
            "  let { script, args } = cmd.parameters;",
            """  aFfRequestTrace("driver.enter", aFfEnabled ? cmd.id : null);
  let { script, args } = cmd.parameters;""",
        )
        method = replace_once(
            method,
            "  return this.execute_(script, args, opts);",
            """  if (aFfEnabled) opts.asterinasDiagnosticRequestId = cmd.id;
  return this.execute_(script, args, opts);""",
        )
        source = source[:start] + method + source[end:]
        start = source.index("GeckoDriver.prototype.execute_ = async function (")
        end = source.index("\n};", start) + len("\n};")
        method = source[start:end]
        method = replace_once(
            method,
            "    async = false,",
            "    async = false,\n    asterinasDiagnosticRequestId = null,",
        )
        method = replace_once(
            method,
            "  await this._handleUserPrompts();",
            """  aFfRequestTrace("driver.prompt_enter", asterinasDiagnosticRequestId, () => this.getBrowsingContext());
  await this._handleUserPrompts();
  aFfRequestTrace("driver.prompt_complete", asterinasDiagnosticRequestId, () => this.getBrowsingContext());""",
        )
        method = replace_once(
            method,
            "  return this.getActor().executeScript(script, args, opts);",
            """  if (aFfEnabled) opts.asterinasDiagnosticRequestId = asterinasDiagnosticRequestId;
  aFfRequestTrace("driver.actor_enter", asterinasDiagnosticRequestId, () => this.getBrowsingContext());
  return this.getActor().executeScript(script, args, opts);""",
        )
        source = source[:start] + method + source[end:]
    elif name == PARENT:
        source = replace_once(
            source,
            "  async sendQuery(name, serializedValue) {",
            """  async sendQuery(name, serializedValue) {
    const aFfRequest = aFfEnabled && name === "MarionetteCommandsParent:executeScript"
      ? serializedValue?.opts?.asterinasDiagnosticRequestId : null;
    aFfRequestTrace("parent.query_enter", aFfRequest, () => this.manager.browsingContext);""",
        )
        source = replace_once(
            source,
            "    let {\n      error,",
            """    const aFfQuery = super.sendQuery(name, serializedValue);
    aFfRequestTrace("parent.query_sent", aFfRequest, () => this.manager.browsingContext);
    let {
      error,""",
        )
        source = replace_once(
            source, "      super.sendQuery(name, serializedValue),", "      aFfQuery,"
        )
        source = replace_once(
            source,
            "    if (error) {",
            """    aFfRequestTrace("parent.query_complete", aFfRequest, () => this.manager.browsingContext);
    if (error) {""",
        )
        source = replace_once(
            source,
            "    return serializedResult;",
            """    aFfRequestTrace("parent.reply_ready", aFfRequest, () => this.manager.browsingContext);
    return serializedResult;""",
        )
        source = replace_once(
            source,
            "              const actor =",
            """              if (methodName === "executeScript") {
                aFfRequestTrace("parent.actor_select", args[2]?.asterinasDiagnosticRequestId, () => browsingContext);
              }
              const actor =""",
        )
    else:
        source = replace_once(
            source,
            "  actorCreated() {",
            """  actorCreated() {
    aFfTrace("child.actor_created", 0, () => this.browsingContext);""",
        )
        source = replace_once(
            source,
            "  async receiveMessage(msg) {",
            """  async receiveMessage(msg) {
    const aFfRequest = aFfEnabled && msg.name === "MarionetteCommandsParent:executeScript"
      ? msg.data?.opts?.asterinasDiagnosticRequestId : null;
    aFfRequestTrace("child.receive", aFfRequest, () => this.browsingContext);""",
        )
        source = replace_once(
            source,
            "          result = await this.executeScript(data);",
            """          aFfRequestTrace("child.script_enter", aFfRequest, () => this.browsingContext);
          result = await this.executeScript(data);
          aFfRequestTrace("child.script_complete", aFfRequest, () => this.browsingContext);""",
        )
        source = replace_once(
            source,
            "        await new Promise(resolve => lazy.executeSoon(resolve));",
            """        aFfRequestTrace("child.tick_enter", aFfRequest, () => this.browsingContext);
        await new Promise(resolve => lazy.executeSoon(resolve));
        aFfRequestTrace("child.tick_complete", aFfRequest, () => this.browsingContext);""",
        )
        source = replace_once(
            source,
            "      // Because in WebDriver classic nodes",
            """      aFfRequestTrace("child.reply_ready", aFfRequest, () => this.browsingContext);
      // Because in WebDriver classic nodes""",
        )
        source = replace_once(
            source,
            "      if (lazy.error.isWebDriverError(e)) {",
            """      aFfRequestTrace("child.error", aFfRequest, () => this.browsingContext);
      if (lazy.error.isWebDriverError(e)) {""",
        )
    return source


def transform_archive(source, destination, *, unbuffered_socket=False):
    """Write a new deterministic ZIP only after validating the frozen input."""
    raw = Path(source).read_bytes()
    if hashlib.sha256(raw).hexdigest() != SOURCE_SHA256:
        raise ValueError("source archive hash does not match Firefox ESR 140.15")
    helper = trace_helper()
    transport_helper = transport_trace_helper()
    output = io.BytesIO()
    changes = {}
    with (
        zipfile.ZipFile(io.BytesIO(raw)) as archive,
        zipfile.ZipFile(output, "w") as derived,
    ):
        if len(set(archive.namelist())) != len(archive.namelist()):
            raise ValueError("duplicate archive member")
        if not set(MODULES).issubset(archive.namelist()):
            raise ValueError("missing diagnostic module")
        derived.comment = archive.comment
        for entry in archive.infolist():
            contents = archive.read(entry)
            if entry.filename in MODULES:
                changed = patch_module(
                    entry.filename,
                    contents.decode("utf-8"),
                    helper=helper,
                    transport_helper=transport_helper,
                    unbuffered_socket=unbuffered_socket,
                ).encode("utf-8")
                changes[entry.filename] = {
                    "source_sha256": hashlib.sha256(contents).hexdigest(),
                    "derived_sha256": hashlib.sha256(changed).hexdigest(),
                }
                contents = changed
            derived.writestr(entry, contents)
    result = output.getvalue()
    with Path(destination).open("xb") as stream:
        stream.write(result)
    return {
        "schema_version": 1,
        "experimental": True,
        "browser_acceptance": False,
        "source_sha256": SOURCE_SHA256,
        "derived_sha256": hashlib.sha256(result).hexdigest(),
        "modules": changes,
        "helper_sha256": hashlib.sha256(helper.encode()).hexdigest(),
        "transport_helper_sha256": hashlib.sha256(
            transport_helper.encode()
        ).hexdigest(),
        "transformer_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "enable_environment": "ASTERINAS_FIREFOX_ACTOR_DIAGNOSTICS=1",
        "record_limit_per_module_per_process": 128,
        "socket_stream_mode": "unbuffered" if unbuffered_socket else "buffered",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--unbuffered-socket", action="store_true")
    args = parser.parse_args()
    manifest = transform_archive(
        args.source, args.output, unbuffered_socket=args.unbuffered_socket
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
