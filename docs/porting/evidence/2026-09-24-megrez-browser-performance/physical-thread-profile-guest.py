import base64
import hashlib
import json
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, '/usr/lib/asterinas')
sys.path.insert(0, '/run/asterinas-tools')
from browser_m5_marionette_gate import Marionette
from browser_system_time import run_thread_sampler


BASE = 'http://10.100.19.216:17895/video-source-perf.html'
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
    for run, (width, height) in enumerate(((1280, 720), (640, 360)), 1):
        mode, source = 'direct', 'clip720.webm'
        before_process = {'firefox': process_ticks(browser_pid),
                          'xorg': process_ticks(xorg_pid)}
        before_threads = thread_ticks(browser_pid)
        started = time.monotonic()
        url = f'{BASE}?mode={mode}&w={width}&h={height}&src={source}&run={run}'
        client.set_timeout(25)
        client.command('WebDriver:Navigate', {'url': url})
        navigated = time.monotonic()
        deadline = navigated + 18
        value = None
        sampler_thread = None
        sampler_results = []
        sampler_errors = []
        nonce = f'{os.getpid()}-{run}-{time.monotonic_ns()}'
        marker = Path(f'/run/firefox-video-ready-{nonce}')
        report_path = Path(f'/run/firefox-video-threads-{nonce}.json')
        while time.monotonic() < deadline:
            client.set_timeout(8)
            response = client.command('WebDriver:ExecuteScript', {
                'script': SCRIPT, 'args': [], 'newSandbox': True,
                'sandbox': 'asterinas-media-path', 'line': 1,
                'filename': 'asterinas-media-path'})
            if isinstance(response, dict) and 'value' in response:
                response = response['value']
            value = json.loads(response)
            if value.get('phase') == 'running' and sampler_thread is None:
                marker.write_text(str(time.monotonic_ns()) + '\n')
                def collect_threads():
                    try:
                        sampler_results.append(run_thread_sampler(
                            Path('/proc'), browser_pid, marker, report_path,
                            interval_seconds=0.5, samples=16, physical=True,
                        ))
                    except Exception as error:
                        sampler_errors.append(repr(error))
                sampler_thread = threading.Thread(target=collect_threads)
                sampler_thread.start()
            if value.get('phase') == 'done':
                value['result'] = json.loads(value['text'])
                break
            if value.get('phase') == 'error':
                break
            time.sleep(0.25)
        if sampler_thread is not None:
            sampler_thread.join(timeout=12)
        if sampler_thread is not None and sampler_thread.is_alive():
            sampler_errors.append('thread sampler exceeded deadline')
        summary = None
        if sampler_results:
            report = sampler_results[0]
            by_thread = {}
            for interval in report['intervals']:
                for item in interval['threads']:
                    key = (item['tid'], item['comm'])
                    entry = by_thread.setdefault(key, [0.0, 0.0, 0.0])
                    entry[0] += item['cpu_user_ms']
                    entry[1] += item['cpu_kernel_ms']
                    entry[2] += item['schedstat']['delta']['runqueue_wait_ns'] / 1e6
            top = sorted(by_thread.items(), key=lambda kv: kv[1][0] + kv[1][1],
                         reverse=True)[:15]
            summary = {
                'samples': report['samples'],
                'duration_ms': sum(i['duration_ms'] for i in report['intervals']),
                'limitations': report['limitations'],
                'raw_report_path': str(report_path),
                'top_threads': [{'tid': tid, 'name': name, 'user_ms': values[0],
                                 'kernel_ms': values[1], 'runqueue_wait_ms': values[2]}
                                for (tid, name), values in top],
            }
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
            'thread_sample': summary, 'thread_sample_errors': sampler_errors,
        }
        payload = json.dumps(record, separators=(',', ':')).encode()
        encoded = base64.b64encode(payload).decode()
        parts = [encoded[start:start + 80] for start in range(0, len(encoded), 80)]
        for index, part in enumerate(parts):
            print(f'THREAD_PROFILE_CHUNK run={run} i={index} data={part}', flush=True)
        print(f'THREAD_PROFILE_END run={run} parts={len(parts)} '
              f'sha256={hashlib.sha256(payload).hexdigest()}', flush=True)
        assert value is not None and value.get('result') is not None, value
        assert value['result']['width'] == width and value['result']['height'] == height
        assert value['result']['visibility'] == 'visible'
        assert value['result']['mode'] == mode
        assert value['result']['ended'] and value['result']['error'] is None
        assert 300 <= value['result']['totalFrames'] <= 305
        assert value['result']['renderedWidth'] == width
        assert value['result']['renderedHeight'] == height
        assert value['result']['source'] == source
        assert summary is not None and not sampler_errors, sampler_errors
finally:
    if created:
        try:
            client.set_timeout(10)
            client.command('WebDriver:DeleteSession')
        except Exception as error:
            print('DELETE_SESSION_ERROR ' + repr(error), flush=True)
    client.close()
