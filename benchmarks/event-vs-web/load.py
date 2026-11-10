#!/usr/bin/env python3
"""Open-loop load for benchmarks/event-vs-web.

  load.py rate  <name> <base-url> <path> <rps> <seconds> <out.json>
  load.py burst <name> <base-url> <path> <concurrency> <out.json>

`rate` sends requests at a fixed rate over persistent HTTPS connections (one per
worker thread, as keep-alive clients do), whether or not earlier responses have
arrived. `burst` opens <concurrency> fresh connections and sends one request on
each at the same moment (cold starts). Every record: scheduled and actual send
time, status, latency, the serving environment (the routes' `environment`
field) and any Lambda error type.
"""
import http.client
import json
import queue
import statistics
import sys
import threading
import time
from collections import Counter
from urllib.parse import urlparse

WORKERS = 256


def send(connection, host, path):
    connection.request("GET", path, headers={"host": host, "user-agent": "bref-bench/1"})
    response = connection.getresponse()
    body = response.read()
    environment = None
    if response.getheader("content-type", "").startswith("application/json"):
        try:
            environment = json.loads(body).get("environment")
        except ValueError:
            pass
    error = response.getheader("lambda-web-error-type") or response.getheader("x-amzn-errortype")
    return response.status, environment, error


def record(scheduled, sent, status, environment, error):
    return {"scheduled": round(scheduled, 4), "sent": round(sent, 4),
            "latency": round(time.time() - sent, 4), "status": status,
            "environment": environment, "error": error}


def rate(host, path, rps, seconds):
    jobs, results, lock = queue.Queue(), [], threading.Lock()

    def worker():
        connection = None
        while (scheduled := jobs.get()) is not None:
            delay = scheduled - time.time()
            if delay > 0:
                time.sleep(delay)
            sent = time.time()
            try:
                connection = connection or http.client.HTTPSConnection(host, timeout=60)
                result = record(scheduled, sent, *send(connection, host, path))
            except Exception as error:  # a failed request is a result, not a crash
                result = record(scheduled, sent, 0, None, type(error).__name__)
                connection = None
            with lock:
                results.append(result)

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(WORKERS)]
    for thread in threads:
        thread.start()
    start = time.time() + 2  # let workers start
    for index in range(rps * seconds):
        jobs.put(start + index / rps)
    for _ in threads:
        jobs.put(None)
    for thread in threads:
        thread.join()
    return start, results


def burst(host, path, concurrency):
    barrier, results, lock = threading.Barrier(concurrency), [], threading.Lock()

    def worker():
        connection = http.client.HTTPSConnection(host, timeout=60)
        connection.connect()  # TLS before the barrier: the burst is HTTP requests
        barrier.wait()
        sent = time.time()
        try:
            result = record(sent, sent, *send(connection, host, path))
        except Exception as error:
            result = record(sent, sent, 0, None, type(error).__name__)
        with lock:
            results.append(result)

    threads = [threading.Thread(target=worker) for _ in range(concurrency)]
    start = time.time()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return start, results


def percentile(values, fraction):
    return round(values[min(len(values) - 1, int(len(values) * fraction))], 3) if values else None


def summarise(results):
    ok = sorted(r["latency"] for r in results if r["status"] == 200)
    queued = sorted(r["sent"] - r["scheduled"] for r in results)
    return {
        "requests": len(results),
        "statuses": dict(Counter(str(r["status"]) for r in results)),
        "errors": dict(Counter(r["error"] for r in results if r["error"])),
        "environments": len({r["environment"] for r in results if r["environment"]}),
        "latency_s": {"p50": percentile(ok, 0.5), "p90": percentile(ok, 0.9),
                      "p99": percentile(ok, 0.99), "max": round(ok[-1], 3) if ok else None,
                      "mean": round(statistics.mean(ok), 3) if ok else None},
        # Client-side queueing: large values mean the load generator, not the
        # target, limited the rate.
        "client_queue_p99_s": percentile(queued, 0.99),
    }


def main():
    mode, name, base, path = sys.argv[1:5]
    host = urlparse(base).netloc
    if mode == "rate":
        rps, seconds, out = int(sys.argv[5]), int(sys.argv[6]), sys.argv[7]
        start, results = rate(host, path, rps, seconds)
        settings = {"rps": rps, "seconds": seconds}
    else:
        concurrency, out = int(sys.argv[5]), sys.argv[6]
        start, results = burst(host, path, concurrency)
        settings = {"concurrency": concurrency}
    summary = summarise(results)
    report = {"name": name, "mode": mode, "path": path, **settings, "start": start,
              "end": max((r["sent"] + r["latency"]) for r in results), "summary": summary,
              "results": sorted(results, key=lambda r: r["scheduled"])}
    with open(out, "w") as handle:
        json.dump(report, handle)
    print(json.dumps({"name": name, "mode": mode, "path": path, **settings, **summary}))


if __name__ == "__main__":
    main()
