#!/usr/bin/env python3
"""Concurrent first requests against a freshly started framework application.

Several PHP-FPM workers serve a Web Function environment's first requests at
the same moment, so anything a framework prepares lazily on its first request
(caches copied to /tmp, compiled files) is prepared by several workers at once.
This starts the packaged application --starts times and sends --burst
simultaneous first requests each time; any non-200 response fails the check.

    python3 tests/frameworks/cold_start.py symfony /tmp/parity-symfony --architecture aarch64

Known upstream issue: bref/symfony-bridge prepares its /tmp cache directory
non-atomically and fails here until upstream/symfony-bridge/atomic-cache-dir.patch
is applied.
"""

import argparse
import collections
import http.client
import importlib.util
import json
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

from run import FRAMEWORK_ENV, LAMBDA_ENV, NODE_IMAGES, PACKAGE_FLAGS, ROOT, docker, env_args



def wait_ready(name):
    for _ in range(150):
        ports = json.loads(docker("inspect", name).stdout)[0]["NetworkSettings"]["Ports"].get("8080/tcp")
        if ports:
            port = int(ports[0]["HostPort"])
            try:
                probe = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
                probe.request("GET", "/__bref_web/health")
                if probe.getresponse().status == 200:
                    return port
            except (OSError, http.client.HTTPException):
                pass
        time.sleep(0.1)
    raise RuntimeError("Application did not become ready:\n" + docker("logs", name, check=False).stderr[-4000:])


def burst(port, count):
    """Open every connection first, then send all requests at once."""
    barrier, statuses, lock = threading.Barrier(count), [], threading.Lock()

    def request():
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        connection.connect()
        barrier.wait()
        try:
            connection.request("GET", "/probe/x", headers={"host": "example.com"})
            response = connection.getresponse()
            response.read()
            status = response.status
        except (OSError, http.client.HTTPException) as error:
            status = type(error).__name__
        with lock:
            statuses.append(status)

    threads = [threading.Thread(target=request) for _ in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return collections.Counter(str(status) for status in statuses)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("framework", choices=FRAMEWORK_ENV)
    parser.add_argument("app_dir", type=Path)
    parser.add_argument("--runtime-dir", type=Path, default=ROOT / "build/ci/runtime")
    parser.add_argument("--rust-binary", type=Path, default=ROOT / "build/ci/bref-web-gateway")
    parser.add_argument("--architecture", default="x86_64", choices=NODE_IMAGES)
    parser.add_argument("--starts", type=int, default=6)
    parser.add_argument("--burst", type=int, default=32)
    parser.add_argument("--workers", type=int, default=16)
    args = parser.parse_args()

    spec = importlib.util.spec_from_file_location("package_extract", ROOT / "packaging/extract.py")
    extractor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(extractor)
    environment = {**LAMBDA_ENV, **FRAMEWORK_ENV[args.framework],
                   "BREF_WEB_MAX_IN_FLIGHT": str(args.workers), "BREF_WEB_QUEUE_CAPACITY": str(args.burst)}
    (ROOT / "build").mkdir(exist_ok=True)
    runs = []
    with tempfile.TemporaryDirectory(prefix="framework-cold-start-", dir=ROOT / "build") as temporary:
        archive = Path(temporary) / "app.zip"
        subprocess.run(["node", str(ROOT / "scripts/package.mjs"), "--runtime-dir", str(args.runtime_dir),
                        "--rust-binary", str(args.rust_binary), "--architecture", args.architecture,
                        "--app-dir", str(args.app_dir.resolve()), "--output", str(archive),
                        *PACKAGE_FLAGS[args.framework]], check=True, capture_output=True)
        extracted = Path(temporary) / "task"
        extractor.extract(archive, extracted)
        for start in range(args.starts):
            name = f"framework-cold-start-{uuid.uuid4().hex[:8]}"
            docker("run", "--detach", "--name", name, "--user", "1000:1000", "--read-only",
                   "--tmpfs", "/tmp:rw,nosuid,nodev,size=512m,mode=1777", "--publish", "127.0.0.1::8080",
                   "--mount", f"type=bind,src={extracted},dst=/var/task,readonly",
                   "-e", "AWS_LAMBDA_HTTP_ENDPOINT=0.0.0.0:8080", *env_args(environment),
                   "--entrypoint", "/var/lang/bin/node", NODE_IMAGES[args.architecture], "/var/task/index.js")
            try:
                statuses = burst(wait_ready(name), args.burst)
            finally:
                logs = docker("logs", name, check=False).stderr
                docker("rm", "-f", name, check=False)
            errors = [line for line in logs.splitlines() if "Uncaught" in line or "PHP Fatal" in line]
            runs.append({"start": start + 1, "statuses": statuses, "errors": errors[:3]})
            print(json.dumps(runs[-1]), flush=True)
    failed = [run for run in runs if set(run["statuses"]) != {"200"}]
    print(f"{args.framework}: {len(runs) - len(failed)}/{len(runs)} cold starts served every first request")
    return int(bool(failed))


if __name__ == "__main__":
    sys.exit(main())
