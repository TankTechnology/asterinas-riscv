import json
import os
import ctypes
from pathlib import Path
import sys
import time

sys.path.insert(0, '/usr/lib/asterinas')
from browser_m5_marionette_gate import Marionette

HOST = 'http://10.100.19.216:17897'
BROWSER_PID = int(os.environ['BROWSER_PID'])
XORG_PID = int(os.environ['XORG_PID'])


class SchedAttr(ctypes.Structure):
    _fields_ = [('size', ctypes.c_uint32), ('policy', ctypes.c_uint32),
                ('flags', ctypes.c_uint64), ('nice', ctypes.c_int32),
                ('priority', ctypes.c_uint32), ('runtime', ctypes.c_uint64),
                ('deadline', ctypes.c_uint64), ('period', ctypes.c_uint64),
                ('util_min', ctypes.c_uint32), ('util_max', ctypes.c_uint32)]


libc = ctypes.CDLL(None, use_errno=True)


def sched_get(tid):
    attr = SchedAttr()
    result = libc.syscall(275, tid, ctypes.byref(attr), ctypes.sizeof(attr), 0)
    if result != 0:
        raise OSError(ctypes.get_errno(), 'sched_getattr')
    return attr


def sched_set(tid, attr):
    result = libc.syscall(274, tid, ctypes.byref(attr), 0)
    if result != 0:
        raise OSError(ctypes.get_errno(), 'sched_setattr')


def tasks(pid):
    result = {}
    for path in Path(f'/proc/{pid}/task').iterdir():
        try:
            stat = (path / 'stat').read_text()
            name = stat[stat.index('(') + 1:stat.rindex(')')]
            fields = stat[stat.rindex(')') + 2:].split()
            schedstat = (path / 'schedstat').read_text().split()
            result[int(path.name)] = {
                'name': name,
                'user_ticks': int(fields[11]),
                'kernel_ticks': int(fields[12]),
                'run_ns': int(schedstat[0]),
                'wait_ns': int(schedstat[1]),
            }
        except (OSError, ValueError, IndexError):
            continue
    return result


def delta(before, after):
    rows = []
    for tid, current in after.items():
        old = before.get(tid)
        if old is None:
            continue
        row = {'tid': tid, 'name': current['name']}
        for field in ('user_ticks', 'kernel_ticks', 'run_ns', 'wait_ns'):
            row[field] = current[field] - old[field]
        rows.append(row)
    return sorted(rows, key=lambda row: row['run_ns'], reverse=True)


client = Marionette('127.0.0.1', 2828, 20)
created = False
priority_saved = {}
priority_mode = os.environ.get('PERF_PRIORITY_ABA') == '1'
if priority_mode:
    runs = (('prioa', 'large'), ('priob', 'large'), ('prioa2', 'large'))
else:
    runs = (('large3', 'large'), ('small2', 'small'))
try:
    client.set_timeout(20)
    session = client.command('WebDriver:NewSession', {'strictFileInteractability': True})
    assert isinstance(session, dict) and isinstance(session.get('sessionId'), str)
    created = True
    for run, size in runs:
        if priority_mode:
            hot = {tid: row for tid, row in tasks(BROWSER_PID).items()
                   if row['name'] in ('Renderer', 'SwComposite')}
            assert len(hot) == 2, hot
            for tid in hot:
                priority_saved.setdefault(tid, sched_get(tid))
                attr = sched_get(tid)
                assert attr.policy == 0, attr.policy
                attr.nice = -5 if run == 'priob' else priority_saved[tid].nice
                sched_set(tid, attr)
            print(f"PERF_PRIORITY run={run} " + ' '.join(
                f"{hot[tid]['name']}:{sched_get(tid).nice}"
                for tid in hot), flush=True)
        before_firefox = tasks(BROWSER_PID)
        before_xorg = tasks(XORG_PID)
        client.set_timeout(30)
        client.command('WebDriver:Navigate', {
            'url': f'{HOST}/probe?run={run}&size={size}&filter=auto',
        })
        deadline = time.monotonic() + 25
        metric = None
        while time.monotonic() < deadline:
            client.set_timeout(8)
            response = client.command('WebDriver:ExecuteScript', {
                'script': "return document.querySelector('#status').textContent;",
                'args': [], 'newSandbox': True, 'sandbox': 'asterinas-video-perf',
                'line': 1, 'filename': 'asterinas-video-perf',
            })
            status = response.get('value') if isinstance(response, dict) else response
            if isinstance(status, str) and status.startswith('{'):
                metric = json.loads(status)
                break
            time.sleep(.25)
        record = {
            'run': run,
            'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
            'metric': metric,
            'firefox_threads': delta(before_firefox, tasks(BROWSER_PID))[:8],
            'xorg_threads': delta(before_xorg, tasks(XORG_PID))[:4],
        }
        print(f"PERF_SUMMARY run={run} dropped={metric['droppedVideoFrames']} frames={metric['totalVideoFrames']}", flush=True)
        for process in ('firefox', 'xorg'):
            for row in record[process + '_threads']:
                print(f"PERF_THREAD run={run} process={process} name={row['name']} "
                      f"tid={row['tid']} user={row['user_ticks']} "
                      f"kernel={row['kernel_ticks']} run_ns={row['run_ns']} "
                      f"wait_ns={row['wait_ns']}", flush=True)
        assert metric is not None and metric['state'] == 'ended', record
        assert metric['totalVideoFrames'] in (300, 301, 302, 303), record
finally:
    for tid, old_attr in priority_saved.items():
        try:
            sched_set(tid, old_attr)
        except OSError as error:
            print(f'PERF_RESTORE_ERROR tid={tid} error={error}', flush=True)
            pass
    if created:
        try:
            client.set_timeout(10)
            client.command('WebDriver:DeleteSession')
        except Exception as error:
            print('DELETE_SESSION_ERROR ' + repr(error), flush=True)
    client.close()
