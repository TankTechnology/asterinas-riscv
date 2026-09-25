# SPDX-License-Identifier: MPL-2.0

import sys,time
from tools.riscv.debian.rootfs.debug_console_qemu_gate import DebugConsoleQemuOperations,orchestrate_debug_console_qemu_gate
from tools.riscv.debian.rootfs.rootfs_gate import parse_gate_args
from tools.riscv.debian.rootfs.rootfs_gate_backend import _safe_output
from tools.riscv.debian.rootfs.gate_runtime import TerminationSignalState

class Ops(DebugConsoleQemuOperations):
 def _run_debug_console_probe(self,session,config):
  super()._run_debug_console_probe(session,config)
  serial=session['serial'];start=serial.checkpoint()
  command=(
   "{ printf 'boot='; cat /proc/sys/kernel/random/boot_id; "
   "printf '\\n-- failed units --\\n'; timeout 5 systemctl --failed --no-pager; "
   "printf '\\n-- browser --\\n'; timeout 5 systemctl status asterinas-browser-web.service --no-pager -l; "
   "printf '\\n-- file owner --\\n'; stat -c '%u:%g %a %n' /home/asterinas/browser-web-timeline.log; "
   "printf '\\n-- proc sysctl --\\n'; cat /proc/sys/kernel/pid_max; "
   "printf '\\n-- valid limit --\\n'; echo 32768 > /proc/sys/kernel/pid_max; echo write_exit=$?; cat /proc/sys/kernel/pid_max; "
   "printf '\\n-- invalid limit --\\n'; echo 300 > /proc/sys/kernel/pid_max; echo write_exit=$?; cat /proc/sys/kernel/pid_max; "
   "printf '\\n-- restore limit --\\n'; echo 4194304 > /proc/sys/kernel/pid_max; echo write_exit=$?; cat /proc/sys/kernel/pid_max; "
   "} > /home/asterinas/wave4-state.txt 2>&1; "
   "timeout 20 journalctl -b --no-pager -o short-monotonic > /home/asterinas/wave4-journal.log 2>&1; "
   "echo $? > /home/asterinas/wave4-journal.exit; "
   "timeout 10 dmesg > /home/asterinas/wave4-dmesg.log 2>&1; "
   "echo $? > /home/asterinas/wave4-dmesg.exit; "
   "sync; ls -l /home/asterinas/wave4-*; "
   "printf '__WAVE4_''DONE__\\n'\n"
  )
  serial.send(command.encode(),time.monotonic()+45)
  serial.wait_for(b'__WAVE4_DONE__',time.monotonic()+45,start=start)
  self._require_output().atomic_write('wave4-probe.serial.log',serial.transcript[start:])

config=parse_gate_args(sys.argv[1:])
_safe_output(config.output_directory)
with TerminationSignalState(), Ops(config) as operations:
 result=orchestrate_debug_console_qemu_gate(config,operations)
print('WAVE4_RESULT',result.get('passed'),result.get('reason'),flush=True)
raise SystemExit(0 if result['passed'] else 1)
