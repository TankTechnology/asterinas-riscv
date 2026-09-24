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

HERE=Path('/root/asterinas/target/lmbench-basic-20260923')
mode,output=sys.argv[1:]
plan=json.loads(Path('/root/asterinas/target/qemu-stability-20260922/browser-release-plan.json').read_text())
a={x['name']:Path(x['path']) for x in plan['artifacts']}
if os.environ.get('ASTERINAS_TEST_KERNEL'): a['kernel']=Path(os.environ['ASTERINAS_TEST_KERNEL'])
config=GateConfig(kernel=a['kernel'],u_boot=a['u_boot'],dtb=a['qemu_dtb'],stage1_initramfs=a['initramfs'],root_image=a['root_image'],manifest=a['root_manifest'],packages_lock=a['packages_lock'],package_checksums=a['package_checksums'],output_directory=HERE/output,boot_timeout=300,command_timeout=60)
physical.physical_browser_start_command=lambda:'/run/asterinas-tools/g start-web'
class Ops(PhysicalGraphicsQemuOperations):
    def prepare(self,config,snapshots,identity):
        prepared=super().prepare(config,snapshots,identity)
        bundle=HERE/'lmbench-runtime.tar.gz'
        result=subprocess.run(['debugfs','-w','-R','write '+str(bundle)+' /opt/lmbench-runtime.tar.gz',str(prepared['root_disk'])],capture_output=True,text=True,timeout=30)
        (config.output_directory/'overlay.log').write_text(result.stdout+result.stderr)
        result.check_returncode()
        guest=(HERE/'guest.py').read_text().replace('__BUNDLE_META__',repr(json.loads((HERE/'bundle.json').read_text()))).replace('__RUNNER_SOURCE__',repr(Path('tools/riscv/lmbench_smoke.py').read_text()))
        guest_source=config.output_directory/'staged-guest.py'
        guest_source.write_text(guest)
        result=subprocess.run(['debugfs','-w','-R','write '+str(guest_source)+' /opt/lifecycle.py',str(prepared['root_disk'])],capture_output=True,text=True,timeout=30)
        (config.output_directory/'guest-overlay.log').write_text(result.stdout+result.stderr)
        result.check_returncode()
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
        serial.wait_for(b'ASTERINAS_DEBUG_CONSOLE_READY uid=0',time.monotonic()+config.boot_timeout)
        self.shell(serial,'test "$(id -u)" = 0')
        if mode=='media':
            self._start_browser(serial,time.monotonic()+180)
            self._browser_pid=self._query_browser_pid(serial,time.monotonic()+60)
            self._wait_for_marionette(serial,time.monotonic()+180)
        guest=(HERE/'guest.py').read_text().replace('__BUNDLE_META__',repr(json.loads((HERE/'bundle.json').read_text()))).replace('__RUNNER_SOURCE__',repr(Path('tools/riscv/lmbench_smoke.py').read_text()))
        if mode=='media':
            from tools.riscv.debian.rootfs.browser_web_marionette_gate import _playback_probe
            guest=guest.replace('__PROBE_FUNCTION__',repr(inspect.getsource(_playback_probe))).replace('__VIDEO__',repr(Path('tools/riscv/debian/rootfs/browser_m5.webm.base64').read_text()))
        (config.output_directory/'delivered-guest.py').write_text(guest)
        digest=__import__('hashlib').sha256(guest.encode()).hexdigest()
        self.shell(serial,"test $(sha256sum /opt/lifecycle.py | cut -d' ' -f1) = "+digest)
        self.shell(serial,"sha256sum /usr/lib/python3.13/__pycache__/threading.cpython-313.pyc")
        self.shell(serial,"cp /opt/lifecycle.py /run/lifecycle.py")
        start=serial.checkpoint();deadline=time.monotonic()+120
        self.shell(serial,"(python3 -u /run/lifecycle.py; printf 'LIFECYCLE_EXIT status=%s\\n' \"$?\") > /run/lifecycle.log 2>&1 & wait_noop=true")
        captured=False
        while time.monotonic()<deadline:
            self.shell(serial,'cat /run/lifecycle.log')
            transcript=serial.transcript[start:]
            if b'"kind": "awaiting-cpu-capture"' in transcript and not captured:
                self.cpu_capture(session,'stopped-desktop')
                self.shell(serial,'touch /run/lifecycle-captured');captured=True
            status=re.search(rb'^LIFECYCLE_EXIT status=(\d+)',transcript,re.M)
            if status:
                if status.group(1)!=b'0': raise GateFailure('guest experiment failed')
                self.shell(serial,'true');return
            time.sleep(.5)
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
