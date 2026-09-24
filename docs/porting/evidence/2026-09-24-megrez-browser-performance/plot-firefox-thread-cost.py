#!/usr/bin/env python3
"""Draw thread-level CPU and runqueue attribution from the short Megrez probe."""

import json
from html import escape
from pathlib import Path

root = Path(__file__).resolve().parent
data = json.loads((root / 'physical-thread-profile-result.json').read_text())
samples = data['samples']
names = ('Renderer', 'SwComposite')
rows = []
for name in names:
    for sample, label in zip(samples, ('1280×720', '640×360')):
        thread = next(t for t in sample['thread_sample']['top_threads'] if t['name'] == name)
        rows.append((name, label, thread['user_ms'] / 1000,
                     thread['kernel_ms'] / 1000,
                     thread['runqueue_wait_ms'] / 1000))

parts = ['''<svg xmlns="http://www.w3.org/2000/svg" width="1120" height="540"
viewBox="0 0 1120 540" role="img"
aria-label="Firefox video playback thread CPU and runqueue wait at two display sizes">
<rect width="1120" height="540" fill="#fbfcfe"/>
<style>
text{font-family:DejaVu Sans,Arial,sans-serif;fill:#1b2735}
.title{font-size:24px;font-weight:700}.subtitle{font-size:13px;fill:#526479}
.head{font-size:16px;font-weight:700}.row{font-size:14px}
.small{font-size:12px;fill:#607186}.value{font-size:13px;font-weight:600}
</style>
<text x="42" y="46" class="title">Firefox 720p VP8: thread-level cost</text>
<text x="42" y="72" class="subtitle">Same 300-frame source; one 10.5 s sampled playback per display size on Megrez Asterinas</text>
<text x="260" y="122" class="head">CPU time (s)</text>
<text x="770" y="122" class="head">Runqueue wait (s)</text>
<rect x="260" y="140" width="14" height="14" fill="#d56738"/><text x="281" y="152" class="small">user</text>
<rect x="333" y="140" width="14" height="14" fill="#efaa78"/><text x="354" y="152" class="small">kernel</text>
<rect x="770" y="140" width="14" height="14" fill="#4386b9"/><text x="791" y="152" class="small">runnable, awaiting CPU</text>
''']

for x, label in ((260, '0'), (378, '2'), (496, '4'), (614, '6'), (732, '8')):
    parts.append(f'<line x1="{x}" y1="172" x2="{x}" y2="426" stroke="#dce4eb"/>')
    parts.append(f'<text x="{x}" y="449" text-anchor="middle" class="small">{label}</text>')
for x, label in ((770, '0'), (865, '1'), (960, '2'), (1055, '3')):
    parts.append(f'<line x1="{x}" y1="172" x2="{x}" y2="426" stroke="#dce4eb"/>')
    parts.append(f'<text x="{x}" y="449" text-anchor="middle" class="small">{label}</text>')

for index, (name, size, user, kernel, wait) in enumerate(rows):
    y = 190 + index * 61
    if index == 2:
        parts.append('<line x1="42" y1="302" x2="1077" y2="302" stroke="#d0dbe5"/>')
    parts.append(f'<text x="42" y="{y + 18}" class="row">{escape(name)} · {escape(size)}</text>')
    parts.append(f'<rect x="260" y="{y}" width="{user * 59:.1f}" height="26" rx="3" fill="#d56738"/>')
    parts.append(f'<rect x="{260 + user * 59:.1f}" y="{y}" width="{kernel * 59:.1f}" height="26" fill="#efaa78"/>')
    parts.append(f'<text x="{272 + (user + kernel) * 59:.1f}" y="{y + 19}" class="value">{user + kernel:.2f}</text>')
    parts.append(f'<rect x="770" y="{y}" width="{wait * 95:.1f}" height="26" rx="3" fill="#4386b9"/>')
    parts.append(f'<text x="{782 + wait * 95:.1f}" y="{y + 19}" class="value">{wait:.2f}</text>')

native_drop = samples[0]['page']['result']['droppedFrames']
small_drop = samples[1]['page']['result']['droppedFrames']
parts.append(f'<text x="42" y="491" class="small">Dropped video frames: native {native_drop}/300; small {small_drop}/300. CPU and wait are separate counters, not additive wall time.</text>')
parts.append('<text x="42" y="513" class="small">This is thread attribution, not a native function-stack flame graph; symbols and RISC-V sampling support were unavailable.</text>')
parts.append('</svg>')
(root / 'firefox-thread-cost.svg').write_text('\n'.join(parts) + '\n')
