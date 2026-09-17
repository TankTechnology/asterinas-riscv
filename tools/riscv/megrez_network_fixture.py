#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""Serve one deterministic payload for bounded Megrez network gates."""

from __future__ import annotations

import argparse
import hashlib
import http.server
import ipaddress
import json
import re
import signal
import struct
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit


FIXTURE_PATH = "/asterinas-network-probe.bin"
PAYLOAD_SIZE = 64 * 1024
PAYLOAD = bytes(range(256)) * (PAYLOAD_SIZE // 256)
PAYLOAD_SHA256 = hashlib.sha256(PAYLOAD).hexdigest()
MAX_REQUEST_RECORDS = 64
BROWSER_INDEX_PATH = "/browser-quality/index.html"
BROWSER_SECOND_PATH = "/browser-quality/second.html"
BROWSER_PERF_PATH = "/browser-quality/perf.html"
BROWSER_PERF_SECOND_PATH = "/browser-quality/perf-second.html"
BROWSER_WORKLOAD_PATH = "/browser-quality/workload.html"
BROWSER_WORKLOAD_RESOURCE_PATH = "/browser-quality/workload-resource.bin"
BROWSER_WORKLOAD_IMAGE_PATH = "/browser-quality/workload-image.png"
BROWSER_IMAGE_PATH = "/browser-quality/pattern.png"
BROWSER_DOWNLOAD_PATH = "/browser-quality/download.bin"
BROWSER_API_PATH = "/browser-quality/capabilities.json"
BROWSER_AUDIO_PATH = "/browser-quality/tone.wav"
BROWSER_CAPTURE_PATH = "/browser-quality/capture.xwd.gz"
BROWSER_PNG_CAPTURE_PATH = "/browser-quality/capture.png"
MAX_CAPTURE_BYTES = 8 * 1024 * 1024
MAX_WORKLOAD_REQUEST_RECORDS = 512
BROWSER_DOWNLOAD = bytes(range(256)) * 1024
BROWSER_DOWNLOAD_SHA256 = hashlib.sha256(BROWSER_DOWNLOAD).hexdigest()
BROWSER_API = b'{"schema_version":1,"token":"asterinas-browser-quality"}\n'
WORKLOAD_RESOURCE_SIZE = 64 * 1024
WORKLOAD_RESOURCE = bytes(range(256)) * (WORKLOAD_RESOURCE_SIZE // 256)


def _pcm_wav() -> bytes:
    """Return 250 ms of deterministic mono PCM without an external asset."""

    samples = bytes((128 + ((index % 32) - 16) * 4) & 0xFF for index in range(2000))
    return (
        b"RIFF"
        + struct.pack("<I", 36 + len(samples))
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, 8000, 8000, 1, 8)
        + b"data"
        + struct.pack("<I", len(samples))
        + samples
    )


BROWSER_AUDIO = _pcm_wav()
BROWSER_IMAGE = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000020000000200806000000737a7af4"
    "000000414944415478da6310cf58f31f19a3035acb338c3a60c01d406f0bd1e547"
    "1d30f00e18cd05a30e18cd05a30e18cd05a30e18cd05a30e18cd05a30e18cd0523"
    "de0100a3694cb594617d3a0000000049454e44ae426082"
)
BROWSER_INDEX = b"""<!doctype html>
<meta charset=utf-8><title>Asterinas Browser Quality</title>
<link rel=icon href=/browser-quality/pattern.png>
<style>body{font-family:sans-serif}.scroll{height:1600px}
.second{position:absolute;left:40px;top:220px}
.download{position:absolute;left:40px;top:260px}</style>
<h1>Asterinas browser quality / \xe6\xb5\x8f\xe8\xa7\x88\xe5\x99\xa8\xe8\xb4\xa8\xe9\x87\x8f</h1>
<form method=get><input name=q><button>Search</button></form>
<img src=/browser-quality/pattern.png alt=pattern>
<p class=second><a href=/browser-quality/second.html>Second page</a></p>
<p class=download><a id=quality-download download=asterinas-browser-quality.bin
 href=/browser-quality/download.bin>Download</a></p>
<canvas id=quality-canvas width=1 height=1 hidden></canvas>
<audio id=quality-audio preload=auto hidden></audio>
<output id=quality-capabilities hidden></output>
<div class=scroll></div>
<script>
(() => {
  'use strict';
  const checks = Object.create(null);
  let wasmFailure = null;
  const search = new URLSearchParams(location.search).get('q') === 'asterinas';
  const phase = search ? 'search' : 'home';
  window.__asterinasCapabilities = {version: 1, phase, state: 'running', checks, error: null};
  const publish = () => {
    document.querySelector('#quality-capabilities').textContent =
      JSON.stringify(window.__asterinasCapabilities);
  };
  const mark = name => {
    publish();
    // Firefox mirrors console output to the guest stderr log used by the
    // QEMU gate.  These markers remain useful when an API blocks the
    // Marionette probe before it can return a JSON snapshot.
    console.log('A_WEB_CAPABILITY_STEP step=' + name);
  };
  publish();
  // Capability checks must not monopolize the page probe on a guest whose
  // storage or media stack is unavailable.  Five seconds is ample for these
  // tiny local resources; a timeout is recorded as a failed capability and
  // the overall gate remains fail-closed.
  const failAfter = (promise, name) => Promise.race([
    promise,
    new Promise((_, reject) => setTimeout(() => reject(new Error(name + '-timeout')), 5000))
  ]);
  const openDatabase = () => new Promise((resolve, reject) => {
    const request = indexedDB.open('asterinas-browser-quality-v1', 1);
    request.onupgradeneeded = () => request.result.createObjectStore('quality');
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error || new Error('indexeddb-open'));
  });
  const transactionDone = transaction => new Promise((resolve, reject) => {
    transaction.oncomplete = resolve;
    transaction.onerror = () => reject(transaction.error || new Error('indexeddb-transaction'));
    transaction.onabort = () => reject(transaction.error || new Error('indexeddb-abort'));
  });
  const readDatabase = (database, key) => new Promise((resolve, reject) => {
    const request = database.transaction('quality').objectStore('quality').get(key);
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error || new Error('indexeddb-read'));
  });
  const run = async () => {
    mark('storage');
    const localKey = 'asterinas-quality-local';
    const sessionKey = 'asterinas-quality-session';
    if (search) {
      checks.localStorage = localStorage.getItem(localKey) === 'home';
      checks.sessionStorage = sessionStorage.getItem(sessionKey) === 'home';
      checks.cookie = document.cookie.split('; ').includes('asterinas_quality=home');
    } else {
      localStorage.setItem(localKey, 'home');
      sessionStorage.setItem(sessionKey, 'home');
      document.cookie = 'asterinas_quality=home; SameSite=Strict; Path=/browser-quality/';
      checks.localStorage = localStorage.getItem(localKey) === 'home';
      checks.sessionStorage = sessionStorage.getItem(sessionKey) === 'home';
      checks.cookie = document.cookie.split('; ').includes('asterinas_quality=home');
    }

    // Do not synchronously create/read a canvas in the readiness document:
    // headless QEMU guests without a compositor can block that call and starve
    // Marionette itself.  The API surface is checked here; pixel rendering is
    // covered by the separate screenshot/render gate.
    checks.canvas = typeof HTMLCanvasElement === 'function' &&
      typeof HTMLCanvasElement.prototype.getContext === 'function';

    mark('wasm');
    try {
      checks.wasm = typeof WebAssembly !== 'undefined' &&
        (await WebAssembly.instantiate(new Uint8Array([
          0,97,115,109,1,0,0,0,1,5,1,96,0,1,127,3,2,1,0,
          7,10,1,6,97,110,115,119,101,114,0,0,10,6,1,4,0,65,42,11
        ]))).instance.exports.answer() === 42;
    } catch (error) {
      checks.wasm = false;
      wasmFailure = String(error && error.message || error).slice(0, 96);
    }

    mark('worker');
    try {
      checks.worker = await failAfter(new Promise((resolve, reject) => {
        const source = 'onmessage=e=>postMessage(e.data*2)';
        const url = URL.createObjectURL(new Blob([source], {type: 'text/javascript'}));
        const worker = new Worker(url);
        worker.onmessage = event => { worker.terminate(); URL.revokeObjectURL(url); resolve(event.data === 42); };
        worker.onerror = event => { worker.terminate(); URL.revokeObjectURL(url); reject(new Error(event.message)); };
        worker.postMessage(21);
      }), 'worker');
    } catch (_) {
      checks.worker = false;
    }

    mark('indexeddb');
    try {
      const database = await failAfter(openDatabase(), 'indexeddb');
      if (search) {
        checks.indexedDb = (await failAfter(readDatabase(database, 'marker'), 'indexeddb-read')) === 'home';
      } else {
        const transaction = database.transaction('quality', 'readwrite');
        transaction.objectStore('quality').put('home', 'marker');
        await failAfter(transactionDone(transaction), 'indexeddb-write');
        checks.indexedDb = (await failAfter(readDatabase(database, 'marker'), 'indexeddb-read')) === 'home';
      }
      database.close();
    } catch (_) {
      checks.indexedDb = false;
    }

    mark('audio');
    try {
      const audio = document.querySelector('#quality-audio');
      checks.audio = await failAfter(new Promise((resolve, reject) => {
        audio.onloadedmetadata = () => resolve(audio.duration > 0.2 && audio.duration < 0.3);
        audio.onerror = () => reject(new Error('audio-decode'));
        audio.src = '/browser-quality/tone.wav';
        audio.load();
      }), 'audio');
    } catch (_) {
      checks.audio = false;
    }

    // Keep network I/O last.  On the guest, a stalled fetch can prevent the
    // promise continuation (and therefore the readiness probe) from running;
    // all local API checks above remain observable in the published report.
    mark('fetch');
    try {
      const api = await failAfter(fetch('/browser-quality/capabilities.json', {cache: 'no-store'}), 'fetch');
      const payload = await failAfter(api.json(), 'fetch-json');
      checks.fetch = api.ok && payload.schema_version === 1 && payload.token === 'asterinas-browser-quality';
    } catch (_) {
      checks.fetch = false;
    }

    mark('complete');
    if (!Object.values(checks).every(value => value === true)) {
      throw new Error(wasmFailure && checks.wasm === false ? 'wasm:' + wasmFailure : 'false-capability');
    }
    window.__asterinasCapabilities.state = 'complete';
    publish();
  };
  // Let the navigation/readiness probe observe a responsive DOM before
  // starting optional capability work.  Some guest implementations service
  // storage/media on the main thread; launching it during HTML parsing can
  // otherwise starve the first Marionette ExecuteScript command.
  setTimeout(() => run().catch(error => {
    window.__asterinasCapabilities.state = 'error';
    const failed = Object.entries(window.__asterinasCapabilities.checks)
      .filter(([, value]) => value !== true).map(([name]) => name).join(',');
    window.__asterinasCapabilities.error = (
      String(error && error.message || error) + (failed ? ':' + failed : '')
    ).slice(0, 160);
    publish();
  }), 500);
})();
</script>"""
BROWSER_SECOND = b"""<!doctype html>
<meta charset=utf-8><title>Second - Asterinas Browser Quality</title>
<h1>Second page / \xe7\xac\xac\xe4\xba\x8c\xe9\xa1\xb5</h1>
<a href=/browser-quality/index.html>First page</a>"""
BROWSER_SEARCH = BROWSER_INDEX.replace(
    b"<title>Asterinas Browser Quality</title>",
    b"<title>asterinas - Asterinas Browser Quality</title>",
)
BROWSER_PERF = b"""<!doctype html>
<html lang=en><meta charset=utf-8><title>Asterinas browser timing</title>
<meta http-equiv=Content-Security-Policy content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'">
<style>body{font:24px sans-serif;margin:48px}#timing-pointer{width:400px;height:240px;background:#1b84a1}
#timing-token{padding:18px;background:#14bb9c}.scroll-space{height:1600px}</style>
<h1>Keyboard / pointer / scroll timing</h1>
<label>Input <input id=timing-input autocomplete=off></label>
<div id=timing-pointer>Move pointer here</div>
<output id=timing-token>0</output>
<a href=/browser-quality/perf-second.html>Local second page</a>
<div class=scroll-space>Scroll this page</div>
<script>
(() => {
  'use strict';
  const token = document.querySelector('#timing-token');
  const samples = [];
  const accepted = Object.create(null);
  let counter = 0;
  let lastPointerMs = -200;
  let lastScrollMs = -200;
  const record = (kind, source, done = () => {}) => {
    const key = source + '-' + kind;
    if ((accepted[key] || 0) >= 64) { done(); return; }
    accepted[key] = (accepted[key] || 0) + 1;
    const startMs = performance.now();
    token.textContent = String(++counter);
    requestAnimationFrame(() => {
      const firstRafMs = performance.now() - startMs;
      requestAnimationFrame(() => {
        const nextRafMs = performance.now() - startMs;
        if (Number.isFinite(firstRafMs) && Number.isFinite(nextRafMs) &&
            firstRafMs >= 0 && nextRafMs >= firstRafMs && nextRafMs <= 60000) {
          samples.push({kind, source, firstRafMs, nextRafMs});
        }
        done();
      });
    });
  };
  const snapshot = () => ({schemaVersion: 1, clockDomain: 'browser-performance-now',
                           samples: samples.slice()});
  document.querySelector('#timing-input').addEventListener('input', event => {
    if (event.isTrusted) record('keyboard', 'trusted');
  });
  document.querySelector('#timing-pointer').addEventListener('pointermove', event => {
    const now = performance.now();
    if (event.isTrusted && now - lastPointerMs >= 200) {
      lastPointerMs = now;
      record('pointer', 'trusted');
    }
  });
  document.addEventListener('scroll', event => {
    const now = performance.now();
    if (event.isTrusted && now - lastScrollMs >= 200) {
      lastScrollMs = now;
      record('scroll', 'trusted');
    }
  }, {passive: true});
  window.__asterinasTimingSnapshot = snapshot;
  window.__asterinasRunSyntheticTiming = async (repetitions = 8) => {
    if (!Number.isInteger(repetitions) || repetitions < 1 || repetitions > 16) {
      throw new Error('synthetic timing repetition bound');
    }
    for (let index = 0; index < repetitions; index++) {
      for (const kind of ['keyboard', 'pointer', 'scroll']) {
        await new Promise(resolve => record(kind, 'synthetic', resolve));
      }
    }
    return snapshot();
  };
})();
</script>"""
BROWSER_PERF_SECOND = b"""<!doctype html>
<html lang=en><meta charset=utf-8><title>Asterinas local navigation timing</title>
<meta http-equiv=Content-Security-Policy content="default-src 'none'; script-src 'unsafe-inline'">
<h1>Local navigation complete</h1>
<a href=/browser-quality/perf.html>Timing first page</a>
<script>
window.__asterinasNavigationSnapshot = () => {
  const entry = performance.getEntriesByType('navigation')[0];
  if (!entry) return null;
  return {schemaVersion: 1, clockDomain: 'browser-navigation',
          startTime: entry.startTime, fetchStart: entry.fetchStart,
          responseStart: entry.responseStart, responseEnd: entry.responseEnd,
          domContentLoadedEventEnd: entry.domContentLoadedEventEnd,
          loadEventEnd: entry.loadEventEnd};
};
</script>"""
BROWSER_WORKLOAD = b"""<!doctype html>
<html lang=en><meta charset=utf-8><title>Asterinas composite browser workload</title>
<meta http-equiv=Content-Security-Policy content="default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'">
<style>body{font:18px sans-serif;margin:24px}#workload-grid{display:grid;grid-template-columns:repeat(16,1fr)}
.workload-node{min-height:4px}.workload-hot{background:#14bb9c}canvas{border:1px solid #1b84a1}</style>
<h1>Asterinas composite browser workload</h1>
<output id=workload-status>idle</output><div id=workload-grid></div>
<canvas id=workload-canvas width=640 height=360></canvas><div id=workload-contexts></div>
<script>
(() => {
  'use strict';
  const modes = {
    smoke: {scale: 1, nodes: 128, resources: 8, contexts: 2},
    profile: {scale: 4, nodes: 512, resources: 32, contexts: 3},
    stress: {scale: 12, nodes: 1024, resources: 96, contexts: 3}
  };
  const names = ['warmup', 'interaction-layout', 'canvas-image',
                 'concurrent-resources', 'navigation-history',
                 'multi-context', 'cooldown'];
  const state = {schemaVersion: 1, workloadVersion: 1,
    clockDomain: 'browser-performance-now', mode: 'smoke', state: 'running',
    phases: [], error: null};
  let started = false;
  const grid = document.querySelector('#workload-grid');
  const canvas = document.querySelector('#workload-canvas');
  const contexts = document.querySelector('#workload-contexts');
  const status = document.querySelector('#workload-status');
  const emptyMetrics = () => ({operationCount: 0, requestCount: 0,
    contextCount: 0, longFrameCount: 0, frameMs: []});
  const nextFrames = metrics => new Promise(resolve => {
    const start = performance.now();
    requestAnimationFrame(() => requestAnimationFrame(() => {
      const elapsed = performance.now() - start;
      if (Number.isFinite(elapsed) && elapsed >= 0 && elapsed <= 60000 &&
          metrics.frameMs.length < 256) {
        metrics.frameMs.push(elapsed);
        if (elapsed > 50) metrics.longFrameCount++;
      }
      resolve();
    }));
  });
  const query = (mode, phase, sequence, passName) =>
    'mode=' + mode + '&phase=' + phase + '&sequence=' + sequence + '&pass=' + passName;
  const loadFrame = (frame, url) => new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('frame-timeout')), 10000);
    frame.onload = () => { clearTimeout(timer); resolve(); };
    frame.onerror = () => { clearTimeout(timer); reject(new Error('frame-load')); };
    frame.src = url;
  });
  const loadImage = url => new Promise((resolve, reject) => {
    const image = new Image();
    image.onload = () => resolve(image);
    image.onerror = () => reject(new Error('image-load'));
    image.src = url;
  });
  const runPool = async (items, limit, operation) => {
    let cursor = 0;
    const worker = async () => {
      while (cursor < items.length) {
        const item = items[cursor++];
        await operation(item);
      }
    };
    await Promise.all(Array.from({length: Math.min(limit, items.length)}, worker));
  };
  const runPhase = async (name, operation) => {
    const phase = {name, state: 'running', startMs: performance.now(),
                   endMs: null, metrics: emptyMetrics()};
    state.phases.push(phase);
    status.textContent = name;
    try {
      await operation(phase.metrics);
      phase.endMs = performance.now();
      phase.state = 'complete';
    } catch (_) {
      phase.endMs = performance.now();
      phase.state = 'failed';
      throw new Error(name + '-failed');
    }
  };
  const warmup = async (metrics, config) => {
    const fragment = document.createDocumentFragment();
    for (let index = 0; index < config.nodes; index++) {
      const node = document.createElement('span');
      node.className = 'workload-node';
      node.textContent = String(index & 15);
      fragment.appendChild(node);
    }
    grid.replaceChildren(fragment);
    metrics.operationCount = config.nodes;
    const response = await fetch('/browser-quality/workload-resource.bin?' +
      query(state.mode, 'resource', 0, 'cold'), {cache: 'no-store'});
    if (!response.ok || (await response.arrayBuffer()).byteLength !== 65536)
      throw new Error('warmup-resource');
    metrics.requestCount = 1;
    await nextFrames(metrics);
  };
  const interactionLayout = async (metrics, config) => {
    const nodes = Array.from(grid.children);
    const rounds = config.scale * 8;
    for (let round = 0; round < rounds; round++) {
      for (let index = round & 1; index < nodes.length; index += 2) {
        nodes[index].classList.toggle('workload-hot');
        nodes[index].textContent = String((round + index) & 31);
      }
      void grid.offsetHeight;
      grid.scrollTop = round & 1 ? grid.scrollHeight : 0;
      metrics.operationCount += nodes.length / 2 + 2;
      await nextFrames(metrics);
    }
  };
  const canvasImage = async (metrics, config) => {
    const count = config.scale * 4;
    const images = [];
    for (let index = 0; index < count; index++) {
      images.push(await loadImage('/browser-quality/workload-image.png?' +
        query(state.mode, 'image', index, 'cold')));
      metrics.requestCount++;
    }
    const context = canvas.getContext('2d');
    if (!context) throw new Error('canvas-context');
    for (let index = 0; index < config.scale * 100; index++) {
      const image = images[index % images.length];
      context.fillStyle = index & 1 ? '#1b84a1' : '#14bb9c';
      context.fillRect((index * 17) % 608, (index * 29) % 328, 32, 32);
      context.drawImage(image, (index * 31) % 608, (index * 13) % 328);
      metrics.operationCount += 2;
    }
    await nextFrames(metrics);
  };
  const concurrentResources = async (metrics, config) => {
    const sequences = Array.from({length: config.resources}, (_, index) => index);
    for (const passName of ['cold', 'warm']) {
      await runPool(sequences, 8, async sequence => {
        const response = await fetch('/browser-quality/workload-resource.bin?' +
          query(state.mode, 'resource', sequence, passName),
          {cache: passName === 'cold' ? 'no-store' : 'default'});
        if (!response.ok || (await response.arrayBuffer()).byteLength !== 65536)
          throw new Error('resource-response');
        metrics.requestCount++;
        metrics.operationCount++;
      });
    }
  };
  const navigationHistory = async (metrics, config) => {
    const frame = document.createElement('iframe');
    contexts.appendChild(frame);
    metrics.contextCount = 1;
    const urls = ['/browser-quality/second.html',
                  '/browser-quality/perf-second.html'];
    for (let index = 0; index < config.scale * 4; index++) {
      await loadFrame(frame, urls[index & 1]);
      metrics.requestCount++;
      metrics.operationCount++;
    }
    frame.remove();
    metrics.contextCount = 0;
  };
  const multiContext = async (metrics, config) => {
    const frames = [];
    for (let index = 0; index < config.contexts; index++) {
      const frame = document.createElement('iframe');
      contexts.appendChild(frame);
      frames.push(frame);
      await loadFrame(frame, '/browser-quality/second.html');
      metrics.contextCount++;
      const response = await fetch('/browser-quality/workload-resource.bin?' +
        query(state.mode, 'context', index, 'cold'), {cache: 'no-store'});
      if (!response.ok) throw new Error('context-resource');
      await response.arrayBuffer();
      metrics.requestCount++;
      metrics.operationCount++;
      await new Promise(resolve => setTimeout(resolve, config.scale));
    }
    await nextFrames(metrics);
    for (const frame of frames) frame.remove();
    metrics.contextCount = 0;
  };
  const cooldown = async metrics => {
    contexts.replaceChildren();
    grid.scrollTop = 0;
    metrics.operationCount = 2;
    await nextFrames(metrics);
  };
  const run = async config => {
    const operations = [
      metrics => warmup(metrics, config),
      metrics => interactionLayout(metrics, config),
      metrics => canvasImage(metrics, config),
      metrics => concurrentResources(metrics, config),
      metrics => navigationHistory(metrics, config),
      metrics => multiContext(metrics, config),
      metrics => cooldown(metrics, config)
    ];
    try {
      for (let index = 0; index < names.length; index++)
        await runPhase(names[index], operations[index]);
      state.state = 'complete';
      status.textContent = 'complete';
    } catch (error) {
      state.state = 'failed';
      state.error = String(error && error.message || 'workload-failed')
        .toLowerCase().replace(/[^a-z0-9-]/g, '-').slice(0, 96) || 'workload-failed';
      contexts.replaceChildren();
      status.textContent = 'failed';
    }
  };
  window.__asterinasStartCompositeWorkload = mode => {
    if (started || !Object.prototype.hasOwnProperty.call(modes, mode))
      throw new Error('workload-mode');
    started = true;
    state.mode = mode;
    void run(modes[mode]);
  };
  window.__asterinasCompositeWorkloadSnapshot = () =>
    JSON.parse(JSON.stringify(state));
})();
</script>"""


