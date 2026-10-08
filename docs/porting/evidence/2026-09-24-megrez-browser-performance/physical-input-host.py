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
fd = open_serial(device)
serial = None
records = []
try:
    _lock_serial(fd)
    serial = SerialConsole(fd, max_bytes=256 * 1024, tx_delay=0.005)

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
            raise RuntimeError(f'{label} failed: {output[-1300:]}')
        return output

    command('preflight', 'id -u; cat /proc/sys/kernel/random/boot_id')
    payload = base64.b64encode(gzip.compress((root / 'physical-input-guest.py').read_bytes(),
                                           mtime=0)).decode()
    command('create-transfer', "printf '' > /run/asterinas-input.b64")
    for index, start in enumerate(range(0, len(payload), 900), 1):
        command(f'transfer-{index}',
                "printf '%s' '" + payload[start:start + 900] +
                "' >> /run/asterinas-input.b64")
    command('extract-guest',
            'base64 -d /run/asterinas-input.b64 | gzip -d > /run/asterinas-input.py')
    output = command(
        'input-probe',
        '_pid=$(pgrep -xo firefox); '
        'nsenter -t "$_pid" -n env BROWSER_PID="$_pid" '
        'timeout 60 python3 -u /run/asterinas-input.py', timeout=75,
    )
    (root / 'physical-input-guest-output.txt').write_text(output)
    chunks = {}
    ending = None
    for line in output.splitlines():
        match = re.fullmatch(r'INPUT_CHUNK i=(\d+) data=(\S+)', line)
        if match:
            chunks[int(match.group(1))] = match.group(2)
        match = re.fullmatch(r'INPUT_END parts=(\d+) sha256=([0-9a-f]{64})', line)
        if match:
            ending = (int(match.group(1)), match.group(2))
    if ending is None or set(chunks) != set(range(ending[0])):
        raise RuntimeError('input result has missing serial chunks')
    data = base64.b64decode(''.join(chunks[i] for i in range(ending[0])))
    if hashlib.sha256(data).hexdigest() != ending[1]:
        raise RuntimeError('input serial checksum mismatch')
    result = json.loads(data)
    if result['boot_id'] != expected_boot:
        raise RuntimeError('input result boot identity mismatch')
    (root / 'physical-input-result.json').write_text(json.dumps(result, indent=2))
    print(json.dumps({key: result[key] for key in
                      ('trusted_input_events', 'p50_ms', 'p95_ms', 'max_ms')}, indent=2))
finally:
    (root / 'physical-input-host-commands.json').write_text(json.dumps(records, indent=2))
    if serial is not None:
        (root / 'physical-input-serial.log.gz').write_bytes(
            gzip.compress(serial.transcript, mtime=0)
        )
    os.close(fd)
