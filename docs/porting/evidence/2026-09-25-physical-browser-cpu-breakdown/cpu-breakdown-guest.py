import json, os, sys, time, traceback
from pathlib import Path

sys.path.insert(0, '/usr/lib/asterinas')
from browser_m5_marionette_gate import Marionette

BASE = 'http://10.100.19.216:18753/'
BROWSER_PID = int(os.environ['BROWSER_PID'])
XORG_PID = int(os.environ['XORG_PID'])
OUT = Path('/run/asterinas-cpu-breakdown.json')
SCRIPT_BENCH = '''return JSON.stringify({url:location.href,hash:location.hash,ready:document.readyState,progress:document.getElementById("info-progress")?.textContent,score:document.getElementById("result-number")?.textContent,summary:document.getElementById("summary")?.className});'''
SCRIPT_VIDEO = '''return JSON.stringify((() => { const s=document.querySelector('#status'); return {phase:s?.dataset.phase,text:s?.textContent,visibility:document.visibilityState,url:location.href}; })());'''

def stat_line(line):
    end = line.rindex(')')
    fields = line[end + 2:].split()
    if len(fields) < 20:
        raise ValueError('short proc stat')
    return {'comm': line[line.index('(') + 1:end], 'ppid': int(fields[1]),
            'minflt': int(fields[7]), 'majflt': int(fields[9]),
            'user': int(fields[11]), 'system': int(fields[12]),
            'start': int(fields[19])}

def read_stat(path):
    try:
        return stat_line(path.read_text())
    except (OSError, ValueError, IndexError):
        return None

def read_sched(path):
    try:
        values = path.read_text().split()
        if len(values) != 3: return None
        return {'runtime_ns':int(values[0]), 'wait_ns':int(values[1]), 'dispatches':int(values[2])}
    except (OSError, ValueError):
        return None

def snapshot():
    ticks = os.sysconf('SC_CLK_TCK')
    uptime = float(Path('/proc/uptime').read_text().split()[0])
    proc = {}
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit(): continue
        record = read_stat(entry / 'stat')
        if record is not None: proc[int(entry.name)] = record
    selected = {BROWSER_PID, XORG_PID}
    changed = True
    while changed:
        before = len(selected)
        selected.update(pid for pid,p in proc.items() if p['ppid'] in selected and p['ppid'] != XORG_PID)
        changed = len(selected) != before
    selected.intersection_update(proc)
    threads = {}
    for pid in selected:
        entries = {}
        try:
            for entry in (Path('/proc') / str(pid) / 'task').iterdir():
                if not entry.name.isdigit(): continue
                t = read_stat(entry / 'stat')
                if t is not None:
                    t['sched'] = read_sched(entry / 'schedstat')
                    entries[int(entry.name)] = t
        except OSError: pass
        threads[pid] = entries
    cpu = None
    try:
        cpu = [int(v) for v in Path('/proc/stat').read_text().splitlines()[0].split()[1:]]
    except (OSError, ValueError, IndexError): pass
    return {'mono':time.monotonic(),'uptime':uptime,'ticks_per_second':ticks,
            'proc':proc,'selected':sorted(selected),'threads':threads,'cpu':cpu}

def counters_delta(a,b):
    return {k:b[k]-a[k] for k in ('user','system','minflt','majflt')}

def summarize(before,after):
    ticks = before['ticks_per_second']
    result = {'wall_seconds':round(after['mono']-before['mono'],3),
              'ticks_per_second':ticks,'processes':[], 'threads':[], 'exited_pids':[],
              'new_pids':[], 'global_cpu_ticks':None}
    if before['cpu'] is not None and after['cpu'] is not None:
        result['global_cpu_ticks']=[b-a for a,b in zip(before['cpu'],after['cpu'])]
    for pid in before['selected']:
        if pid not in after['selected'] or before['proc'][pid]['start'] != after['proc'][pid]['start']:
            result['exited_pids'].append(pid)
    for pid in after['selected']:
        current=after['proc'][pid]
        earlier=before['proc'].get(pid)
        if earlier is None or earlier['start'] != current['start']:
            result['new_pids'].append(pid)
            if current['start'] < before['uptime'] * ticks: continue
            earlier={'user':0,'system':0,'minflt':0,'majflt':0}
        delta=counters_delta(earlier,current)
        result['processes'].append({'pid':pid,'comm':current['comm'],
            'role':'xorg' if pid==XORG_PID else ('firefox-main' if pid==BROWSER_PID else 'firefox-child'),
            **delta})
        bt=before['threads'].get(pid,{})
        for tid,now in after['threads'].get(pid,{}).items():
            old=bt.get(tid)
            if old is None or old['start'] != now['start']:
                if now['start'] < before['uptime'] * ticks: continue
                old={'user':0,'system':0,'sched':None}
            row={'pid':pid,'tid':tid,'comm':now['comm'],'user':now['user']-old['user'],
                 'system':now['system']-old['system']}
            if old['sched'] is not None and now['sched'] is not None:
                row.update({'wait_ns':now['sched']['wait_ns']-old['sched']['wait_ns'],
                            'dispatches':now['sched']['dispatches']-old['sched']['dispatches']})
            result['threads'].append(row)
    result['processes'].sort(key=lambda p:p['user']+p['system'],reverse=True)
    result['threads'].sort(key=lambda p:p['user']+p['system'],reverse=True)
    return result

