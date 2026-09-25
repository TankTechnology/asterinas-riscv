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
    payload = base64.b64encode(gzip.compress((root / 'physical-rgb-guest.py').read_bytes(),
                                           mtime=0)).decode()
    command('create-transfer', "printf '' > /run/asterinas-rgb.b64")
    for index, start in enumerate(range(0, len(payload), 900), 1):
        command(f'transfer-{index}',
                "printf '%s' '" + payload[start:start + 900] +
                "' >> /run/asterinas-rgb.b64")
    command('extract-guest',
            'base64 -d /run/asterinas-rgb.b64 | gzip -d > /run/asterinas-rgb.py')
    output = command(
        'canvas-probe',
        '_pid=$(pgrep -xo firefox); _xpid=$(pgrep -xo Xorg); '
        'nsenter -t "$_pid" -n env BROWSER_PID="$_pid" XORG_PID="$_xpid" '
        'timeout 100 python3 -u /run/asterinas-rgb.py', timeout=115,
    )
    (root / 'physical-rgb-guest-output.txt').write_text(output)
    chunks = {}
    endings = {}
    for line in output.splitlines():
        match = re.fullmatch(r'RGB_CHUNK run=(\d+) i=(\d+) data=(\S+)', line)
        if match:
            chunks.setdefault(int(match.group(1)), {})[int(match.group(2))] = match.group(3)
        match = re.fullmatch(r'RGB_END run=(\d+) parts=(\d+) sha256=([0-9a-f]{64})', line)
        if match:
            endings[int(match.group(1))] = (int(match.group(2)), match.group(3))
    samples = []
    for run in range(1, 5):
        if run not in endings or run not in chunks:
            raise RuntimeError(f'RGB run {run} is missing serial chunks')
        count, digest = endings[run]
        if set(chunks[run]) != set(range(count)):
            raise RuntimeError(f'RGB run {run} has missing serial chunks')
        payload = base64.b64decode(''.join(chunks[run][i] for i in range(count)))
        if hashlib.sha256(payload).hexdigest() != digest:
            raise RuntimeError(f'RGB run {run} serial checksum mismatch')
        sample = json.loads(payload)
        if sample['run'] != run:
            raise RuntimeError(f'RGB run {run} label mismatch')
        samples.append(sample)
    (root / 'physical-rgb-result.json').write_text(json.dumps({
        'boot_id': expected_boot, 'image_sha256':
        '3e3707b7e59f46303a914395a8dd4a85de55d0feb063fa26bb510130ce02b40e',
        'samples': samples,
    }, indent=2))
    for sample in samples:
        page = sample['page']['result']
        print(f"run={sample['run']} size={page['width']}x{page['height']} "
              f"frames={page['frames']} p95ms={page['p95Ms']:.1f} "
              f"over33={page['over33Ms']} firefox_ticks={sample['firefox_ticks']} "
              f"xorg_ticks={sample['xorg_ticks']}")
finally:
    (root / 'physical-rgb-host-commands.json').write_text(json.dumps(records, indent=2))
    if serial is not None:
        (root / 'physical-rgb-serial.log.gz').write_bytes(
            gzip.compress(serial.transcript, mtime=0)
        )
    os.close(fd)
