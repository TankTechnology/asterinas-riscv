# SPDX-License-Identifier: MPL-2.0

"""Serve one fixed VP8 clip for a short Firefox YUV sampling A/B/A.

The caller supplies the 300-frame clip and its SHA-256. Each run ID is
write-once, so an interrupted measurement cannot silently replace evidence.
"""

import argparse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import parse_qs, urlparse


RUN = re.compile(r"[a-z0-9]{1,20}")
SIZES = {"large": (1280, 720), "small": (640, 360)}
FILTERS = {"auto": "auto", "crisp": "crisp-edges"}

HTML = """<!doctype html><meta charset="utf-8"><title>Asterinas 300-frame VP8 probe</title>
<style>body{margin:0;background:#111;color:white;font:20px sans-serif}#status{padding:12px}</style>
<video id="clip" muted playsinline autoplay width="WIDTH" height="HEIGHT" style="image-rendering: FILTER" src="/clip720.webm"></video><div id="status">Preparing</div>
<script>
(() => {
  const video = document.getElementById('clip');
  const run = 'RUNID';
  let playingAt = null;
  let done = false;
  const finish = async (state, reason='') => {
    if (done) return; done = true;
    const q = video.getVideoPlaybackQuality ? video.getVideoPlaybackQuality() : null;
    const result = {run, state, reason, displayWidth:WIDTH, displayHeight:HEIGHT,
      computedSampling:getComputedStyle(video).imageRendering,
      sourceWidth:video.videoWidth, sourceHeight:video.videoHeight,
      duration:video.duration, currentTime:video.currentTime,
      elapsedWallMs:playingAt === null ? null : performance.now()-playingAt,
      totalVideoFrames:q ? q.totalVideoFrames : null,
      droppedVideoFrames:q ? q.droppedVideoFrames : null,
      corruptedVideoFrames:q ? q.corruptedVideoFrames : null,
      mozDecodedFrames:video.mozDecodedFrames ?? null,
      mozParsedFrames:video.mozParsedFrames ?? null,
      mozPresentedFrames:video.mozPresentedFrames ?? null,
      mozPaintedFrames:video.mozPaintedFrames ?? null,
      readyState:video.readyState, networkState:video.networkState,
      userAgent:navigator.userAgent};
    document.getElementById('status').textContent = JSON.stringify(result);
    await fetch('/metrics', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(result), keepalive:true});
  };
  video.addEventListener('playing', () => { if (playingAt === null) playingAt=performance.now(); });
  video.addEventListener('ended', () => finish('ended'));
  video.addEventListener('error', () => finish('error', String(video.error?.code ?? 'unknown')));
  setTimeout(() => finish('timeout'), 30000);
  video.play().catch(error => finish('play-error', String(error)));
})();
</script>"""


def render_page(run: str, size: str, sampling: str) -> bytes:
    if RUN.fullmatch(run) is None or size not in SIZES or sampling not in FILTERS:
        raise ValueError("invalid video probe variant")
    width, height = SIZES[size]
    return (
        HTML.replace("RUNID", run)
        .replace("WIDTH", str(width))
        .replace("HEIGHT", str(height))
        .replace("FILTER", FILTERS[sampling])
        .encode()
    )


def make_handler(root: Path):
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(root), **kwargs)

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path != "/probe":
                return super().do_GET()
            values = parse_qs(parsed.query)
            if any(len(values.get(key, ())) != 1 for key in ("run", "size", "filter")):
                self.send_error(400)
                return
            try:
                body = render_page(
                    values["run"][0], values["size"][0], values["filter"][0]
                )
            except ValueError:
                self.send_error(400)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if self.path != "/metrics":
                self.send_error(404)
                return
            try:
                size = int(self.headers.get("Content-Length", "-1"))
                if not 0 < size <= 16384:
                    raise ValueError("invalid payload size")
                body = self.rfile.read(size)
                result = json.loads(body)
                run = result["run"]
                if not isinstance(run, str) or RUN.fullmatch(run) is None:
                    raise ValueError("invalid run ID")
                path = root / f"metrics-{run}.json"
                with path.open("x") as stream:
                    json.dump(result, stream, sort_keys=True)
                    stream.write("\n")
            except (ValueError, KeyError, TypeError):
                self.send_error(400)
                return
            except FileExistsError:
                self.send_error(409)
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok\n")

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--clip-sha256", required=True)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=17897)
    args = parser.parse_args()
    clip = args.root / "clip720.webm"
    if hashlib.sha256(clip.read_bytes()).hexdigest() != args.clip_sha256:
        parser.error("clip SHA-256 mismatch")
    with ThreadingHTTPServer((args.bind, args.port), make_handler(args.root)) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
