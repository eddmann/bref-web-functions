#!/usr/bin/env python3
"""Lambda's own accounting for a load.py run, and its cost.

  analyse.py <run.json> [<run.json> ...] > summary.json

Needs the AWS CLI (`aws`) for the Region the functions run in. Names follow the
infra/aws stack: `bench-event` is the Bref event function (log group
/aws/lambda/bref-web-bench-event); anything else is a Web Function
(/aws/lambda/web/bref-web-<name>.local).

Prices: AWS Lambda pricing page, US East (N. Virginia), Arm, read 2026-11-09.
Web Functions bill requests, active vCPU time (not I/O wait) and the memory
each environment consumes for as long as it exists, including idle time.
Event functions bill requests and configured memory for the billed duration.
"""
import json
import re
import subprocess
import sys
from datetime import datetime

WEB_REQUEST = 0.30 / 1_000_000
WEB_VCPU_SECOND = 0.0000276944
WEB_GB_SECOND = 0.0000036667
EVENT_REQUEST = 0.20 / 1_000_000
EVENT_GB_SECOND_ARM = 0.0000133334
# Environments stayed alive about five minutes after their last request on AWS.
IDLE_SECONDS = 300

REPORT = re.compile(r"Duration: ([\d.]+) ms\s+Billed Duration: (\d+) ms\s+Memory Size: (\d+) MB\s+"
                    r"Max Memory Used: (\d+) MB(?:\s+Init Duration: ([\d.]+) ms)?")


def events(group, pattern, start, end):
    found, token = [], None
    while True:
        command = ["aws", "logs", "filter-log-events", "--log-group-name", group, "--filter-pattern", pattern,
                   "--start-time", str(int(start * 1000)), "--end-time", str(int(end * 1000)), "--output", "json"]
        if token:
            command += ["--next-token", token]
        page = json.loads(subprocess.run(command, capture_output=True, text=True, check=True).stdout)
        found += page.get("events", [])
        token = page.get("nextToken")
        if not token:
            return found


def web(name, start, end):
    group = f"/aws/lambda/web/bref-web-{name}.local"
    # Lambda reports usage per environment per minute; run.sh's gaps keep a
    # window from spanning two runs, so take every window that ends within a
    # minute of this run (its last one includes idle time the environment is billed for).
    platform = [json.loads(e["message"]) for e in events(group, '"platform."', start - 5, end + 65)]
    summaries = [p for p in platform if p["type"] == "platform.invokeSummary"]
    cpu = sum(s["record"]["metrics"]["cpuTimeMs"] for s in summaries) / 1000
    cpu += sum(p["record"]["metrics"].get("totalCpuTimeMs", 0) for p in platform
               if p["type"] == "platform.initReport") / 1000
    gb_seconds, peak_memory = 0.0, 0
    for summary in summaries:
        window_start = datetime.fromisoformat(summary["record"]["startTime"][:26].rstrip("Z") + "+00:00")
        window_end = datetime.fromisoformat(summary["timestamp"].replace("Z", "+00:00"))
        memory = summary["record"]["metrics"]["maxMemoryUsageMb"]
        peak_memory = max(peak_memory, memory)
        gb_seconds += memory / 1024 * max(0.0, (window_end - window_start).total_seconds())
    requests = sum(s["record"]["metrics"]["requests"] for s in summaries)
    environments = {p["record"].get("instanceId") for p in summaries}
    cold_starts = sum(1 for p in platform if p["type"] == "platform.initStart")
    cost = requests * WEB_REQUEST + cpu * WEB_VCPU_SECOND + gb_seconds * WEB_GB_SECOND
    idle = len(environments) * (peak_memory / 1024) * IDLE_SECONDS * WEB_GB_SECOND
    return {"kind": "web", "requests_billed": requests, "cold_starts": cold_starts,
            "environments": len(environments), "active_cpu_s": round(cpu, 2),
            "memory_gb_s": round(gb_seconds, 1), "peak_environment_memory_mb": peak_memory,
            "cost_usd": round(cost, 6), "idle_tail_cost_usd": round(idle, 6)}


def event(name, start, end):
    group = f"/aws/lambda/bref-web-{name}"
    # REPORT lines are written as each request ends: the run's own window is exact.
    found = events(group, '"REPORT RequestId"', start - 1, end + 5)
    reports = [r for r in (REPORT.search(e["message"]) for e in found) if r]
    streams = {e["logStreamName"] for e in found}
    billed_gb_s = sum(int(r.group(2)) / 1000 * int(r.group(3)) / 1024 for r in reports)
    cost = len(reports) * EVENT_REQUEST + billed_gb_s * EVENT_GB_SECOND_ARM
    return {"kind": "event", "requests_billed": len(reports),
            "cold_starts": sum(1 for r in reports if r.group(5)),
            "environments": len(streams), "billed_gb_s": round(billed_gb_s, 1),
            "mean_duration_ms": round(sum(float(r.group(1)) for r in reports) / max(1, len(reports)), 1),
            "peak_environment_memory_mb": max((int(r.group(4)) for r in reports), default=0),
            "cost_usd": round(cost, 6), "idle_tail_cost_usd": 0.0}


def main():
    rows = []
    for path in sys.argv[1:]:
        run = json.load(open(path))
        name = run["name"]
        accounting = event(name, run["start"], run["end"]) if name == "bench-event" else web(name, run["start"], run["end"])
        requests = max(1, accounting["requests_billed"])
        rows.append({"run": path, "name": name, "path": run["path"], "mode": run["mode"],
                     **{k: run[k] for k in ("rps", "seconds", "concurrency") if k in run},
                     "client": run["summary"], "lambda": accounting,
                     "cost_per_million_usd": round(accounting["cost_usd"] / requests * 1_000_000, 2)})
        print(f"{name:12} {run['path']:22} cold={accounting['cold_starts']:<4} envs={accounting['environments']:<4} "
              f"p50={run['summary']['latency_s']['p50']} p99={run['summary']['latency_s']['p99']} "
              f"per1M=${rows[-1]['cost_per_million_usd']}", file=sys.stderr)
    json.dump(rows, sys.stdout, indent=2)


if __name__ == "__main__":
    main()
