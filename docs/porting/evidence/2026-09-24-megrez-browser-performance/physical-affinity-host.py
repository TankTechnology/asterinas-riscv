import base64
import gzip
import hashlib
import json
import os
import re
import secrets
import time
from pathlib import Path

from tools.riscv.debian.rootfs.gate_runtime import SerialConsole
from tools.riscv.megrez_board_session import open_serial, read_available
from tools.riscv.megrez_debug_board import _lock_serial


root = Path('/home/ubuntu/asterinas-board-media-20260924')
device = '/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AL02XYO2-if00-port0'
expected_boot = 'c6846443-9d90-4ff5-854e-5c4132fcfbbc'
records = []
fd = open_serial(device)
serial = None
try:
    _lock_serial(fd)
    serial = SerialConsole(fd, max_bytes=512 * 1024, tx_delay=0.005)

    def command(label, body, timeout=20):
        nonce = secrets.token_hex(8)
        wrapped = (
            body + f"; _s=$?; printf '\\nEND_%s status=%s uid=%s boot=%s\\n' {nonce} "
            '"$_s" "$(id -u)" "$(cat /proc/sys/kernel/random/boot_id)"'
        )
        start = serial.checkpoint()
        serial.send(wrapped.encode() + b'\n', time.monotonic() + 40)
        serial.wait_for(f'END_{nonce} status='.encode(), time.monotonic() + timeout,
                        start=start)
        output = (serial.transcript[start:].decode(errors='replace') +
                  read_available(fd, 0.5)).replace('\r', '')
        match = re.search(rf'END_{nonce} status=(\d+) uid=(\d+) boot=([0-9a-f-]+)', output)
        status = int(match.group(1)) if match else None
        boot = match.group(3) if match else None
        records.append({'label': label, 'nonce': nonce, 'status': status,
                        'boot_id': boot})
        if status != 0 or match.group(2) != '0' or boot != expected_boot:
            raise RuntimeError(f'{label} failed: {output[-1000:]}')
        return output

    command('preflight', 'id -u; cat /proc/sys/kernel/random/boot_id')
    payload = base64.b64encode(gzip.compress((root / 'physical-affinity-guest.py').read_bytes(),
                                           mtime=0)).decode()
    command('create-transfer', "printf '' > /run/asterinas-affinity.b64")
    for index, start in enumerate(range(0, len(payload), 900), 1):
        command(f'transfer-{index}',
                "printf '%s' '" + payload[start:start + 900] +
                "' >> /run/asterinas-affinity.b64")
    command('extract-guest',
            'base64 -d /run/asterinas-affinity.b64 | gzip -d > /run/asterinas-affinity.py')
    output = command(
        'affinity-probe',
        '_pid=$(pgrep -xo firefox); _xpid=$(pgrep -xo Xorg); '
        'nsenter -t "$_pid" -n env BROWSER_PID="$_pid" XORG_PID="$_xpid" '
        'timeout 85 python3 -u /run/asterinas-affinity.py', timeout=95,
    )
    (root / 'physical-affinity-guest-output.txt').write_text(output)
    if 'RESTORED_AFFINITY' not in output:
        raise RuntimeError('guest did not confirm affinity restoration')
    chunks = {}
    endings = {}
    for line in output.splitlines():
        match = re.fullmatch(r'AFFINITY_CHUNK run=(\d+) i=(\d+) data=(\S+)', line)
        if match:
            chunks.setdefault(int(match.group(1)), {})[int(match.group(2))] = match.group(3)
        match = re.fullmatch(r'AFFINITY_END run=(\d+) parts=(\d+) sha256=([0-9a-f]{64})', line)
        if match:
            endings[int(match.group(1))] = (int(match.group(2)), match.group(3))
    samples = []
    for run in range(1, 4):
        if run not in endings or run not in chunks:
            raise RuntimeError(f'media-path run {run} is missing serial chunks')
        count, digest = endings[run]
        if set(chunks[run]) != set(range(count)):
            raise RuntimeError(f'media-path run {run} has missing serial chunks')
        payload = base64.b64decode(''.join(chunks[run][i] for i in range(count)))
        if hashlib.sha256(payload).hexdigest() != digest:
            raise RuntimeError(f'media-path run {run} serial checksum mismatch')
        sample = json.loads(payload)
        if sample['run'] != run:
            raise RuntimeError(f'media-path run {run} label mismatch')
        samples.append(sample)
    (root / 'physical-affinity-result.json').write_text(json.dumps({
        'boot_id': expected_boot, 'image_sha256':
        '3e3707b7e59f46303a914395a8dd4a85de55d0feb063fa26bb510130ce02b40e',
        'samples': samples,
    }, indent=2))
    for sample in samples:
        page = sample['page']['result']
        print(f"run={sample['run']} variant={sample['variant']} size={page['width']}x{page['height']} "
              f"source={page['source']} frames={page['totalFrames']} "
              f"dropped={page['droppedFrames']} callbacks={page['callbackCount']} "
              f"elapsed={sample['elapsed_seconds']}")
        print('thread_deltas', sample['thread_deltas'], 'affinity', sample['affinity'])
finally:
    if serial is not None:
        try:
            nonce = secrets.token_hex(8)
            cleanup = (
                "python3 -c 'import os,subprocess; "
                "p=int(subprocess.check_output([\"pgrep\",\"-xo\",\"firefox\"])); "
                "[(os.sched_setaffinity(int(t),{0,1,2,3}),print(t,\"restored\")) "
                "for t in os.listdir(f\"/proc/{p}/task\") "
                "if open(f\"/proc/{p}/task/{t}/comm\").read().strip() "
                "in (\"Renderer\",\"SwComposite\")]'; "
                + f"printf '\\nCLEANUP_%s uid=%s boot=%s\\n' {nonce} "
                '"$(id -u)" "$(cat /proc/sys/kernel/random/boot_id)"'
            )
            serial.send(cleanup.encode() + b'\n', time.monotonic() + 20)
            serial.wait_for(f'CLEANUP_{nonce}'.encode(), time.monotonic() + 15)
            output = read_available(fd, 0.5)
            records.append({'label': 'host-affinity-cleanup', 'nonce': nonce,
                            'output': output[-1000:]})
        except Exception as error:
            records.append({'label': 'host-affinity-cleanup', 'error': repr(error)})
        (root / 'physical-affinity-serial.log.gz').write_bytes(
            gzip.compress(serial.transcript, mtime=0)
        )
    (root / 'physical-affinity-host-commands.json').write_text(json.dumps(records, indent=2))
    os.close(fd)