def script(client,source):
    client.set_timeout(15)
    value=client.command('WebDriver:ExecuteScript',{'script':source,'args':[],
        'newSandbox':True,'sandbox':'asterinas-cpu-breakdown','line':1,
        'filename':'asterinas-cpu-breakdown'})
    if isinstance(value,dict): value=value.get('value')
    return json.loads(value)

def benchmark(client):
    url=BASE+'?iterationCount=1&startAutomatically'
    client.set_timeout(120)
    client.command('WebDriver:Navigate',{'url':url})
    deadline=time.monotonic()+260
    last=None
    while time.monotonic()<deadline:
        last=script(client,SCRIPT_BENCH)
        if last['hash']=='#summary':
            if last['summary']!='valid' or not last['score'] or last['score']=='Error':
                raise RuntimeError('invalid Speedometer result: '+repr(last))
            return last
        time.sleep(2)
    raise TimeoutError('Speedometer did not complete: '+repr(last))

def video(client,run):
    url=BASE+'_asterinas_video.html?mode=direct&w=1280&h=720&src=clip720.webm&run='+str(run)
    client.set_timeout(30)
    client.command('WebDriver:Navigate',{'url':url})
    deadline=time.monotonic()+35
    last=None
    while time.monotonic()<deadline:
        last=script(client,SCRIPT_VIDEO)
        if last['phase']=='done':
            result=json.loads(last['text'])
            if not result['ended'] or result['error'] is not None or result['totalFrames']!=300 or result['visibility']!='visible':
                raise RuntimeError('invalid video result: '+repr(result))
            return result
        if last['phase']=='error': raise RuntimeError('video error: '+repr(last))
        time.sleep(0.5)
    raise TimeoutError('video did not end: '+repr(last))

def phase(name,operation,data):
    print('PHASE_START',name,flush=True)
    before=snapshot(); output=operation(); after=snapshot()
    record={'name':name,'measurement':summarize(before,after),'output':output}
    data['phases'].append(record)
    print('PHASE_DONE',name,'wall',record['measurement']['wall_seconds'],flush=True)
    OUT.write_text(json.dumps(data,separators=(',',':')))

def main():
    data={'schema':1,'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
          'browser_pid':BROWSER_PID,'xorg_pid':XORG_PID,
          'phase_order':['idle_before','speedometer','idle_after','video_1','video_2','video_3','idle_final'],
          'phases':[],'error':None}
    client=None
    try:
        client=Marionette('127.0.0.1',2828,20)
        client.set_timeout(30)
        session=client.command('WebDriver:NewSession',{'strictFileInteractability':True})
        data['firefox_version']=session.get('capabilities',{}).get('browserVersion')
        phase('idle_before',lambda:(time.sleep(12) or None),data)
        phase('speedometer',lambda:benchmark(client),data)
        phase('idle_after',lambda:(time.sleep(12) or None),data)
        for run in (1,2,3): phase('video_'+str(run),lambda run=run:video(client,run),data)
        phase('idle_final',lambda:(time.sleep(12) or None),data)
    except BaseException as error:
        data['error']=repr(error)
        traceback.print_exc()
    finally:
        if client is not None:
            try:
                client.set_timeout(10);client.command('WebDriver:DeleteSession')
            except Exception as error: print('DELETE_SESSION_ERROR',repr(error),flush=True)
            client.close()
        OUT.write_text(json.dumps(data,separators=(',',':')))
        print('PROBE_FINISHED',json.dumps({'error':data['error'],'phases':[x['name'] for x in data['phases']]}),flush=True)
    return 0 if data['error'] is None else 1

if __name__=='__main__': raise SystemExit(main())
