import base64
import hashlib
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, '/usr/lib/asterinas')
from browser_m5_marionette_gate import Marionette


URL = 'http://10.100.19.216:17895/video-source-perf.html?mode=direct&w=1280&h=720&src=clip720.webm'
SCRIPT = '''return JSON.stringify((() => {
  const s = document.querySelector('#status');
  return {phase: s && s.dataset.phase, text: s && s.textContent,
    visibility: document.visibilityState};
})());'''


def thread(pid, name):
    found = []
    for task in Path(f'/proc/{pid}/task').iterdir():
        try:
            if (task / 'comm').read_text().strip() == name:
                found.append(int(task.name))
        except OSError:
            continue
    if len(found) != 1:
        raise RuntimeError(f'{name}: expected one thread, found {found}')
    return found[0]


def counters(pid, tid):
    path = Path(f'/proc/{pid}/task/{tid}')
    stat = (path / 'stat').read_text()
    fields = stat[stat.rindex(')') + 2:].split()
    sched = (path / 'schedstat').read_text().split()
    return {
        'user_ticks': int(fields[11]), 'kernel_ticks': int(fields[12]),
        'runqueue_wait_ns': int(sched[1]),
    }


pid = int(os.environ['BROWSER_PID'])
boot_id = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
assert boot_id == 'c6846443-9d90-4ff5-854e-5c4132fcfbbc'
tids = {'Renderer': thread(pid, 'Renderer'),
        'SwComposite': thread(pid, 'SwComposite')}
original = {name: sorted(os.sched_getaffinity(tid)) for name, tid in tids.items()}
assert all(affinity == [0, 1, 2, 3] for affinity in original.values()), original
client = Marionette('127.0.0.1', 2828, 20)
created = False
try:
    client.set_timeout(20)
    session = client.command('WebDriver:NewSession', {'strictFileInteractability': True})
    assert isinstance(session, dict) and isinstance(session.get('sessionId'), str)
    created = True
    for run, variant in enumerate(('default-before', 'split-affinity', 'default-after'), 1):
        if variant == 'split-affinity':
            os.sched_setaffinity(tids['Renderer'], {2})
            os.sched_setaffinity(tids['SwComposite'], {3})
        else:
            for name, tid in tids.items():
                os.sched_setaffinity(tid, original[name])
        actual = {name: sorted(os.sched_getaffinity(tid)) for name, tid in tids.items()}
        before = {name: counters(pid, tid) for name, tid in tids.items()}
        started = time.monotonic()
        url = f'{URL}&run={run}&variant={variant}'
        client.set_timeout(25)
        client.command('WebDriver:Navigate', {'url': url})
        deadline = time.monotonic() + 18
        value = None
        while time.monotonic() < deadline:
            client.set_timeout(8)
            response = client.command('WebDriver:ExecuteScript', {
                'script': SCRIPT, 'args': [], 'newSandbox': True,
                'sandbox': 'asterinas-affinity', 'line': 1,
                'filename': 'asterinas-affinity'})
            if isinstance(response, dict) and 'value' in response:
                response = response['value']
            value = json.loads(response)
            if value.get('phase') == 'done':
                value['result'] = json.loads(value['text'])
                break
            if value.get('phase') == 'error':
                break
            time.sleep(0.25)
        after = {name: counters(pid, tid) for name, tid in tids.items()}
        record = {'run': run, 'variant': variant, 'boot_id': boot_id,
                  'url': url, 'elapsed_seconds': round(time.monotonic() - started, 3),
                  'tids': tids, 'affinity': actual, 'page': value,
                  'thread_deltas': {
                      name: {key: after[name][key] - before[name][key]
                             for key in before[name]}
                      for name in tids}}
        payload = json.dumps(record, separators=(',', ':')).encode()
        encoded = base64.b64encode(payload).decode()
        parts = [encoded[i:i + 80] for i in range(0, len(encoded), 80)]
        for index, part in enumerate(parts):
            print(f'AFFINITY_CHUNK run={run} i={index} data={part}', flush=True)
        print(f'AFFINITY_END run={run} parts={len(parts)} '
              f'sha256={hashlib.sha256(payload).hexdigest()}', flush=True)
        result = value.get('result') if value else None
        assert result and result['ended'] and result['error'] is None, value
        assert result['visibility'] == 'visible' and result['renderedWidth'] == 1280
        assert 300 <= result['totalFrames'] <= 305, result
finally:
    for name, tid in tids.items():
        try:
            os.sched_setaffinity(tid, original[name])
        except OSError as error:
            print(f'RESTORE_ERROR {name} {error!r}', flush=True)
    if created:
        try:
            client.set_timeout(10)
            client.command('WebDriver:DeleteSession')
        except Exception as error:
            print(f'DELETE_SESSION_ERROR {error!r}', flush=True)
    client.close()
    print('RESTORED_AFFINITY ' + json.dumps({
        name: sorted(os.sched_getaffinity(tid)) for name, tid in tids.items()}),
        flush=True)
