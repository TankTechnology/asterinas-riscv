# SPDX-License-Identifier: MPL-2.0
"""Offline archive checks and isolated tests of the instrumented Gecko code."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from tools.riscv.diagnostics import firefox_actor_overlay as overlay


ROOT = Path(__file__).parents[3]
ARCHIVE = (
    ROOT / "target/current-main-physical-graphics/inputs/firefox-esr-140.15-omni.ja"
)
NODE = os.environ.get("ASTERINAS_TEST_NODE", "node")
HAS_NODE = shutil.which(NODE) is not None


def javascript(source):
    result = subprocess.run(
        [NODE, "--input-type=module"],
        input=source,
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    return json.loads(result.stdout)


class ActorTraceTests(unittest.TestCase):
    def helper(self, enabled, body):
        return javascript(
            "const lines=[]; const Services={env:{get:()=>"
            + json.dumps(enabled)
            + "},appinfo:{processID:67}}; let dump=s=>lines.push(s);\n"
            + overlay.trace_helper()
            + "\n"
            + body
            + "\nconsole.log(JSON.stringify(lines.map(x=>JSON.parse(x.slice(11)))));"
        )

    @unittest.skipUnless(HAS_NODE, "requires an explicitly available cached Node")
    def test_default_off_and_exact_opt_in(self):
        for value in ("", "0", "true", "01"):
            self.assertEqual(
                self.helper(value, 'aFfTrace("test",4,()=>{throw 1});'), []
            )
        records = self.helper(
            "1", 'aFfTrace("test",4,()=>({id:9,currentWindowGlobal:{osPid:291}}));'
        )
        self.assertEqual(
            records,
            [
                dict(
                    version=1,
                    stage="test",
                    pid=67,
                    request=4,
                    context=9,
                    target_pid=291,
                )
            ],
        )

    @unittest.skipUnless(HAS_NODE, "requires an explicitly available cached Node")
    def test_bounded_and_scalar_only(self):
        records = self.helper(
            "1",
            'for(let i=0;i<1000;i++) aFfTrace("test",4,()=>({id:"SECRET",currentWindowGlobal:{osPid:{secret:1}}}));',
        )
        self.assertEqual(len(records), 128)
        self.assertNotIn("SECRET", json.dumps(records))
        self.assertTrue(
            all(r["context"] == 0 and r["target_pid"] == 0 for r in records)
        )

    @unittest.skipUnless(HAS_NODE, "requires an explicitly available cached Node")
    def test_diagnostic_sink_failure_does_not_escape(self):
        self.assertEqual(
            self.helper("1", 'dump=()=>{throw Error("sink")}; aFfTrace("test",4);'), []
        )

    @unittest.skipUnless(HAS_NODE, "requires an explicitly available cached Node")
    def test_zero_is_a_valid_request_id_but_null_is_absent(self):
        records = self.helper(
            "1", 'aFfRequestTrace("test",0);aFfRequestTrace("test",null);'
        )
        self.assertEqual([r["request"] for r in records], [0])

    def test_exact_edit_rejects_missing_or_duplicate_anchor(self):
        for source in ("abc", "needle needle"):
            with self.assertRaisesRegex(ValueError, "anchor"):
                overlay.replace_once(source, "needle", "new")

    def test_wrong_archive_rejected_without_output(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "bad.ja"
            source.write_bytes(b"not the frozen archive")
            output = Path(directory) / "output.ja"
            with self.assertRaisesRegex(ValueError, "hash"):
                overlay.transform_archive(source, output)
            self.assertFalse(output.exists())

    @unittest.skipUnless(HAS_NODE, "requires an explicitly available cached Node")
    def test_transport_trace_is_default_off_bounded_and_scalar_only(self):
        source = (
            "const lines=[]; const Services={env:{get:()=>ENABLED},"
            "appinfo:{processID:67}}; let dump=s=>lines.push(s);\n"
            + overlay.trace_helper()
            + "\n"
            + overlay.transport_trace_helper()
            + "\nfor(let i=0;i<1000;i++) "
            'aFfTransportTrace("process.enter",i,"SECRET",1176,4);\n'
            "console.log(JSON.stringify(lines));"
        )
        disabled = javascript(source.replace("ENABLED", json.dumps("0")))
        self.assertEqual(disabled, [])
        enabled = javascript(source.replace("ENABLED", json.dumps("1")))
        self.assertEqual(len(enabled), 128)
        self.assertNotIn("SECRET", json.dumps(enabled))
        first = json.loads(enabled[0][len("A_FF_TRANSPORT ") :])
        self.assertEqual(
            set(first),
            {
                "version",
                "stage",
                "pid",
                "sequence",
                "available",
                "header",
                "expected",
                "received",
                "request",
            },
        )

    @unittest.skipUnless(ARCHIVE.exists(), "requires the pinned Firefox archive")
    def test_transport_module_marks_callback_parser_and_dispatch_boundaries(self):
        with zipfile.ZipFile(ARCHIVE) as archive:
            source = overlay.patch_module(
                overlay.TRANSPORT, archive.read(overlay.TRANSPORT).decode()
            )
        for marker in (
            'aFfTrace("transport.loaded")',
            'aFfTransportTrace("wait.arm")',
            'aFfTransportTrace("input.ready"',
            'aFfTransportTrace("probe.available"',
            'aFfTransportTrace("process.enter"',
            'aFfTransportTrace("packet.complete"',
            'aFfTransportTrace("json.ready"',
            'aFfTransportTrace("packet.dispatch"',
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, source)
        self.assertIn("Ci.nsITimer.TYPE_REPEATING_SLACK", source)
        self.assertIn("this._aFfProbeTimer.cancel()", source)

    @unittest.skipUnless(ARCHIVE.exists(), "requires the pinned Firefox archive")
    def test_server_unbuffered_variant_is_explicit_and_default_is_unchanged(self):
        with zipfile.ZipFile(ARCHIVE) as archive:
            original = archive.read(overlay.SERVER).decode()
        default = overlay.patch_module(overlay.SERVER, original)
        unbuffered = overlay.patch_module(
            overlay.SERVER, original, unbuffered_socket=True
        )
        self.assertIn("clientSocket.openInputStream(0, 0, 0)", default)
        self.assertIn("clientSocket.openOutputStream(0, 0, 0)", default)
        self.assertIn(
            "clientSocket.openInputStream(Ci.nsITransport.OPEN_UNBUFFERED, 0, 0)"
            ".QueryInterface(Ci.nsIAsyncInputStream)",
            unbuffered,
        )
        self.assertIn(
            "clientSocket.openOutputStream(Ci.nsITransport.OPEN_UNBUFFERED, 0, 0)"
            ".QueryInterface(Ci.nsIAsyncOutputStream)",
            unbuffered,
        )


@unittest.skipUnless(
    ARCHIVE.exists() and HAS_NODE, "requires the pinned Firefox archive and cached Node"
)
class PackagedActorTests(unittest.TestCase):
    def test_transformation_uses_one_helper_snapshot(self):
        original = overlay.trace_helper()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "snapshot.ja"
            with patch.object(
                overlay, "trace_helper", side_effect=[original, "different helper"] * 5
            ) as read:
                manifest = overlay.transform_archive(ARCHIVE, output)
            self.assertEqual(read.call_count, 1)
            self.assertEqual(
                manifest["helper_sha256"], hashlib.sha256(original.encode()).hexdigest()
            )
            with zipfile.ZipFile(output) as archive:
                self.assertTrue(
                    all(original in archive.read(n).decode() for n in overlay.MODULES)
                )

    def module(self, name):
        with zipfile.ZipFile(ARCHIVE) as archive:
            return overlay.patch_module(name, archive.read(name).decode())

    def test_parent_passes_request_and_preserves_response(self):
        result = javascript(
            """
