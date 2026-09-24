# SPDX-License-Identifier: MPL-2.0

import hashlib,json,os,subprocess,time
from pathlib import Path
START=time.monotonic()
def emit(kind,value):print('LIFECYCLE '+json.dumps({'kind':kind,'elapsed':round(time.monotonic()-START,3),'value':value}),flush=True)
meta=__BUNDLE_META__
archive=Path('/opt/lmbench-runtime.tar.gz');assert hashlib.sha256(archive.read_bytes()).hexdigest()==meta['archive_sha256']
subprocess.run(['tar','xzf',str(archive),'-C','/'],check=True,timeout=30)
for test in ['/opt/reserved-ports-test','/opt/udp-loopback-test']:
 r=subprocess.run([test],capture_output=True,text=True,timeout=5);emit('regression',{'test':test,'status':r.returncode,'stdout':r.stdout,'stderr':r.stderr});assert r.returncode==0
subprocess.run(['systemctl','stop','asterinas-desktop-m5.service'],check=True,timeout=6)
subprocess.run(['useradd','--system','--no-create-home','--shell','/usr/sbin/nologin','rpc'],check=True,timeout=5)
root=Path('/opt/lmbench');runtime=json.loads((root/'asterinas-runtime.json').read_text())
env=os.environ.copy();env['PATH']=runtime['packages']['make']+'/bin:'+env['PATH']
log=Path('/run/native.log').open('w')
p=subprocess.Popen(['make','results'],cwd=root/'src',env=env,stdout=log,stderr=subprocess.STDOUT)
emit('native-start',{'argv':['make','results'],'pid':p.pid})
while p.poll() is None:
 time.sleep(10)
 active=[]
 for d in Path('/proc').iterdir():
  if not d.name.isdigit():continue
  try:
   cmd=(d/'cmdline').read_text().replace('\0',' ')
   if cmd and any(x in cmd.split(' ')[0] for x in ['lat_','bw_','rpcbind','mhz','line','memsize','enough','make','par_','stream','tlb','lmdd']):active.append({'pid':d.name,'cmd':cmd})
  except OSError:pass
 emit('progress',{'active':active})
for f in (root/'asterinas-runs').rglob('*'):
 if f.is_file() and f.name in ('report.json','native-results.txt','CONFIG','rpcbind.log','stdout','stderr','status.log'):
  emit('artifact',{'path':str(f),'text':f.read_text(errors='replace')})
emit('native-exit',{'status':p.returncode})
emit('complete','native-results-collected')
assert p.returncode==0
