import base64
import gzip
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
    serial = SerialConsole(fd, max_bytes=128 * 1024, tx_delay=0.005)

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
        records.append({'label': label, 'nonce': nonce, 'status':
                        int(match.group(1)) if match else None})
        if not match or match.group(2) != '0' or match.group(3) != expected_boot:
            raise RuntimeError(f'{label} identity failed: {output[-1000:]}')
        return int(match.group(1)), output

    status, _ = command('preflight', 'id -u; cat /proc/sys/kernel/random/boot_id')
    assert status == 0
    payload = base64.b64encode(gzip.compress(
        (root / 'physical-gecko-profiler-probe-guest.py').read_bytes(), mtime=0
    )).decode()
    command('create-transfer', "printf '' > /run/gecko-profiler-probe.b64")
    for index, start in enumerate(range(0, len(payload), 900), 1):
        command(f'transfer-{index}', "printf '%s' '" + payload[start:start + 900] +
                "' >> /run/gecko-profiler-probe.b64")
    status, _ = command('extract-guest',
                        'base64 -d /run/gecko-profiler-probe.b64 | gzip -d > '
                        '/run/gecko-profiler-probe.py')
    assert status == 0
    status, output = command(
        'probe', '_pid=$(pgrep -xo firefox); nsenter -t "$_pid" -n '
        'env ASTERINAS_MARIONETTE_DEBUG_ERRORS=1 '
        'timeout 30 python3 -u /run/gecko-profiler-probe.py', timeout=40,
    )
    (root / 'physical-gecko-profiler-probe-output.txt').write_text(output)
    print('exit', status)
    print(output[-1800:])
finally:
    (root / 'physical-gecko-profiler-probe-commands.json').write_text(
        json.dumps(records, indent=2)
    )
    if serial is not None:
        (root / 'physical-gecko-profiler-probe-serial.log.gz').write_bytes(
            gzip.compress(serial.transcript, mtime=0)
        )
    os.close(fd)
