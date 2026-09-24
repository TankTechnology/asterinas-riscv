# SPDX-License-Identifier: MPL-2.0

import json
import os
import sys
import time

sys.path.insert(0, '/usr/lib/asterinas')
from browser_m5_marionette_gate import Marionette

BASE = 'http://10.100.19.216:17894/'
SCRIPT = '''return JSON.stringify((() => {
  const v = document.querySelector('video');
  if (!v) return {missing: true, url: location.href};
  const q = v.getVideoPlaybackQuality();
  return {url: location.href, currentTime: v.currentTime, duration: v.duration,
    ended: v.ended, readyState: v.readyState, error: v.error && v.error.code,
    width: v.videoWidth, height: v.videoHeight,
    totalFrames: q.totalVideoFrames, droppedFrames: q.droppedVideoFrames};
})());'''


def ticks(pid):
    words = open(f'/proc/{pid}/stat').read().split()
    return int(words[13]) + int(words[14])


browser_pid = int(os.environ['BROWSER_PID'])
xorg_pid = int(os.environ['XORG_PID'])
client = Marionette('127.0.0.1', 2828, 20)
created = False
try:
    client.set_timeout(20)
    session = client.command('WebDriver:NewSession', {'strictFileInteractability': True})
    assert isinstance(session, dict) and isinstance(session.get('sessionId'), str)
    created = True
    for page in ('index720.html', 'index720-small.html'):
        before = {'firefox': ticks(browser_pid), 'xorg': ticks(xorg_pid)}
        started = time.monotonic()
        client.set_timeout(25)
        client.command('WebDriver:Navigate', {'url': BASE + page})
        navigated = time.monotonic()
        deadline = navigated + 18
        while True:
            client.set_timeout(10)
            value = client.command('WebDriver:ExecuteScript', {
                'script': SCRIPT, 'args': [], 'newSandbox': True,
                'sandbox': 'asterinas-quick-media', 'line': 1,
                'filename': 'asterinas-quick-media'})
            if isinstance(value, dict) and 'value' in value:
                value = value['value']
            sample = json.loads(value)
            if sample.get('ended') or sample.get('error') or time.monotonic() >= deadline:
                break
            time.sleep(.25)
        after = {'firefox': ticks(browser_pid), 'xorg': ticks(xorg_pid)}
        print(f'MEDIA page={page} ended={int(sample["ended"])} error={sample["error"]} '
              f'frames={sample["totalFrames"]} dropped={sample["droppedFrames"]}', flush=True)
        print(f'CPU page={page} firefox_ticks={after["firefox"] - before["firefox"]} '
              f'xorg_ticks={after["xorg"] - before["xorg"]}', flush=True)
        print(f'TIME page={page} navigate={navigated - started:.3f} '
              f'elapsed={time.monotonic() - navigated:.3f}', flush=True)
        assert sample['ended'] and sample['error'] is None and sample['totalFrames'] == 300
finally:
    if created:
        try:
            client.set_timeout(10)
            client.command('WebDriver:DeleteSession')
        except Exception as error:
            print('DELETE_SESSION_ERROR ' + repr(error), flush=True)
    client.close()