const lines=[];let sent;
const Services={env:{get:()=>"1"},appinfo:{processID:67}};
const dump=s=>lines.push(JSON.parse(s.slice(11)));
const ChromeUtils={defineESModuleGetters(){},defineLazyGetter(){}};
class JSWindowActorParent {
  constructor(){this.manager={browsingContext:{id:9,currentWindowGlobal:{osPid:291}}};}
  sendQuery(name,value){sent={name,value};return Promise.resolve({serializedValue:42});}
}
"""
            + self.module(overlay.PARENT)
            + """
lazy.getSeenNodesForBrowsingContext=()=>new Set();
lazy.json={mapFromNavigableIds:x=>x};
const actor=new MarionetteCommandsParent();actor.actorCreated();
const result=await actor.executeScript("SECRET",[],{asterinasDiagnosticRequestId:4});
console.log(JSON.stringify({result,request:sent.value.opts.asterinasDiagnosticRequestId,lines}));
"""
        )
        self.assertEqual(result["result"], 42)
        self.assertEqual(result["request"], 4)
        self.assertEqual(
            [r["stage"] for r in result["lines"] if r["request"] == 4],
            [
                "parent.query_enter",
                "parent.query_sent",
                "parent.query_complete",
                "parent.reply_ready",
            ],
        )
        self.assertTrue(
            all(r["target_pid"] == 291 for r in result["lines"] if r["request"] == 4)
        )
        self.assertNotIn("SECRET", json.dumps(result))

    def test_driver_passes_id_only_when_enabled(self):
        source = self.module(overlay.DRIVER)
        methods = []
        for anchor in (
            "GeckoDriver.prototype.executeScript = function (cmd) {",
            "GeckoDriver.prototype.execute_ = async function (",
        ):
            start = source.index(anchor)
            methods.append(source[start : source.index("\n};", start) + 3])
        for enabled in ("0", "1"):
            result = javascript(
                """
const lines=[];let sent; const Services={env:{get:()=>ENABLED},appinfo:{processID:67}};
const dump=s=>lines.push(JSON.parse(s.slice(11)));
const lazy={assert:new Proxy({}, {get:()=>()=>{}}),pprint:()=>""};
function GeckoDriver(){}
""".replace("ENABLED", json.dumps(enabled))
                + overlay.trace_helper()
                + "\n".join(methods)
                + """