def browser_resource(path: str) -> tuple[str, bytes] | None:
    """Return one deterministic browser resource for an exact path."""

    return {
        BROWSER_INDEX_PATH: ("text/html; charset=utf-8", BROWSER_INDEX),
        BROWSER_SECOND_PATH: ("text/html; charset=utf-8", BROWSER_SECOND),
        BROWSER_PERF_PATH: ("text/html; charset=utf-8", BROWSER_PERF),
        BROWSER_PERF_SECOND_PATH: ("text/html; charset=utf-8", BROWSER_PERF_SECOND),
        BROWSER_WORKLOAD_PATH: ("text/html; charset=utf-8", BROWSER_WORKLOAD),
        BROWSER_IMAGE_PATH: ("image/png", BROWSER_IMAGE),
        BROWSER_DOWNLOAD_PATH: ("application/octet-stream", BROWSER_DOWNLOAD),
        BROWSER_API_PATH: ("application/json", BROWSER_API),
        BROWSER_AUDIO_PATH: ("audio/wav", BROWSER_AUDIO),
    }.get(path)


_WORKLOAD_QUERY = re.compile(
    r"mode=(smoke|profile|stress)&"
    r"phase=(image|resource|context)&"
    r"sequence=(0|[1-9][0-9]{0,2})&"
    r"pass=(cold|warm)\Z"
)


