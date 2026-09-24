# SPDX-License-Identifier: MPL-2.0

import base64, gzip, inspect, json, os, re, secrets, subprocess, sys, time
from pathlib import Path
import tools.riscv.physical_graphics_qemu_gate as physical
from tools.riscv.physical_graphics_qemu_gate import PhysicalGraphicsQemuOperations, _next_line
from tools.riscv.debian.rootfs.desktop_m5_qemu_gate import DesktopM5QemuOperations
from tools.riscv.debian.rootfs.debug_console_qemu_gate import DebugConsoleQemuOperations
from tools.riscv.debian.rootfs.rootfs_gate import GateConfig, GateFailure
from tools.riscv.debian.rootfs.systemd_m2_gate import orchestrate_systemd_m2_gate
from tools.riscv.debian.rootfs.gate_protocol import GateResult

HERE=Path('/root/asterinas/target/lmbench-native-20260923')
mode,output=sys.argv[1:]
plan=json.loads(Path('/root/asterinas/target/qemu-stability-20260922/browser-release-plan.json').read_text())
a={x['name']:Path(x['path']) for x in plan['artifacts']}
if os.environ.get('ASTERINAS_TEST_KERNEL'): a['kernel']=Path(os.environ['ASTERINAS_TEST_KERNEL'])
config=GateConfig(kernel=a['kernel'],u_boot=a['u_boot'],dtb=a['qemu_dtb'],stage1_initramfs=a['initramfs'],root_image=a['root_image'],manifest=a['root_manifest'],packages_lock=a['packages_lock'],package_checksums=a['package_checksums'],output_directory=HERE/output,boot_timeout=300,command_timeout=60)
physical.physical_browser_start_command=lambda:'/run/asterinas-tools/g start-web'
class Ops(PhysicalGraphicsQemuOperations):
    def prepare(self,config,snapshots,identity):
        prepared=super().prepare(config,snapshots,identity)
        bundle=HERE/'lmbench-runtime-no-egrep.tar.gz'
        result=subprocess.run(['debugfs','-w','-R','write '+str(bundle)+' /opt/lmbench-runtime.tar.gz',str(prepared['root_disk'])],capture_output=True,text=True,timeout=30)
        (config.output_directory/'overlay.log').write_text(result.stdout+result.stderr)
        result.check_returncode()
        test=HERE/'reserved-ports-test'
        if test.exists():
            subprocess.run(['debugfs','-w','-R','write '+str(test)+' /opt/reserved-ports-test',str(prepared['root_disk'])],check=True,capture_output=True,text=True,timeout=10)
        test=HERE/'udp-loopback-test'
        if test.exists():
            subprocess.run(['debugfs','-w','-R','write '+str(test)+' /opt/udp-loopback-test',str(prepared['root_disk'])],check=True,capture_output=True,text=True,timeout=10)
        trace_bundle=HERE/'strace-runtime.tar.gz'
        if trace_bundle.exists():
            subprocess.run(['debugfs','-w','-R','write '+str(trace_bundle)+' /opt/strace-runtime.tar.gz',str(prepared['root_disk'])],check=True,capture_output=True,text=True,timeout=15)
        rpc_diag=HERE/'rpc-diag'
        if rpc_diag.exists():
            subprocess.run(['debugfs','-w','-R','write '+str(rpc_diag)+' /opt/rpc-diag',str(prepared['root_disk'])],check=True,capture_output=True,text=True,timeout=10)
        lat_rpc_timeout=HERE/'lat-rpc-timeout'
        if lat_rpc_timeout.exists():
            subprocess.run(['debugfs','-w','-R','write '+str(lat_rpc_timeout)+' /opt/lat-rpc-timeout',str(prepared['root_disk'])],check=True,capture_output=True,text=True,timeout=10)
        lat_rpc_debug=HERE/'lat-rpc-debug'
        if lat_rpc_debug.exists():
            subprocess.run(['debugfs','-w','-R','write '+str(lat_rpc_debug)+' /opt/lat-rpc-debug',str(prepared['root_disk'])],check=True,capture_output=True,text=True,timeout=10)
        return prepared
    def serial_observer(self,config,boot_number):
        parent=super().serial_observer(config,boot_number)
        def observe(payload):
            with (config.output_directory/'live.serial.log').open('ab') as f: f.write(payload)
            parent(payload)
        return observe
    def shell(self,serial,command,timeout=12):
        nonce=secrets.token_hex(12);cursor=serial.checkpoint();deadline=time.monotonic()+timeout
        serial.send((command+"; s=$?; printf '\\nCONTROL_%s status=%s uid=%s boot=%s\\n' "+nonce+" \"$s\" \"$(id -u)\" \"$(cat /proc/sys/kernel/random/boot_id)\"\n").encode(),deadline)
        while True:
            line,cursor=_next_line(serial,cursor,deadline)
            if line.startswith('CONTROL_'+nonce+' '):
                print(line,flush=True)
                if not re.fullmatch('CONTROL_'+nonce+r' status=0 uid=0 boot=[0-9a-f-]{36}',line): raise GateFailure('control failed: '+line)
                return
    def cpu_capture(self,session,label):
        monitor=session['monitor'];records={};deadline=time.monotonic()+10
        try:
            records['stop']=monitor.command('stop',deadline).decode(errors='replace')
            records['cpus']=monitor.command('info cpus',deadline).decode(errors='replace')
            for cpu in range(4):
                monitor.command('cpu '+str(cpu),deadline)
                records['cpu-'+str(cpu)]=monitor.command('info registers',deadline).decode(errors='replace')
            records['irq']=monitor.command('info irq',deadline).decode(errors='replace')
        finally:
            records['continue']=monitor.command('cont',time.monotonic()+3).decode(errors='replace')
            (config.output_directory/(label+'.cpu.json')).write_text(json.dumps(records,indent=2)+'\n')
        addresses=re.findall(r'\bpc\s+([0-9a-f]{16})', '\n'.join(records.values()))
        if addresses:
            elf=a['kernel'].with_name('kernel.elf')
            p=subprocess.run(['riscv64-linux-gnu-addr2line','-afC','-e',str(elf)]+['0x'+v for v in addresses],capture_output=True,text=True,timeout=5)
            (config.output_directory/(label+'.symbols.txt')).write_text(p.stdout+p.stderr)
        print('CPU_CAPTURE '+label,flush=True)
    def run_protocol(self,session,config):
        try: self.experiment(session,config)
        except BaseException:
            try:
                self.cpu_capture(session,'failure')
            except Exception as capture_error:
                (config.output_directory/'failure.capture-error.txt').write_text(repr(capture_error)+'\n')
            raise
    def experiment(self,session,config):
        DesktopM5QemuOperations.run_protocol(self,session,config)
        serial=session['serial']
        self._quiesce_external_services(serial,time.monotonic()+60)
        self._wait_for_local_graphics(serial,time.monotonic()+180)
        self._run_debug_console_probe(session,config)
        if mode=='media':
            self._start_browser(serial,time.monotonic()+180)
            self._browser_pid=self._query_browser_pid(serial,time.monotonic()+60)
            self._wait_for_marionette(serial,time.monotonic()+180)
        guest=(HERE/'guest.py').read_text().replace('__BUNDLE_META__',repr(json.loads((HERE/'bundle-no-egrep.json').read_text())))
        if mode=='media':
            from tools.riscv.debian.rootfs.browser_web_marionette_gate import _playback_probe
            guest=guest.replace('__PROBE_FUNCTION__',repr(inspect.getsource(_playback_probe))).replace('__VIDEO__',repr(Path('tools/riscv/debian/rootfs/browser_m5.webm.base64').read_text()))
        (config.output_directory/'delivered-guest.py').write_text(guest)
        encoded=base64.b64encode(gzip.compress(guest.encode())).decode()
        self.shell(serial,"printf '' > /run/lifecycle.b64")
        for offset in range(0,len(encoded),900): self.shell(serial,"printf '%s' '"+encoded[offset:offset+900]+"' >> /run/lifecycle.b64")
        self.shell(serial,"base64 -d /run/lifecycle.b64 | gzip -d > /run/lifecycle.py")
        start=serial.checkpoint();deadline=time.monotonic()+960
        self.shell(serial,"(python3 -u /run/lifecycle.py; printf 'LIFECYCLE_EXIT status=%s\\n' \"$?\") > /run/lifecycle.log 2>&1 & wait_noop=true")
        # Keep the development console idle during the signal and local-network benchmarks.
        time.sleep(180)
        captured=False
        while time.monotonic()<deadline:
            self.shell(serial, "python3 -c 'from pathlib import Path; import sys; p=Path(\"/run/lifecycle.offset\"); n=int(p.read_text()) if p.exists() else 0; d=Path(\"/run/lifecycle.log\").read_bytes(); sys.stdout.buffer.write(d[n:]); p.write_text(str(len(d)))'", timeout=30)
            transcript=serial.transcript[start:]
            if b'"kind": "awaiting-cpu-capture"' in transcript and not captured:
                self.cpu_capture(session,'stopped-desktop')
                self.shell(serial,'touch /run/lifecycle-captured');captured=True
            status=re.search(rb'^LIFECYCLE_EXIT status=(\d+)',transcript,re.M)
            if status:
                if status.group(1)!=b'0': raise GateFailure('guest experiment failed')
                self.shell(serial,'true');return
            time.sleep(2)
        raise GateFailure('short experiment deadline expired')
    def classify_transcript(self,transcript,**kwargs):
        passed=bool(re.search(rb'^LIFECYCLE_EXIT status=0\r?\r?$',transcript,re.M))
        return GateResult(passed,'guest experiment incomplete',None)
    def publish(self,config,prepared,transcript,result):
        result['experiment']=mode;result['physical']=False
        DebugConsoleQemuOperations.publish(self,config,prepared,transcript,result)
with Ops(config) as ops:
    result=orchestrate_systemd_m2_gate(config,ops,classifier=ops.classify_transcript)
    print(json.dumps(result,indent=2),flush=True)
    sys.exit(0 if result['passed'] else 1)
