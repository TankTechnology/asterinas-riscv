#!/usr/bin/env python3
"""Run every concurrency model in this directory.

Two kinds of models live here:

* loom crates (`*/Cargo.toml`): bounded model checking of a kernel protocol,
  run with `cargo test`. Some tests assert that loom *finds* the bug in the
  pre-fix protocol; that check is part of the crate and needs no special
  handling here.
* TLA+ specs (`*/*.tla` with `*.cfg` configs): run with TLC. A config states
  what its run must produce in a `\\* EXPECT:` comment:

      \\* EXPECT: ok
      \\* EXPECT: violation NoLostImmediate

  A `violation` expectation turns the pre-fix model into a regression test:
  if the bug is ever "fixed" by accident (or the spec drifts away from the
  kernel), the run fails instead of silently passing.

Usage:
    python3 tools/riscv/concurrency-models/run.py
    python3 tools/riscv/concurrency-models/run.py --only pollee-poll
    python3 tools/riscv/concurrency-models/run.py --loom-max-preemptions 6

The TLA+ jar defaults to `target/tla/tla2tools.jar`; see `README.md` for how
to fetch it.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
DEFAULT_JAR = REPO / "target" / "tla" / "tla2tools.jar"

EXPECT_RE = re.compile(r"\\\*\s*EXPECT:\s*(?P<kind>ok|violation)(?:\s+(?P<what>\S+))?")


@dataclass
class Result:
    name: str
    ok: bool
    detail: str = ""
    output: str = field(default="", repr=False)


def run(cmd: list[str], cwd: Path, env: dict[str, str] | None = None,
        timeout: int = 1800) -> subprocess.CompletedProcess[str]:
    print(f"$ (cd {cwd.relative_to(REPO) if cwd.is_relative_to(REPO) else cwd} && {' '.join(cmd)})",
          flush=True)
    proc_env = os.environ.copy()
    if env:
        proc_env.update(env)
    try:
        return subprocess.run(cmd, cwd=cwd, env=proc_env, capture_output=True,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode(errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode(errors="replace")
        return subprocess.CompletedProcess(cmd, 124, stdout, stderr + f"\ntimed out after {timeout}s")


def loom_crates() -> list[Path]:
    return sorted(p.parent for p in HERE.glob("*/Cargo.toml"))


def run_loom(crate: Path, preemptions: str | None) -> Result:
    env = {}
    if preemptions:
        env["LOOM_MAX_PREEMPTIONS"] = preemptions
    proc = run(["cargo", "test", "--quiet"], cwd=crate, env=env, timeout=3600)
    detail = ""
    m = re.search(r"test result: (\w+)\. (\d+) passed; (\d+) failed", proc.stdout)
    if m:
        detail = f"{m.group(2)} passed, {m.group(3)} failed"
    if proc.returncode == 0:
        return Result(crate.name, True, detail, proc.stdout + proc.stderr)
    return Result(crate.name, False, detail or f"exit {proc.returncode}",
                  proc.stdout + proc.stderr)


def spec_for(cfg: Path) -> Path | None:
    """The spec a config refines: the same-stem file, else the module in the
    config's directory (consensus in the TLA+ community is `Module.tla` with
    several `ModuleConfig.cfg` variants)."""
    same_stem = cfg.with_suffix(".tla")
    if same_stem.exists():
        return same_stem
    # TLC writes a counterexample as `<Module>_TTrace_<n>.tla` next to the
    # spec; it is an artifact of the previous run, not a module.
    specs = sorted(p for p in cfg.parent.glob("*.tla")
                   if "_TTrace_" not in p.name)
    if len(specs) == 1:
        return specs[0]
    if specs:
        print(f"warning: {cfg.relative_to(HERE)}: several .tla files, none named "
              f"{same_stem.name}; skipping", file=sys.stderr)
    return None


def tla_configs() -> list[tuple[Path, Path]]:
    pairs = []
    for cfg in sorted(HERE.glob("*/*.cfg")):
        spec = spec_for(cfg)
        if spec is not None:
            pairs.append((spec, cfg))
    return pairs


def expectation(cfg: Path) -> tuple[str, str | None]:
    text = cfg.read_text()
    m = EXPECT_RE.search(text)
    if not m:
        return "ok", None
    return m.group("kind"), m.group("what")


def run_tlc(spec: Path, cfg: Path, jar: Path, workers: int) -> Result:
    kind, what = expectation(cfg)
    name = f"{cfg.parent.name}/{cfg.name}"
    if not jar.exists():
        return Result(name, False, f"missing TLA+ tools: {jar}")
    proc = run(["java", "-XX:+UseParallelGC", "-cp", str(jar), "tlc2.TLC",
                "-workers", str(workers), "-config", cfg.name, spec.name],
               cwd=cfg.parent, timeout=1800)
    out = proc.stdout + proc.stderr

    violation = re.search(r"Invariant (\w+) is violated", out)
    if kind == "ok":
        if proc.returncode != 0 or violation:
            return Result(name, False,
                          f"unexpected failure (exit {proc.returncode})"
                          + (f", {violation.group(1)} violated" if violation else ""), out)
        # Progress lines also contain "distinct states found"; the last one is
        # the final count.
        counts = re.findall(r"([\d,]+) distinct states found", out)
        if counts:
            return Result(name, True, f"{counts[-1].replace(',', '')} states", out)
        return Result(name, True, "no error", out)

    # expected a violation
    if not violation:
        return Result(name, False, "expected a counterexample, TLC found none", out)
    if what and violation.group(1) != what:
        return Result(name, False,
                      f"violated {violation.group(1)}, expected {what}", out)
    trace = re.search(r"The behavior up to this point is:\n(.*?)\n\d+ states generated",
                      out, re.S)
    detail = f"found {violation.group(1)}"
    if trace:
        detail += "; counterexample captured"
    return Result(name, True, detail, out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", action="append", default=[],
                        help="run only the crate/spec directory with this name (repeatable)")
    parser.add_argument("--loom-max-preemptions", default=None,
                        help="LOOM_MAX_PREEMPTIONS for the loom crates")
    parser.add_argument("--tla-jar", type=Path, default=DEFAULT_JAR)
    parser.add_argument("--tlc-workers", type=int, default=4)
    parser.add_argument("--verbose", action="store_true",
                        help="dump the full output of failing runs")
    args = parser.parse_args()

    selected = set(args.only)

    results: list[Result] = []
    crates = [c for c in loom_crates() if not selected or c.name in selected]
    for crate in crates:
        results.append(run_loom(crate, args.loom_max_preemptions))

    pairs = [(s, c) for (s, c) in tla_configs()
             if not selected or c.parent.name in selected]
    for spec, cfg in pairs:
        results.append(run_tlc(spec, cfg, args.tla_jar, args.tlc_workers))

    if not results:
        print("no models selected", file=sys.stderr)
        return 2

    print()
    width = max(len(r.name) for r in results)
    failed = 0
    for r in results:
        status = "PASS" if r.ok else "FAIL"
        print(f"{r.name:<{width}}  {status}  {r.detail}")
        if not r.ok:
            failed += 1
            if args.verbose:
                print(r.output, flush=True)
    print(f"\n{len(results) - failed}/{len(results)} models OK")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
