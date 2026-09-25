import importlib.util, json, os, subprocess, time, traceback
from pathlib import Path

DIAG=Path('/run/asterinas-wave3')
DIAG.mkdir(exist_ok=True)
spec=importlib.util.spec_from_file_location('cpu_breakdown','/run/asterinas-cpu-breakdown.py')
cpu=importlib.util.module_from_spec(spec);spec.loader.exec_module(cpu)
cpu.OUT=DIAG/'result.json'
EVENTS=DIAG/'phase-events.jsonl'

def event(name,edge):
    row={'name':name,'edge':edge,'monotonic_ns':time.monotonic_ns(),
         'uptime':float(Path('/proc/uptime').read_text().split()[0])}
    with EVENTS.open('a') as stream:stream.write(json.dumps(row,separators=(',',':'))+'\n')
    print('WAVE3_PHASE',name,edge,'uptime',row['uptime'],flush=True)
    try:subprocess.run(['/usr/bin/logger','-t','ASTERINAS_WAVE3',json.dumps(row,separators=(',',':'))],timeout=2,check=False)
    except (OSError,subprocess.TimeoutExpired):pass

def phase(name,op,data):
    event(name,'start')
    try:cpu.phase(name,op,data)
    finally:event(name,'end')

def video(client,run,sample=False):
    url=cpu.BASE+'_asterinas_video.html?mode=direct&w=1280&h=720&src=clip720.webm&run='+run
    client.set_timeout(30);client.command('WebDriver:Navigate',{'url':url})
    deadline=time.monotonic()+35
    sampler=None
    last=None
    try:
        while time.monotonic()<deadline:
            last=cpu.script(client,cpu.SCRIPT_VIDEO)
            if sample and sampler is None and last['phase']=='running':
                sampler=subprocess.Popen(['/usr/bin/timeout','25','/usr/bin/python3',
                    '/run/asterinas-thread-pc-sampler.py','--pid',str(cpu.BROWSER_PID),
                    '--comm','Renderer','--comm','SwComposite','--samples','30',
                    '--interval-ms','200','--output',str(DIAG/'active-pcs.jsonl')],
                    stdout=(DIAG/'pc-sampler.stdout').open('w'),
                    stderr=(DIAG/'pc-sampler.stderr').open('w'))
            if last['phase']=='done':
                result=json.loads(last['text'])
                if not result['ended'] or result['error'] is not None or result['totalFrames']!=300 or result['visibility']!='visible':
                    raise RuntimeError('invalid video: '+repr(result))
                if result['renderedWidth']!=1280 or result['renderedHeight']!=720:
                    raise RuntimeError('wrong display size')
                return result
            if last['phase']=='error':raise RuntimeError('video error: '+repr(last))
            time.sleep(0.25)
        raise TimeoutError('video did not end: '+repr(last))
    finally:
        if sampler is not None:
            try:
                status=sampler.wait(timeout=10)
                print('PC_SAMPLER_STATUS',status,flush=True)
                if status!=0:raise RuntimeError('PC sampler exited '+str(status))
            except subprocess.TimeoutExpired:
                sampler.kill();sampler.wait(timeout=3)
                raise TimeoutError('PC sampler deadline')

def main():
    data={'schema':1,'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
          'firefox_pid':cpu.BROWSER_PID,'xorg_pid':cpu.XORG_PID,'phases':[],'error':None}
    client=None
    try:
        client=cpu.Marionette('127.0.0.1',2828,20)
        client.set_timeout(30);s=client.command('WebDriver:NewSession',{'strictFileInteractability':True})
        data['firefox_version']=s.get('capabilities',{}).get('browserVersion')
        phase('idle_before',lambda:(time.sleep(10) or None),data)
        phase('speedometer',lambda:cpu.benchmark(client),data)
        phase('video_base_a',lambda:video(client,'base_a'),data)
        phase('video_pc_sample',lambda:video(client,'pc_sample',True),data)
        phase('video_base_b',lambda:video(client,'base_b'),data)
        phase('idle_after',lambda:(time.sleep(10) or None),data)
    except BaseException as error:
        data['error']=repr(error);traceback.print_exc()
    finally:
        if client is not None:
            try:client.set_timeout(10);client.command('WebDriver:DeleteSession')
            except Exception as error:print('DELETE_SESSION_ERROR',repr(error),flush=True)
            client.close()
        cpu.OUT.write_text(json.dumps(data,separators=(',',':')))
        print('WAVE3_FINISHED',json.dumps({'error':data['error'],'phases':[x['name'] for x in data['phases']]}),flush=True)
    return 0 if data['error'] is None else 1

if __name__=='__main__':raise SystemExit(main())
