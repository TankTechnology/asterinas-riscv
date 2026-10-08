import base64
import hashlib
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, '/usr/lib/asterinas')
from browser_m5_marionette_gate import Marionette


BASE = 'http://10.100.19.216:17895/canvas-perf.html'
SCRIPT = '''return JSON.stringify((() => {
  const status = document.querySelector('#status');
  return {phase: status && status.dataset.phase,
    text: status && status.textContent,
    visibility: document.visibilityState, url: location.href,
    readyState: document.readyState};
})());'''


def process_ticks(pid):
    stat = Path(f'/proc/{pid}/stat').read_text()
    fields = stat[stat.rindex(')') + 2:].split()
    return int(fields[11]) + int(fields[12])


def thread_ticks(pid):
    values = {}
    for task in Path(f'/proc/{pid}/task').iterdir():
        try:
            stat = (task / 'stat').read_text()
            name = stat[stat.index('(') + 1:stat.rindex(')')]
            fields = stat[stat.rindex(')') + 2:].split()
            values[task.name] = (name, int(fields[11]) + int(fields[12]))
        except (OSError, ValueError, IndexError):
            continue
    return values


def hot_threads(before, after):
    changes = []
    for tid, (name, value) in after.items():
        if tid in before:
            changes.append({'tid': tid, 'name': name,
                            'ticks': value - before[tid][1]})
    return sorted(changes, key=lambda item: item['ticks'], reverse=True)[:6]


browser_pid = int(os.environ['BROWSER_PID'])
xorg_pid = int(os.environ['XORG_PID'])
boot_id = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
client = Marionette('127.0.0.1', 2828, 20)
created = False
try:
    client.set_timeout(20)
    session = client.command('WebDriver:NewSession', {'strictFileInteractability': True})
    assert isinstance(session, dict) and isinstance(session.get('sessionId'), str)
    created = True
    for run, (width, height) in enumerate(((640, 360), (1280, 720),
                                           (1280, 720), (640, 360)), 1):
        before_process = {'firefox': process_ticks(browser_pid),
                          'xorg': process_ticks(xorg_pid)}
        before_threads = thread_ticks(browser_pid)
        started = time.monotonic()
        url = f'{BASE}?w={width}&h={height}&run={run}'
        client.set_timeout(25)
        client.command('WebDriver:Navigate', {'url': url})
        navigated = time.monotonic()
        deadline = navigated + 13
        value = None
        while time.monotonic() < deadline:
            client.set_timeout(8)
            response = client.command('WebDriver:ExecuteScript', {
                'script': SCRIPT, 'args': [], 'newSandbox': True,
                'sandbox': 'asterinas-canvas-perf', 'line': 1,
                'filename': 'asterinas-canvas-perf'})
            if isinstance(response, dict) and 'value' in response:
                response = response['value']
            value = json.loads(response)
            if value.get('phase') == 'done':
                value['result'] = json.loads(value['text'])
                break
            if value.get('phase') == 'error':
                break
            time.sleep(0.25)
        after_process = {'firefox': process_ticks(browser_pid),
                         'xorg': process_ticks(xorg_pid)}
        after_threads = thread_ticks(browser_pid)
        record = {
            'run': run, 'boot_id': boot_id, 'url': url,
            'navigate_seconds': round(navigated - started, 3),
            'elapsed_seconds': round(time.monotonic() - navigated, 3),
            'page': value,
            'firefox_ticks': after_process['firefox'] - before_process['firefox'],
            'xorg_ticks': after_process['xorg'] - before_process['xorg'],
            'hot_threads': hot_threads(before_threads, after_threads),
        }
        payload = json.dumps(record, separators=(',', ':')).encode()
        encoded = base64.b64encode(payload).decode()
        parts = [encoded[start:start + 80] for start in range(0, len(encoded), 80)]
        for index, part in enumerate(parts):
            print(f'CANVAS_CHUNK run={run} i={index} data={part}', flush=True)
        print(f'CANVAS_END run={run} parts={len(parts)} '
              f'sha256={hashlib.sha256(payload).hexdigest()}', flush=True)
        assert value is not None and value.get('result') is not None, value
        assert value['result']['width'] == width and value['result']['height'] == height
        assert value['result']['visibility'] == 'visible'
finally:
    if created:
        try:
            client.set_timeout(10)
            client.command('WebDriver:DeleteSession')
        except Exception as error:
            print('DELETE_SESSION_ERROR ' + repr(error), flush=True)
    client.close()
