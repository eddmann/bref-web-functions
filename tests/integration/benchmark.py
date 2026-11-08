#!/usr/bin/env python3
"""Comparable local HTTP measurements. These do not estimate AWS cost or capacity."""

import argparse
import concurrent.futures
import datetime
import json
import math
import pathlib
import statistics
import sys
import time

from run import Client, HarnessLock, check


def percentile(values, quantile):
    values = sorted(values)
    position = (len(values) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def measure(client, count, concurrency, path):
    durations, errors, statuses = [], [], {}
    def invoke(_):
        try:
            response = client.request(path)
            return response["seconds"], response["status"], None
        except Exception as error:
            return None, None, f"{type(error).__name__}: {error}"
    start = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        for seconds, status, error in executor.map(invoke, range(count)):
            if error:
                errors.append(error)
            else:
                statuses[str(status)] = statuses.get(str(status), 0) + 1
                durations.append(seconds * 1000)
    elapsed = time.monotonic() - start
    result = {"requests": count, "concurrency": concurrency, "seconds": elapsed,
              "completed_requests_per_second": len(durations) / elapsed,
              "successful_requests_per_second": statuses.get("200", 0) / elapsed,
              "statuses": statuses, "transport_errors": len(errors), "error_samples": errors[:3]}
    if durations:
        result["latency_ms"] = {"min": min(durations), "median": statistics.median(durations),
                                "p95": percentile(durations, .95), "p99": percentile(durations, .99), "max": max(durations)}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--label", default="gateway")
    parser.add_argument("--iterations", type=int, default=200, help="Request count at each concurrency")
    parser.add_argument("--concurrency", type=int, nargs="+", default=[1, 4, 16])
    parser.add_argument("--path", default="/health")
    parser.add_argument("--output", type=pathlib.Path)
    args = parser.parse_args()
    if not 1 <= args.iterations <= 100000 or any(not 1 <= value <= 256 for value in args.concurrency):
        parser.error("iterations must be 1..100000 and concurrency must be 1..256")
    if not args.path.startswith("/") or args.path.startswith("//"):
        parser.error("--path must be an origin-relative path")
    with HarnessLock():
        try:
            client = Client(args.base_url)
            for _ in range(5):
                check(client.request(args.path)["status"] == 200, "Warmup did not return HTTP 200")
        except Exception as error:
            parser.error(f"Cannot warm up local fixture: {error}")
        results = [measure(client, args.iterations, concurrency, args.path) for concurrency in args.concurrency]
    report = {"label": args.label, "base_url": args.base_url, "path": args.path,
              "timestamp_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "methodology": "5 warmup requests; fixed requests per concurrency; one new HTTP connection per request; identical harness for both gateways.",
              "scope": "Local prototype measurement only. No AWS cost, scaling, memory or production-performance claim.",
              "results": results}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return int(any(result["transport_errors"] or result["statuses"].get("200", 0) != args.iterations for result in results))


if __name__ == "__main__":
    sys.exit(main())