const driver=new GeckoDriver(); driver.currentSession={timeouts:{script:30000}};
driver.getBrowsingContext=()=>({id:9,currentWindowGlobal:{osPid:291}});
driver._handleUserPrompts=async()=>{};
driver.getActor=()=>({executeScript:(script,args,opts)=>{sent=opts;return 42;}});
const result=await driver.executeScript({id:4,parameters:{script:"SECRET",args:[]}});
console.log(JSON.stringify({result,sent,lines}));
"""
            )
            self.assertEqual(result["result"], 42)
            self.assertEqual(result["sent"]["timeout"], 30000)
            if enabled == "0":
                self.assertNotIn("asterinasDiagnosticRequestId", result["sent"])
                self.assertEqual(result["lines"], [])
            else:
                self.assertEqual(result["sent"]["asterinasDiagnosticRequestId"], 4)
                self.assertEqual(
                    [r["stage"] for r in result["lines"]],
                    [
                        "driver.enter",
                        "driver.prompt_enter",
                        "driver.prompt_complete",
                        "driver.actor_enter",
                    ],
                )

    def test_only_selected_members_change_deterministically(self):
        with tempfile.TemporaryDirectory() as directory:
            a, b = (Path(directory) / name for name in ("a.ja", "b.ja"))
            manifest = overlay.transform_archive(ARCHIVE, a)
            overlay.transform_archive(ARCHIVE, b)
            self.assertEqual(a.read_bytes(), b.read_bytes())
            self.assertEqual(
                manifest["source_sha256"],
                hashlib.sha256(ARCHIVE.read_bytes()).hexdigest(),
            )
            with zipfile.ZipFile(ARCHIVE) as original, zipfile.ZipFile(a) as changed:
                self.assertEqual(original.namelist(), changed.namelist())
                actual = {
                    n
                    for n in original.namelist()
                    if original.read(n) != changed.read(n)
                }
                self.assertEqual(actual, set(overlay.MODULES))
                for name in actual:
                    subprocess.run(
                        [NODE, "--input-type=module", "--check"],
                        input=changed.read(name),
                        check=True,
                        capture_output=True,
                    )
            with self.assertRaises(FileExistsError):
                overlay.transform_archive(ARCHIVE, a)

    def child(self, fail=False, enabled="1"):
        with zipfile.ZipFile(ARCHIVE) as archive:
            source = overlay.patch_module(
                overlay.CHILD, archive.read(overlay.CHILD).decode()
            )
        return javascript(
            """
const lines=[]; const Services={env:{get:()=>ENABLED},appinfo:{processID:291}};
const dump=s=>lines.push(JSON.parse(s.slice(11)));
const ChromeUtils={defineESModuleGetters(){},defineLazyGetter(){},
  domProcessChild:{getActor:()=>({getNodeCache:()=>null})}};
class JSWindowActorChild {
  constructor(){this.browsingContext={id:9};this.contentWindow={browsingContext:this.browsingContext};
    this.document={defaultView:{}};this.manager={innerWindowId:12};}
}
""".replace("ENABLED", json.dumps(enabled))
            + source
            + """
let tick; const originalError={code:123};
lazy.Sandboxes=class {};lazy.sandbox={createMutable:()=>({})};
lazy.evaluate={sandbox:()=>FAIL ? Promise.reject(originalError) : Promise.resolve(42)};
lazy.error={isWebDriverError:()=>false};
lazy.json={deserialize:x=>x,clone:x=>({seenNodeIds:new Map(),serializedValue:x,hasSerializedWindows:false})};
lazy.executeSoon=f=>{tick=f;};
const actor=new MarionetteCommandsChild();
const pending=actor.receiveMessage({name:"MarionetteCommandsParent:executeScript",
  data:{script:"SECRET",args:[],opts:{asterinasDiagnosticRequestId:4}}});
await new Promise(setImmediate);
const before=lines.map(x=>x.stage);
if(tick)tick();
const result=await pending;
console.log(JSON.stringify({before,after:lines,result,sameError:result.error===originalError}));
""".replace("FAIL", str(fail).lower())
        )

    def test_child_waits_for_next_tick_before_serialized_reply(self):
        result = self.child()
        self.assertIn("child.script_complete", result["before"])
        self.assertNotIn("child.tick_complete", result["before"])
        self.assertEqual(
            [r["stage"] for r in result["after"] if r["request"] == 4],
            [
                "child.receive",
                "child.script_enter",
                "child.script_complete",
                "child.tick_enter",
                "child.tick_complete",
                "child.reply_ready",
            ],
        )
        self.assertEqual(result["result"]["serializedValue"], 42)
        self.assertNotIn("SECRET", json.dumps(result))

    def test_original_child_error_is_preserved(self):
        result = self.child(fail=True)
        self.assertTrue(result["sameError"])
        self.assertEqual(result["after"][-1]["stage"], "child.error")
        self.assertFalse(result["result"]["isWebDriverError"])

    def test_disabled_child_preserves_result_without_logging(self):
        result = self.child(enabled="0")
        self.assertEqual(result["after"], [])
        self.assertEqual(result["result"]["serializedValue"], 42)


if __name__ == "__main__":
    unittest.main()
