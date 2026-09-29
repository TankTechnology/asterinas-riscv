"""Two short same-core CPU-share samples for the setpriority regression."""

import json
import os
import struct
import time


def sample(background_nice):
    children = []
    for name, nice in (('foreground', 0), ('background', background_nice)):
        start_read, start_write = os.pipe()
        ready_read, ready_write = os.pipe()
        result_read, result_write = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(start_write)
            os.close(ready_read)
            os.close(result_read)
            os.sched_setaffinity(0, {2})
            if nice:
                os.nice(nice)
            os.write(ready_write, b'1')
            deadline, = struct.unpack('<Q', os.read(start_read, 8))
            start_cpu = time.process_time_ns()
            iterations = 0
            while time.monotonic_ns() < deadline:
                iterations += 1
            result = {'name': name, 'nice': os.nice(0), 'pid': os.getpid(),
                      'cpu_ms': round((time.process_time_ns() - start_cpu) / 1e6, 3),
                      'iterations': iterations}
            os.write(result_write, json.dumps(result).encode())
            os._exit(0)
        os.close(start_read)
        os.close(ready_write)
        os.close(result_write)
        children.append((pid, start_write, ready_read, result_read))
    for _, _, ready_read, _ in children:
        assert os.read(ready_read, 1) == b'1'
    deadline = time.monotonic_ns() + 2_000_000_000
    for _, start_write, _, _ in children:
        os.write(start_write, struct.pack('<Q', deadline))
        os.close(start_write)
    result = {}
    for pid, _, _, result_read in children:
        value = json.loads(os.read(result_read, 4096))
        result[value['name']] = value
        _, status = os.waitpid(pid, 0)
        assert status == 0, status
    return result


print('BOOT_ID ' + open('/proc/sys/kernel/random/boot_id').read().strip(), flush=True)
for phase, background_nice in (('equal', 0), ('deprioritized', 10)):
    result = sample(background_nice)
    for name in ('foreground', 'background'):
        value = result[name]
        print(f"SHARE phase={phase} name={name} nice={value['nice']} "
              f"cpu_ms={value['cpu_ms']} iterations={value['iterations']}", flush=True)
