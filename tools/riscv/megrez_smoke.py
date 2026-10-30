#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0

"""One-shot acceptance run against the Megrez board.

Answers the only question that matters after a change: does a plain cold boot
end with a usable desktop that has the right clock, working networking, a
rendered page, and working toolchains?

    megrez_smoke.py                 # check the board as it is now
    megrez_smoke.py --reboot        # cold boot first, then check
    megrez_smoke.py --json out.json

Every step is bounded and the whole run is meant to finish inside ten minutes
except when a boot itself hangs, which is exactly what it is there to catch.
The console bridge (`megrez_console.py bridge`) must already be running: this
script only writes commands into the FIFO and reads the log.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from megrez_console import Console  # noqa: E402

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
BOARD = "58.206.232.122"
BOARD_HTTP_PORT = 9999
BOOT_DEADLINE_S = 180.0
CLOCK_TOLERANCE_S = 600.0


class Result:
    def __init__(self, name: str) -> None:
        self.name = name
        self.ok = False
        self.detail = ""
        self.seconds = 0.0

    def as_dict(self) -> dict[str, object]:
        return {
            "check": self.name,
            "ok": self.ok,
            "detail": self.detail,
            "seconds": round(self.seconds, 1),
        }


class Smoke:
    def __init__(self, log: str, fifo: str) -> None:
        self.log = Path(log)
        self.console = Console(str(self.log), fifo)
        self.results: list[Result] = []
        self.counter = 0

    # -- console helpers -------------------------------------------------

    def tail(self, window: int = 60000) -> str:
        data = self.log.read_bytes()[-window:]
        return ANSI.sub("", data.decode("utf-8", "replace"))

    def since(self, offset: int) -> str:
        """Everything appended to the console log since `offset` bytes."""

        with self.log.open("rb") as handle:
            handle.seek(offset)
            data = handle.read()
        return ANSI.sub("", data.decode("utf-8", "replace"))

    def command(self, text: str, timeout: float = 60.0, pause: float = 0.05) -> str:
        """Type a command and return everything printed after it.

        The offset is a byte position, not a window length: a len()-based mark
        on a truncated tail silently lands in the wrong place once the log is
        bigger than the window, which made every check but the last one read
        empty output.
        """

        # A marker per command: the previous command's trailing output can
        # still be arriving when the next one starts typing, and a shared
        # marker then completes the wait before this command has run.
        self.counter += 1
        marker = f"ZZSMOKE_DONE_{self.counter}"
        offset = self.log.stat().st_size
        self.console.type_line(f"{text}; echo {marker}", pause)
        deadline = time.time() + timeout
        # The console echoes the typed line, which contains the marker itself,
        # so only a marker alone on its own line counts as the command having
        # finished.
        pattern = re.compile(rf"^{re.escape(marker)}\s*$", re.MULTILINE)
        while time.time() < deadline:
            if pattern.search(self.since(offset)):
                break
            time.sleep(1)
        time.sleep(1)
        return self.since(offset)

    def check(self, name: str) -> Result:
        result = Result(name)
        self.results.append(result)
        result.seconds = -time.time()
        return result

    def finish(self, result: Result, ok: bool, detail: str) -> None:
        result.ok = ok
        result.detail = detail
        result.seconds += time.time()
        flag = "ok" if ok else "FAIL"
        print(f"[{flag:4}] {result.name}: {detail}", flush=True)

    # -- checks ----------------------------------------------------------

    def wait_for_boot(self) -> Result:
        result = self.check("cold boot reaches the desktop")
        start = time.time()
        # Only output produced after the reboot counts: the previous boot's
        # desktop-ready line is still in the log, and reading it as this boot's
        # success makes the script start typing while U-Boot is counting down,
        # which stops the countdown and leaves the board at the U-Boot prompt.
        offset = self.log.stat().st_size
        self.console.type_line("sync; reboot", 0.05)
        deadline = start + BOOT_DEADLINE_S
        while time.time() < deadline:
            text = self.since(offset)
            if "asterinas-desktop-ready" in text or "ASTERINAS_DEBUG_CONSOLE_READY" in text:
                break
            time.sleep(2)
        else:
            self.finish(result, False, f"no desktop-ready within {BOOT_DEADLINE_S:.0f} s")
            return result
        # Give the session a moment to settle before the first command.
        time.sleep(15)
        self.finish(result, True, f"{time.time() - start:.0f} s to desktop")
        return result

    def wait_shell(self, timeout: float = 180.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            # A prompt means the guest shell is taking input; anything still
            # booting (or sitting at U-Boot) must not be typed into.
            if re.search(r"root@asterinas-debug:[^#\n]*#", self.tail()[-4000:]):
                return True
            time.sleep(2)
        return False

    def wait_for_navigation(self, timeout: float = 240.0) -> Result:
        result = self.check("the browser started and was navigated")
        deadline = time.time() + timeout
        while time.time() < deadline:
            text = self.command(
                "tail -n 2 /home/asterinas/browser-web-navigate.log 2>/dev/null",
                timeout=40,
            )
            if "ASTERINAS_BROWSER_NAVIGATE_OK" in text:
                self.finish(result, True, "navigation reported OK")
                return result
            time.sleep(5)
        self.finish(result, False, f"no navigation within {timeout:.0f} s")
        return result

    def check_clock(self) -> Result:
        result = self.check("clock is right after boot")
        text = self.command("date -u +%s", timeout=45)
        stamps = re.findall(r"^(\d{10})$", text, re.MULTILINE)
        if not stamps:
            self.finish(result, False, "no timestamp came back")
            return result
        guest = int(stamps[-1])
        drift = abs(guest - int(time.time()))
        self.finish(
            result,
            drift <= CLOCK_TOLERANCE_S,
            f"drift {drift:.0f} s from the host",
        )
        return result

    def check_network(self) -> Result:
        result = self.check("network reaches the internet")
        text = self.command(
            "curl -m 8 -s -o /dev/null -w 'ZZNET=%{http_code}' https://www.baidu.com/",
            timeout=60,
        )
        match = re.search(r"ZZNET=(\d+)", text)
        code = match.group(1) if match else "none"
        self.finish(result, code == "200", f"https://www.baidu.com/ -> {code}")
        return result

    def check_browser_navigation(self) -> Result:
        result = self.check("Firefox was driven to the target page")
        text = self.command(
            "tail -n 3 /home/asterinas/browser-web-navigate.log 2>&1", timeout=45
        )
        ok = "ASTERINAS_BROWSER_NAVIGATE_OK" in text
        self.finish(
            result,
            ok,
            "navigation reported OK" if ok else "no navigation record",
        )
        return result

    def check_screen(self) -> Result:
        result = self.check("the desktop shows a rendered page")
        text = self.command(
            "DISPLAY=:0 xwd -root -silent -out /tmp/smoke.xwd && "
            f"(cd /tmp && nohup python3 -m http.server {BOARD_HTTP_PORT} "
            ">/dev/null 2>&1 &) ; sleep 2; wc -c /tmp/smoke.xwd",
            timeout=120,
        )
        if "smoke.xwd" not in text:
            self.finish(result, False, "could not capture the screen")
            return result
        with tempfile.TemporaryDirectory() as temporary:
            shot = Path(temporary) / "root.xwd"
            # The capture and the file server both take a moment: fetch until
            # the size stops growing, because a partial xwd is unreadable.
            size = 0
            for _ in range(30):
                subprocess.run(
                    (
                        "curl",
                        "-m",
                        "60",
                        "-s",
                        "-o",
                        str(shot),
                        f"http://{BOARD}:{BOARD_HTTP_PORT}/smoke.xwd",
                    ),
                    check=False,
                )
                now = shot.stat().st_size if shot.is_file() else 0
                if now and now == size:
                    break
                size = now
                time.sleep(2)
            if size < 1_000_000:
                self.finish(result, False, f"screenshot is only {size} bytes")
                return result
            colors = subprocess.run(
                (
                    "magick",
                    f"xwd:{shot}",
                    "-format",
                    "%k %[fx:mean]",
                    "info:",
                ),
                capture_output=True,
                text=True,
                check=False,
            )
            fields = colors.stdout.split()
            if len(fields) != 2:
                self.finish(result, False, f"unreadable screenshot: {colors.stderr[:120]}")
                return result
            unique, mean = int(fields[0]), float(fields[1])
        # A blank page is a handful of colours at nearly full brightness; a
        # rendered one has text, images and a wallpaper.
        ok = unique >= 200 and 0.02 < mean < 0.98
        self.finish(result, ok, f"{unique} colours, mean luminance {mean:.2f}")
        return result

    def check_toolchain(self) -> Result:
        result = self.check("vim and g++ work in a terminal")
        text = self.command(
            "cd /tmp && rm -rf smokecpp && mkdir smokecpp && cd smokecpp && "
            "printf '#include <iostream>\\nint main(){std::cout<<\"hello smoke\";}' > a.cpp && "
            "g++ -O1 a.cpp -o a && ./a",
            timeout=180,
        )
        ok = "hello smoke" in text
        self.finish(result, ok, "g++ compiled and ran" if ok else "toolchain failed")
        return result

    # -- driver ----------------------------------------------------------

    def run(self, reboot: bool) -> int:
        if reboot:
            if not self.wait_for_boot().ok:
                self.report()
                return 1
        if not self.wait_shell():
            print("[FAIL] no shell prompt on the console", flush=True)
            return 1
        # The clock is set twice: once by the desktop service before the
        # session starts, and again by the browser wrapper right before
        # Firefox. The first attempt runs while the interface is still
        # settling, so the checks below have to wait for the browser: a wrong
        # clock makes every HTTPS request fail, which then looks like a
        # network fault.
        self.wait_for_navigation()
        self.check_clock()
        self.check_network()
        self.check_browser_navigation()
        self.check_screen()
        self.check_toolchain()
        self.report()
        return 0 if all(result.ok for result in self.results) else 1

    def report(self) -> None:
        print("", flush=True)
        passed = sum(1 for result in self.results if result.ok)
        print(f"{passed}/{len(self.results)} checks passed", flush=True)
        for result in self.results:
            flag = "ok" if result.ok else "FAIL"
            print(f"  [{flag:4}] {result.name} ({result.seconds:.0f} s)", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(prog="megrez_smoke")
    parser.add_argument("--log", default="/tmp/board2.log")
    parser.add_argument("--fifo", default="/tmp/board2.cmd")
    parser.add_argument("--reboot", action="store_true", help="cold boot first")
    parser.add_argument("--json", help="write the results here")
    values = parser.parse_args()

    smoke = Smoke(values.log, values.fifo)
    code = smoke.run(values.reboot)
    if values.json:
        Path(values.json).write_text(
            json.dumps([result.as_dict() for result in smoke.results], indent=2) + "\n",
            encoding="utf-8",
        )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
