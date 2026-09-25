import base64
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time

sys.path.insert(0, '/usr/lib/asterinas')
from browser_m5_marionette_gate import Marionette


URL = 'http://10.100.19.216:17895/input-perf.html'
READ = '''return JSON.stringify((() => {
  const input = document.querySelector('#input');
  const results = document.querySelector('#results');
  return {url: location.href, readyState: document.readyState,
    visibility: document.visibilityState, value: input && input.value,
    count: results && Number(results.dataset.count),
    samples: results && results.textContent};
})());'''


def execute(client, script):
    result = client.command('WebDriver:ExecuteScript', {
        'script': script, 'args': [], 'newSandbox': True,
        'sandbox': 'asterinas-input-perf', 'line': 1,
        'filename': 'asterinas-input-perf'})
    if isinstance(result, dict) and 'value' in result:
        result = result['value']
    return result


env = os.environ.copy()
env['DISPLAY'] = ':0'
browser_pid = int(os.environ['BROWSER_PID'])
boot_id = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
client = Marionette('127.0.0.1', 2828, 20)
created = False
try:
    client.set_timeout(20)
    session = client.command('WebDriver:NewSession', {'strictFileInteractability': True})
    assert isinstance(session, dict) and isinstance(session.get('sessionId'), str)
    created = True
    client.set_timeout(25)
    client.command('WebDriver:Navigate', {'url': URL})
    windows = subprocess.check_output(
        ['xdotool', 'search', '--onlyvisible', '--class', 'firefox'],
        env=env, timeout=8, text=True).splitlines()
    assert windows
    window = windows[-1]
    subprocess.run(['xdotool', 'windowactivate', '--sync', window], env=env,
                   check=True, timeout=8)
    execute(client, "document.querySelector('#input').focus(); return 'focused';")
    subprocess.run(['xdotool', 'type', '--clearmodifiers', '--delay', '75',
                    '--', 'abcdefghijklmnop'], env=env, check=True, timeout=10)
    deadline = time.monotonic() + 8
    value = None
    while time.monotonic() < deadline:
        client.set_timeout(8)
        value = json.loads(execute(client, READ))
        if value.get('count') == 16:
            break
        time.sleep(0.2)
    assert value is not None and value.get('count') == 16, value
    assert value['value'] == 'abcdefghijklmnop', value
    assert value['visibility'] == 'visible', value
    samples = json.loads(value['samples'])
    assert len(samples) == 16
    ordered = sorted(samples)
    result = {'boot_id': boot_id, 'browser_pid': browser_pid, 'window': window,
              'url': value['url'], 'samples_ms': samples,
              'p50_ms': statistics.median(samples),
              'p95_ms': ordered[15], 'max_ms': ordered[-1],
              'trusted_input_events': 16}
    payload = json.dumps(result, separators=(',', ':')).encode()
    encoded = base64.b64encode(payload).decode()
    parts = [encoded[start:start + 80] for start in range(0, len(encoded), 80)]
    for index, part in enumerate(parts):
        print(f'INPUT_CHUNK i={index} data={part}', flush=True)
    print(f'INPUT_END parts={len(parts)} sha256={hashlib.sha256(payload).hexdigest()}',
          flush=True)
finally:
    if created:
        try:
            client.set_timeout(10)
            client.command('WebDriver:DeleteSession')
        except Exception as error:
            print('DELETE_SESSION_ERROR ' + repr(error), flush=True)
    client.close()
