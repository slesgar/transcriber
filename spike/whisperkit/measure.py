#!/usr/bin/env python3
"""Запуск команды под /usr/bin/time -l с опросом CPU% через ps.

Всё в одном процессе: дочерний процесс ждём здесь же, сэмплер — поток,
никаких фоновых шеллов, которые могли бы пережить прогон.

  ./measure.py <metrics.json> -- <cmd> [args...]
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
import time


def sample_cpu(pid: int, stop: threading.Event, out: list[float], interval: float = 2.0) -> None:
    """Сумма %CPU по дереву процессов: ps по pid и его детям."""
    while not stop.wait(interval):
        try:
            r = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,%cpu="],
                               capture_output=True, text=True, timeout=10)
        except Exception:
            continue
        procs = {}
        for line in r.stdout.splitlines():
            parts = line.split()
            if len(parts) != 3:
                continue
            try:
                procs[int(parts[0])] = (int(parts[1]), float(parts[2]))
            except ValueError:
                continue
        if pid not in procs:
            continue
        tree, frontier = {pid}, [pid]
        while frontier:
            nxt = [p for p, (ppid, _) in procs.items() if ppid in frontier and p not in tree]
            tree.update(nxt)
            frontier = nxt
        out.append(sum(procs[p][1] for p in tree if p in procs))


def main() -> int:
    sep = sys.argv.index("--")
    metrics_path = sys.argv[1]
    cmd = ["/usr/bin/time", "-l"] + sys.argv[sep + 1:]

    cpu: list[float] = []
    stop = threading.Event()
    t0 = time.monotonic()
    proc = subprocess.Popen(cmd, stderr=subprocess.PIPE, text=True, bufsize=1)
    sampler = threading.Thread(target=sample_cpu, args=(proc.pid, stop, cpu), daemon=True)
    sampler.start()

    err_lines = []
    for line in proc.stderr:
        sys.stderr.write(line)
        err_lines.append(line)
    proc.wait()
    stop.set()
    sampler.join(timeout=5)
    wall = time.monotonic() - t0

    err = "".join(err_lines)
    m = re.search(r"(\d+)\s+maximum resident set size", err)
    peak_rss_mb = int(m.group(1)) / (1024 * 1024) if m else None

    metrics = {
        "cmd": sys.argv[sep + 1:],
        "returncode": proc.returncode,
        "wall_seconds": round(wall, 2),
        "peak_rss_mb": round(peak_rss_mb, 1) if peak_rss_mb else None,
        "cpu_samples": len(cpu),
        "cpu_mean_pct": round(sum(cpu) / len(cpu), 1) if cpu else None,
        "cpu_max_pct": round(max(cpu), 1) if cpu else None,
    }
    with open(metrics_path, "w") as f:
        json.dump(metrics, f, ensure_ascii=False, indent=1)
    print(json.dumps(metrics, ensure_ascii=False), file=sys.stderr)
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
