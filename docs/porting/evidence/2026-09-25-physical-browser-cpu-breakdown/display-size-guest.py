import importlib.util, json, os, time, traceback
from pathlib import Path

spec=importlib.util.spec_from_file_location('cpu_breakdown','/run/asterinas-cpu-breakdown.py')
cpu=importlib.util.module_from_spec(spec)
spec.loader.exec_module(cpu)
cpu.OUT=Path('/run/asterinas-cpu-display-ab.json')

def video(client,width,height,run):
    url=(cpu.BASE+'_asterinas_video.html?mode=direct&w='+str(width)+'&h='+str(height)
         +'&src=clip720.webm&run='+str(run))
    client.set_timeout(30)
    client.command('WebDriver:Navigate',{'url':url})
    deadline=time.monotonic()+35
    while time.monotonic()<deadline:
        state=cpu.script(client,cpu.SCRIPT_VIDEO)
        if state['phase']=='done':
            result=json.loads(state['text'])
            if not result['ended'] or result['error'] is not None or result['totalFrames']!=300:
                raise RuntimeError(repr(result))
            if result['renderedWidth']!=width or result['renderedHeight']!=height:
                raise RuntimeError('wrong display size: '+repr(result))
            if result['visibility']!='visible':raise RuntimeError('hidden video')
            return result
        if state['phase']=='error':raise RuntimeError(repr(state))
        time.sleep(0.5)
    raise TimeoutError('video did not finish')

def main():
    data={'schema':1,'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
          'browser_pid':cpu.BROWSER_PID,'xorg_pid':cpu.XORG_PID,'phases':[],'error':None}
    client=None
    try:
        client=cpu.Marionette('127.0.0.1',2828,20)
        client.set_timeout(30)
        client.command('WebDriver:NewSession',{'strictFileInteractability':True})
        for name,width,height in [('small_1',640,360),('small_2',640,360),('native_after',1280,720)]:
            cpu.phase(name,lambda width=width,height=height:video(client,width,height,name),data)
    except BaseException as error:
        data['error']=repr(error);traceback.print_exc()
    finally:
        if client is not None:
            try:client.set_timeout(10);client.command('WebDriver:DeleteSession')
            except Exception as error:print('DELETE_SESSION_ERROR',repr(error),flush=True)
            client.close()
        cpu.OUT.write_text(json.dumps(data,separators=(',',':')))
        print('DISPLAY_AB_FINISHED',json.dumps({'error':data['error'],'phases':[p['name'] for p in data['phases']]}),flush=True)
    return 0 if data['error'] is None else 1

if __name__=='__main__':raise SystemExit(main())
