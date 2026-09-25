import json
import sys

sys.path.insert(0, '/usr/lib/asterinas')
from browser_m5_marionette_gate import Marionette


client = Marionette('127.0.0.1', 2829, 20)
created = False
try:
    client.set_timeout(20)
    session = client.command('WebDriver:NewSession', {'strictFileInteractability': True})
    assert isinstance(session, dict) and isinstance(session.get('sessionId'), str)
    created = True
    client.set_timeout(10)
    client.command('WebDriver:Navigate', {'url': 'about:support'})
    rows = client.command('WebDriver:ExecuteScript', {
        'script': '''return JSON.stringify(Array.from(document.querySelectorAll('tr'))
          .map(row => row.textContent.trim().replace(/\\s+/g, ' '))
          .filter(row => /Compositing|WebRender|GPU Process/i.test(row))
          .slice(0, 8));''',
        'args': [], 'newSandbox': True, 'sandbox': 'default',
        'line': 1, 'filename': 'asterinas-compositor-probe',
    })
    if isinstance(rows, dict) and 'value' in rows:
        rows = rows['value']
    for row in json.loads(rows):
        print('SUPPORT_ROW ' + row[:110], flush=True)
    context = client.command('Marionette:SetContext', {'value': 'chrome'})
    print('SET_CONTEXT ' + repr(context), flush=True)
    script = '''return JSON.stringify((() => {
      const profiler = typeof Services !== 'undefined' && Services.profiler;
      return {services: typeof Services, chromeUtils: typeof ChromeUtils,
        profiler: typeof profiler,
        active: profiler && profiler.IsActive(),
        features: profiler && profiler.GetFeatures().slice(0, 20),
        firefoxVersion: Services.appinfo.version,
        forceDisabled: Services.prefs.getBoolPref('gfx.webrender.force-disabled', false),
        software: Services.prefs.getBoolPref('gfx.webrender.software', false)};
    })());'''
    result = client.command('WebDriver:ExecuteScript', {
        'script': script, 'args': [], 'newSandbox': True,
        'sandbox': 'system', 'line': 1, 'filename': 'asterinas-profiler-probe',
    })
    if isinstance(result, dict) and 'value' in result:
        result = result['value']
    print('PROFILER_PROBE ' + json.dumps(json.loads(result), separators=(',', ':')),
          flush=True)
finally:
    if created:
        try:
            client.set_timeout(10)
            client.command('WebDriver:DeleteSession')
        except Exception as error:
            print('DELETE_SESSION_ERROR ' + repr(error), flush=True)
    client.close()
