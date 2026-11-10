#!/usr/bin/env python3
"""Exercise the AWS test-only archive locally before deploying it.

Runs the exact ZIP in the managed Node image (as scripts/test-package.py does)
with short FPM/HTTP deadlines, then checks each test route and the gateway's
log lines. The final case uses the stuck route: real PHP-FPM cannot stop that
worker, so the runtime must retire and exit nonzero. This is local evidence,
not AWS behaviour.
"""

import argparse
import http.client
import importlib.util
import json
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))
from node_images import NODE_IMAGES  # noqa: E402

PLATFORMS = {"x86_64": "linux/amd64", "aarch64": "linux/arm64"}
FPM_TIMEOUT_SECONDS = 2
REQUEST_TIMEOUT_MS = 3000
# FPM checks request_terminate_timeout only every timeout / 3, so it ends an
# overrun up to a third late. With the deployed 28 s / 30 s that is 9.33 s, far
# wider than the 2 s margin. 9 s / 9 s reproduces that (3 s heartbeat beyond
# the HTTP deadline) quickly; the short settings above cannot.
OVERRUN_FPM_TIMEOUT_SECONDS = 9
OVERRUN_REQUEST_TIMEOUT_MS = 9000
TEST_TOKEN = "local-test-token-0123456789"


def docker(*args, check=True):
    result = subprocess.run(["docker", *args], capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RuntimeError(f"docker {args[0]} failed: {result.stderr[-2000:]}")
    return result


def request(port, path, request_id, timeout=15, token=TEST_TOKEN, extra_headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    started = time.monotonic()
    headers = {"lambda-web-request-id": request_id, "host": "example.com", "connection": "close",
               **(extra_headers or {})}
    if token is not None:
        headers["x-aws-test-token"] = token
    connection.request("GET", path, headers=headers)
    response = connection.getresponse()
    first_byte = time.monotonic() - started
    lines, arrivals = [], []
    while True:
        # Decoded body lines (chunk framing removed), timed as they arrive.
        line = response.readline()
        if not line:
            break
        lines.append(line)
        arrivals.append(round(time.monotonic() - started, 3))
    return {"status": response.status, "body": b"".join(lines), "first_byte_s": round(first_byte, 3),
            "arrivals_s": arrivals, "total_s": round(time.monotonic() - started, 3)}


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def start_runtime(extracted, architecture, fpm_seconds, request_ms, environment=()):
    """Run the extracted archive in the managed Node image; return (container, port)."""
    name = f"aws-local-{uuid.uuid4().hex[:8]}"
    extra = [item for variable in environment for item in ("-e", variable)]
    docker("run", "--detach", "--name", name, "--platform", PLATFORMS[architecture], *extra,
           "--user", "1000:1000", "--read-only",
           "--tmpfs", "/tmp:rw,nosuid,nodev,size=512m,mode=1777", "--publish", "127.0.0.1::8080",
           "--mount", f"type=bind,src={extracted},dst=/var/task,readonly",
           "-e", "AWS_LAMBDA_HTTP_ENDPOINT=0.0.0.0:8080",
           "-e", f"BREF_WEB_FPM_TIMEOUT_SECONDS={fpm_seconds}",
           "-e", f"BREF_WEB_REQUEST_TIMEOUT_MS={request_ms}",
           "-e", f"AWS_TEST_TOKEN={TEST_TOKEN}",
           "--entrypoint", "/var/lang/bin/node", NODE_IMAGES[architecture], "/var/task/index.js")
    for _ in range(100):
        ports = json.loads(docker("inspect", name).stdout)[0]["NetworkSettings"]["Ports"].get("8080/tcp")
        if ports:
            port = int(ports[0]["HostPort"])
            try:
                if request(port, "/__bref_web/health", "health", timeout=1)["status"] == 200:
                    return name, port
            except (OSError, http.client.HTTPException):
                pass
        time.sleep(0.1)
    docker("rm", "-f", name, check=False)
    raise RuntimeError("runtime did not become ready")


def container_logs(name):
    result = docker("logs", name, check=False)
    return result.stderr + result.stdout


# What Lambda's ingress delivers for a client sending its own forwarding headers
# (measured on AWS): X-Forwarded-For appended to, proto/port overwritten, the
# rest passed through.
SPOOFED = {"x-forwarded-for": "192.0.2.1, 203.0.113.7", "x-forwarded-proto": "https", "x-forwarded-port": "443",
           "x-forwarded-host": "evil.example", "x-forwarded-prefix": "/evil", "forwarded": "for=192.0.2.9",
           "x-real-ip": "192.0.2.5", "x-amzn-trace-id": "Root=spoofed"}


def check_forwarded_headers(extracted, architecture):
    """Passthrough (default) keeps client headers like Bref; ingress keeps only Lambda's."""
    seen = {}
    for mode in ("passthrough", "ingress"):
        name, port = start_runtime(extracted, architecture, FPM_TIMEOUT_SECONDS, REQUEST_TIMEOUT_MS,
                                   [f"BREF_WEB_FORWARDED_HEADERS={mode}"])
        try:
            body = json.loads(request(port, "/", f"local-headers-{mode}", extra_headers=SPOOFED)["body"])
            seen[mode] = {"headers": {k: v for k, v in body["headers"].items() if k in SPOOFED},
                          "trace": body["cgi"]["_X_AMZN_TRACE_ID"]}
        finally:
            docker("rm", "-f", name, check=False)
    passthrough, ingress = seen["passthrough"], seen["ingress"]
    check(passthrough["headers"] == SPOOFED and passthrough["trace"] == "Root=spoofed",
          f"passthrough changed client headers: {passthrough}")
    check(ingress["headers"] == {"x-forwarded-for": "203.0.113.7", "x-forwarded-proto": "https",
                                 "x-forwarded-port": "443"} and ingress["trace"] is None,
          f"ingress mode passed client headers: {ingress}")
    return seen


def check_overruns(extracted, architecture):
    """Overruns ended late by FPM's heartbeat must resolve without retiring the runtime."""
    name, port = start_runtime(extracted, architecture, OVERRUN_FPM_TIMEOUT_SECONDS, OVERRUN_REQUEST_TIMEOUT_MS)
    heartbeat = OVERRUN_FPM_TIMEOUT_SECONDS / 3
    try:
        # Four workers, staggered across one heartbeat, so one deadline falls just
        # after a check and FPM ends that worker almost a full heartbeat late.
        statuses = [None] * 4

        def overrun(index):
            statuses[index] = request(port, "/aws-test/sleep?ms=60000", f"local-overrun-{index}", timeout=20)["status"]

        threads = []
        for index in range(4):
            thread = threading.Thread(target=overrun, args=(index,))
            thread.start()
            threads.append(thread)
            time.sleep(heartbeat / 4)
        for thread in threads:
            thread.join()
        check(all(status in (502, 504) for status in statuses), f"overruns returned {statuses}")
        # Every worker ends by FPM's worst case (timeout + heartbeat after it started).
        deadline = time.monotonic() + OVERRUN_FPM_TIMEOUT_SECONDS + heartbeat + 3
        while time.monotonic() < deadline:
            logs = container_logs(name)
            if logs.count("execution timed out") >= 4 or "unresolved_backend=true" in logs:
                break
            time.sleep(0.2)
        time.sleep(0.5)
        state = json.loads(docker("inspect", name).stdout)[0]["State"]
        logs = container_logs(name)
        check(state["Running"] and "unresolved_backend=true" not in logs,
              "a worker FPM ended late retired the runtime")
        check(logs.count("execution timed out") == 4, "FPM did not terminate every overrunning worker")
        after = request(port, "/", "local-after-overrun")
        check(after["status"] == 200, f"runtime did not serve after overruns: {after['status']}")
        delays = sorted(round(float(value), 3) for value in re.findall(r"execution timed out \(([\d.]+) sec", logs))
        return {"statuses": statuses, "fpm_termination_s": delays, "after": after["status"]}, logs
    finally:
        docker("rm", "-f", name, check=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location("package_extract", ROOT / "packaging/extract.py")
    extractor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(extractor)
    results, logs = {}, ""
    (ROOT / "build").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="aws-local-", dir=ROOT / "build") as temporary:
        extracted = Path(temporary) / "task"
        manifest = extractor.extract(args.archive.resolve(), extracted)
        architecture = manifest["architecture"]
        results["forwarded_headers"] = check_forwarded_headers(extracted, architecture)
        results["overruns"], overrun_logs = check_overruns(extracted, architecture)
        name, port = start_runtime(extracted, architecture, FPM_TIMEOUT_SECONDS, REQUEST_TIMEOUT_MS)
        try:
            diagnostic = request(port, "/", "local-diagnostic")
            body = json.loads(diagnostic["body"])
            check(diagnostic["status"] == 200 and body["ok"] and body["sapi"] == "fpm-fcgi", "diagnostic failed")
            check(body["request_id"] == "local-diagnostic", "request ID not passed through")
            check(body["environment"]["first_request_in_environment"] is True, "first request not detected")
            second = json.loads(request(port, "/", "local-diagnostic-2")["body"])
            check(second["environment"]["environment_id"] == body["environment"]["environment_id"]
                  and second["environment"]["first_request_in_environment"] is False, "environment marker unstable")
            check(TEST_TOKEN not in diagnostic["body"].decode(), "diagnostic page revealed the test token")
            results["diagnostic"] = {"environment": body["environment"], "cgi": body["cgi"]}

            unauthorised = request(port, "/aws-test/sleep?ms=1", "local-no-token", token=None)
            wrong = request(port, "/aws-test/sleep?ms=1", "local-wrong-token", token="wrong")
            check(unauthorised["status"] == 403 and wrong["status"] == 403, "test routes not protected by the token")
            results["token"] = {"missing": unauthorised["status"], "wrong": wrong["status"]}

            fast = request(port, "/aws-test/sleep?ms=200", "local-sleep-fast")
            check(fast["status"] == 200, f"short sleep failed: {fast['status']}")
            # FPM's script limit (2 s) ends the worker by 2.67 s (its heartbeat is
            # timeout / 3), before the HTTP deadline (3 s): the client sees 502.
            # With the deployed 28 s / 30 s that is not guaranteed; see check_overruns.
            slow = request(port, "/aws-test/sleep?ms=10000", "local-sleep-slow")
            check(slow["status"] == 502, f"sleep beyond the FPM limit returned {slow['status']}")
            results["sleep"] = {"fast_s": fast["total_s"], "slow_status": slow["status"], "slow_s": slow["total_s"]}

            stream = request(port, "/aws-test/stream?chunks=3&interval_ms=500", "local-stream")
            check(stream["status"] == 200 and len(stream["arrivals_s"]) == 3, "stream incomplete")
            check(stream["arrivals_s"][1] - stream["arrivals_s"][0] > 0.3, f"chunks were buffered: {stream['arrivals_s']}")
            results["stream"] = {"arrivals_s": stream["arrivals_s"]}

            large = request(port, "/aws-test/large?mb=16", "local-large")
            check(large["status"] == 200 and len(large["body"]) == 16 * 1048576, "large response truncated")
            crash = request(port, "/aws-test/crash", "local-crash")
            check(crash["status"] == 502, f"crashed worker returned {crash['status']}")
            after_crash = request(port, "/", "local-after-crash")
            check(after_crash["status"] == 200, "runtime did not recover from a worker crash")
            memory = request(port, "/aws-test/memory?mb=512", "local-memory")
            check(memory["status"] >= 500, f"allocation beyond memory_limit returned {memory['status']}")
            results["failures"] = {"crash": crash["status"], "after_crash": after_crash["status"],
                                   "memory_over_limit": memory["status"]}

            # Real PHP-FPM cannot stop this worker, so completion is never confirmed.
            stuck_started = time.monotonic()
            stuck = request(port, "/aws-test/stuck?seconds=60", "local-stuck", timeout=30)
            check(stuck["status"] == 504, f"stuck request returned {stuck['status']}")
            exit_code = None
            for _ in range(200):
                state = json.loads(docker("inspect", name).stdout)[0]["State"]
                if not state["Running"]:
                    exit_code = state["ExitCode"]
                    break
                time.sleep(0.1)
            check(exit_code not in (None, 0), f"runtime did not retire with a nonzero exit (exit={exit_code})")
            results["retirement"] = {"stuck_status": stuck["status"], "exit_code": exit_code,
                                     "seconds_to_exit": round(time.monotonic() - stuck_started, 2)}
        finally:
            logs = container_logs(name)
            docker("rm", "-f", name, check=False)

    for marker in ("startup_ms=", "request_id=local-sleep-slow gateway_status=502",
                   "request_id=local-stuck gateway_status=504 reason=response_head_timeout", "unresolved_backend=true",
                   "runtime retiring: backend completion unconfirmed request_id=local-stuck", "aws_test route=stuck"):
        check(marker in logs, f"log line missing: {marker}")
    results["log_markers_present"] = True
    if args.output:
        args.output.mkdir(parents=True, exist_ok=True)
        (args.output / "container.log").write_text(logs)
        (args.output / "overrun-container.log").write_text(overrun_logs)
        (args.output / "result.json").write_text(json.dumps(results, indent=2))
    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
