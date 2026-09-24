# SPDX-License-Identifier: MPL-2.0

import hashlib,json,math,os,re,shutil,signal,subprocess,time
from pathlib import Path
START=time.monotonic()
def emit(kind,value):
    print('LIFECYCLE '+json.dumps({'kind':kind,'elapsed':round(time.monotonic()-START,3),'value':value}),flush=True)
meta=__BUNDLE_META__
archive=Path('/opt/lmbench-runtime.tar.gz')
assert hashlib.sha256(archive.read_bytes()).hexdigest()==meta['archive_sha256']
subprocess.run(['tar','xzf',str(archive),'-C','/'],check=True,timeout=90)
bindir=Path(meta['package'])/'bin'
for name in ['lat_syscall','lat_proc','lat_pipe','lat_ctx','lat_sig','bw_mem','lat_mmap','lat_pagefault','hello']:
    assert hashlib.sha256((bindir/name).read_bytes()).hexdigest()==meta['binary_sha256'][name],name
emit('identity',{'kernel':os.uname().release,'arch':os.uname().machine,'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),'bundle':meta['archive_sha256']})
script=Path('/run/lmbench-smoke.py')
script.write_text(__RUNNER_SOURCE__)
p=subprocess.run(['python3',str(script),'--bin-dir',str(bindir),'--output','/run/lmbench-result.json'],timeout=100)
report=json.loads(Path('/run/lmbench-result.json').read_text())
emit('smoke-report',report)
assert p.returncode==0 and report['passed'],report
emit('complete','pass')