def _parse_workload_query(query: str) -> tuple[str, str, int, str] | None:
    match = _WORKLOAD_QUERY.fullmatch(query)
    if match is None:
        return None
    mode, phase, sequence_raw, pass_name = match.groups()
    sequence = int(sequence_raw)
    if sequence >= 256:
        return None
    return mode, phase, sequence, pass_name


def is_successful_summary(
    summary: Mapping[str, object], *, expected_requests: int
) -> bool:
    """Require an exact set of successful fixed-payload requests."""

    if (
        isinstance(expected_requests, bool)
        or not isinstance(expected_requests, int)
        or not 0 < expected_requests <= MAX_REQUEST_RECORDS
    ):
        return False
    requests = summary.get("requests")
    if not isinstance(requests, list) or len(requests) != expected_requests:
        return False
    if (
        summary.get("schema_version") != 1
        or summary.get("payload_path") != FIXTURE_PATH
        or summary.get("payload_sha256") != PAYLOAD_SHA256
        or summary.get("payload_size") != PAYLOAD_SIZE
        or summary.get("request_count") != expected_requests
        or summary.get("records_truncated") is not False
    ):
        return False
    return all(
        isinstance(record, dict)
        and record.get("body_bytes") == PAYLOAD_SIZE
        and record.get("path") == FIXTURE_PATH
        and record.get("status") == 200
        for record in requests
    )


@dataclass(frozen=True)
class FixtureConfig:
    """The exact local listener and optional peer restriction."""

    bind_address: str = "127.0.0.1"
    port: int = 17894
    allowed_peer: str | None = None

    def __post_init__(self) -> None:
        _validate_ipv4(self.bind_address, "bind address")
        if isinstance(self.port, bool) or not isinstance(self.port, int):
            raise ValueError("port must be an integer between 0 and 65535")
        if not 0 <= self.port <= 65535:
            raise ValueError("port must be an integer between 0 and 65535")
        if self.allowed_peer is not None:
            _validate_ipv4(self.allowed_peer, "allowed peer")


def _validate_ipv4(value: str, name: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an IPv4 address")
    try:
        parsed = ipaddress.ip_address(value)
    except ValueError as error:
        raise ValueError(f"{name} must be an IPv4 address") from error
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError(f"{name} must be a canonical IPv4 address")


class FixtureServer:
    """One explicitly owned ThreadingHTTPServer with bounded evidence."""

    def __init__(self, config: FixtureConfig) -> None:
        self.config = config
        self._server: http.server.ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._request_count = 0
        self._records: list[dict[str, object]] = []
        self._last_timestamp = 0
        self._workload_request_count = 0
        self._workload_records: list[dict[str, object]] = []
        self._workload_active = 0
        self._workload_max_active = 0
        self._capture: bytes | None = None
        self._capture_evidence: dict[str, object] | None = None

    def __enter__(self) -> FixtureServer:
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        del exc_info
        self.close()

    @property
    def address(self) -> str:
        server = self._require_server()
        return str(server.server_address[0])

    @property
    def port(self) -> int:
        server = self._require_server()
        return int(server.server_address[1])

    @property
    def endpoint(self) -> str:
        return f"http://{self.address}:{self.port}{FIXTURE_PATH}"

    @property
    def running(self) -> bool:
        return self._server is not None and self._thread is not None

    @property
    def thread(self) -> threading.Thread:
        if self._thread is None:
            raise RuntimeError("fixture server is not running")
        return self._thread

    def _require_server(self) -> http.server.ThreadingHTTPServer:
        if self._server is None:
            raise RuntimeError("fixture server is not running")
        return self._server

    def start(self) -> FixtureServer:
        if self._server is not None:
            return self
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:
                owner._handle_get(self)

            def do_POST(self) -> None:
                owner._handle_post(self)

            def log_message(self, format: str, *args: object) -> None:
                del format, args

        server = http.server.ThreadingHTTPServer(
            (self.config.bind_address, self.config.port), Handler
        )
        server.daemon_threads = True
        thread = threading.Thread(
            target=server.serve_forever,
            name="megrez-network-fixture",
        )
        self._server = server
        self._thread = thread
        thread.start()
        return self

    def close(self) -> None:
        server = self._server
        thread = self._thread
        if server is None:
            return
        self._server = None
        self._thread = None
        server.shutdown()
        server.server_close()
        if thread is not None:
            thread.join(timeout=2)
            if thread.is_alive():
                raise RuntimeError("fixture server thread did not stop")

    def _handle_get(self, request: http.server.BaseHTTPRequestHandler) -> None:
        peer = request.client_address[0]
        target = urlsplit(request.path)
        is_browser_request = target.path.startswith("/browser-quality/")
        is_workload_request = target.path in {
            BROWSER_WORKLOAD_RESOURCE_PATH,
            BROWSER_WORKLOAD_IMAGE_PATH,
        }
        workload_start_ns = 0
        workload_active = 0
        if is_workload_request:
            workload_start_ns, workload_active = self._begin_workload_request()
        if self.config.allowed_peer is not None and peer != self.config.allowed_peer:
            status = 403
            body = b""
            content_type = "application/octet-stream"
        elif is_browser_request:
            status, content_type, body = self._browser_response(
                target.path, target.query
            )
        elif request.path != FIXTURE_PATH:
            status = 404
            body = b""
            content_type = "application/octet-stream"
        else:
            status = 200
            body = PAYLOAD
            content_type = "application/octet-stream"

        # Publish legacy request evidence before the fixed-length response is
        # visible to the client.  Otherwise a client can finish reading the
        # body and ask for the summary during the tiny interval before the
        # handler's finally block records it.
        if not is_browser_request:
            self._record(peer, request.path, status, len(body))
        try:
            self._send_response(request, status, body, content_type)
            request.wfile.write(body)
        finally:
            if is_workload_request:
                self._finish_workload_request(
                    target.query,
                    status,
                    len(body),
                    workload_start_ns,
                    workload_active,
                )

    def _browser_response(self, path: str, query: str) -> tuple[int, str, bytes]:
        if path == BROWSER_INDEX_PATH:
            if not query:
                return 200, "text/html; charset=utf-8", BROWSER_INDEX
            if query == "q=asterinas":
                return 200, "text/html; charset=utf-8", BROWSER_SEARCH
            return 400, "text/plain; charset=utf-8", b""
        if query:
            parsed = _parse_workload_query(query)
            if path not in {
                BROWSER_WORKLOAD_RESOURCE_PATH,
                BROWSER_WORKLOAD_IMAGE_PATH,
            } or parsed is None:
                return 400, "text/plain; charset=utf-8", b""
            if path == BROWSER_WORKLOAD_IMAGE_PATH:
                return 200, "image/png", BROWSER_IMAGE
            return 200, "application/octet-stream", WORKLOAD_RESOURCE
        if path in {BROWSER_WORKLOAD_RESOURCE_PATH, BROWSER_WORKLOAD_IMAGE_PATH}:
            return 400, "text/plain; charset=utf-8", b""
        resource = browser_resource(path)
        if resource is None:
            return 404, "text/plain; charset=utf-8", b""
        content_type, body = resource
        return 200, content_type, body

    def _handle_post(self, request: http.server.BaseHTTPRequestHandler) -> None:
        peer = request.client_address[0]
        target = urlsplit(request.path)
        if self.config.allowed_peer is not None and peer != self.config.allowed_peer:
            self._send_response(request, 403)
            return
        if (
            target.path not in (BROWSER_CAPTURE_PATH, BROWSER_PNG_CAPTURE_PATH)
            or target.query
        ):
            self._send_response(request, 404)
            return
        if request.headers.get("Transfer-Encoding") is not None:
            self._send_response(request, 400)
            return
        lengths = request.headers.get_all("Content-Length", failobj=[])
        if not lengths:
            self._send_response(request, 411)
            return
        if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdecimal():
            self._send_response(request, 400)
            return
        size = int(lengths[0])
        if size == 0:
            self._send_response(request, 400)
            return
        if size > MAX_CAPTURE_BYTES:
            self._send_response(request, 413)
            return
        request.connection.settimeout(1.0)
        try:
            payload = request.rfile.read(size)
        except OSError:
            self._send_response(request, 400)
            return
        if len(payload) != size:
            self._send_response(request, 400)
            return
        with self._lock:
            if self._capture is not None:
                status = 409
            else:
                self._capture = bytes(payload)
                self._capture_evidence = {
                    "bytes": size,
                    "path": target.path,
                    "peer": peer,
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
                status = 201
        self._send_response(request, status)

    def _send_response(
        self,
        request: http.server.BaseHTTPRequestHandler,
        status: int,
        body: bytes = b"",
        content_type: str = "application/octet-stream",
    ) -> None:
        request.send_response(status)
        request.send_header("Content-Length", str(len(body)))
        request.send_header("Content-Type", content_type)
        request.send_header("Cache-Control", "no-store")
        request.send_header("Connection", "close")
        request.end_headers()
        request.close_connection = True

    def _begin_workload_request(self) -> tuple[int, int]:
        with self._lock:
            self._workload_active += 1
            self._workload_max_active = max(
                self._workload_max_active, self._workload_active
            )
            return time.monotonic_ns(), self._workload_active

    def _finish_workload_request(
        self,
        query: str,
        status: int,
        body_bytes: int,
        start_ns: int,
        active_at_start: int,
    ) -> None:
        parsed = _parse_workload_query(query)
        mode, phase, sequence, pass_name = (
            parsed if parsed is not None else ("invalid", "invalid", -1, "invalid")
        )
        with self._lock:
            self._workload_active -= 1
            self._workload_request_count += 1
            if len(self._workload_records) < MAX_WORKLOAD_REQUEST_RECORDS:
                self._workload_records.append(
                    {
                        "active_at_start": active_at_start,
                        "body_bytes": body_bytes,
                        "mode": mode,
                        "monotonic_end_ns": max(time.monotonic_ns(), start_ns),
                        "monotonic_start_ns": start_ns,
                        "pass": pass_name,
                        "phase": phase,
                        "sequence": sequence,
                        "status": status,
                    }
                )

    def capture_payload(self) -> bytes | None:
        """Return the immutable accepted capture, if one exists."""

        with self._lock:
            return self._capture

    def capture_summary(self) -> dict[str, object] | None:
        """Return detached evidence for the accepted capture."""

        with self._lock:
            if self._capture_evidence is None:
                return None
            return dict(self._capture_evidence)

    def _record(self, peer: str, path: str, status: int, body_bytes: int) -> None:
        with self._lock:
            self._request_count += 1
            now = max(time.monotonic_ns(), self._last_timestamp + 1)
            self._last_timestamp = now
            if len(self._records) < MAX_REQUEST_RECORDS:
                self._records.append(
                    {
                        "body_bytes": body_bytes,
                        "monotonic_ns": now,
                        "path": path,
                        "peer": peer,
                        "status": status,
                    }
                )

    def summary(self) -> dict[str, object]:
        """Return a detached canonical-schema snapshot of bounded evidence."""

        with self._lock:
            records = [dict(record) for record in self._records]
            request_count = self._request_count
            workload_records = [dict(record) for record in self._workload_records]
            workload_request_count = self._workload_request_count
            workload_max_active = self._workload_max_active
        return {
            "payload_path": FIXTURE_PATH,
            "payload_sha256": PAYLOAD_SHA256,
            "payload_size": PAYLOAD_SIZE,
            "records_truncated": request_count > len(records),
            "request_count": request_count,
            "requests": records,
            "schema_version": 1,
            "workload_max_active": workload_max_active,
            "workload_records_truncated": workload_request_count
            > len(workload_records),
            "workload_request_count": workload_request_count,
            "workload_requests": workload_records,
        }

    def summary_json(self) -> bytes:
        return (
            json.dumps(self.summary(), sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()


def _ipv4_argument(value: str) -> str:
    try:
        _validate_ipv4(value, "address")
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error
    return value


def _port_argument(value: str) -> int:
    try:
        port = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("port must be between 0 and 65535") from error
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 0 and 65535")
    return port


def _parse_args(arguments: Sequence[str] | None = None) -> FixtureConfig:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind-address", type=_ipv4_argument, default="127.0.0.1")
    parser.add_argument("--port", type=_port_argument, default=17894)
    parser.add_argument("--allow-peer", type=_ipv4_argument)
    values = parser.parse_args(arguments)
    return FixtureConfig(values.bind_address, values.port, values.allow_peer)


def main(arguments: Sequence[str] | None = None) -> int:
    config = _parse_args(arguments)
    stop = threading.Event()
    previous: dict[int, signal.Handlers] = {}

    def request_stop(signum: int, frame: object) -> None:
        del signum, frame
        stop.set()

    for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.getsignal(signum)
        signal.signal(signum, request_stop)
    try:
        with FixtureServer(config) as server:
            print(server.endpoint, flush=True)
            stop.wait()
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
