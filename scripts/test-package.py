#!/usr/bin/env python3
"""Run the exact extracted native ZIP in the managed Node base, without Bref /opt.

Suites: smoke (one request), integration (tests/integration), bref (tests/bref),
lifecycle (tests/lifecycle: one worker, no queue, a 150 ms request deadline).
"""

import argparse
import datetime
import hashlib
import importlib.util
import json
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from node_images import NODE_IMAGES  # noqa: E402

SUITES = {
    "integration": "tests/integration/run.py",
    "bref": "tests/bref/run.py",
    "lifecycle": "tests/lifecycle/run.py",
}
# The lifecycle checks occupy the only worker and need a short request deadline.
SUITE_ENVIRONMENT = {
    "lifecycle": {"BREF_WEB_MAX_IN_FLIGHT": "1", "BREF_WEB_QUEUE_CAPACITY": "0",
                  "BREF_WEB_REQUEST_TIMEOUT_MS": "150"},
}


def docker(*args):
    try:
        return subprocess.check_output(["docker", *args], text=True,
                                       stderr=subprocess.STDOUT).strip()
    except subprocess.CalledProcessError as error:
        raise RuntimeError(error.output.strip()) from error


def ready(url, name):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url + "/__bref_web/health", timeout=.5) as response:
                if response.status == 200:
                    return
        except Exception:
            pass
        time.sleep(.05)
    raise RuntimeError(f"{name} did not become ready: {docker('logs', name)}")


def run_harness(args, log):
    with log.open("w") as output:
        return subprocess.run([sys.executable] + args, cwd=ROOT,
                              stdout=output, stderr=subprocess.STDOUT).returncode



def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--suite", choices=("smoke", *SUITES), default="smoke")
    parser.add_argument("--output", type=Path, default=ROOT / "results/preaws/package-test")
    parser.add_argument("--body-limit", type=int, default=6291456)
    args = parser.parse_args()
    if not 1 <= args.body_limit <= 2147483647 :
        parser.error("Invalid body limit")
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    archive = args.archive.resolve()
    spec = importlib.util.spec_from_file_location("package_extract", ROOT / "packaging/extract.py")
    extractor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(extractor)
    sys.path.insert(0, str(ROOT / "tests/integration"))
    from run import Client, check

    name = f"bref-web-package-{uuid.uuid4().hex[:10]}"
    running = False
    result = {
        "timestamp_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "archive": str(archive), "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "scope": "Exact archive in a local managed Node base image; not the AWS Web Function service.",
        "suite": args.suite, "passed": False,
        "configuration": {"workers": 4, "max_body_bytes": args.body_limit},
    }
    (ROOT / "build").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="bref-web-exact-zip-", dir=ROOT / "build") as temporary:
        extracted = Path(temporary) / "task"
        manifest = extractor.extract(archive, extracted)
        expected = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}.get(platform.machine())
        if manifest["architecture"] != expected:
            parser.error("Native execution requires an archive matching this host; ELF inspection is not execution proof")
        result["architecture"] = manifest["architecture"]
        node_image = NODE_IMAGES[manifest["architecture"]]
        result["node_image"] = node_image
        try:
            started = time.monotonic()
            docker("run", "--detach", "--name", name,
                   "--platform", "linux/amd64" if expected == "x86_64" else "linux/arm64",
                   "--user", "1000:1000", "--read-only",
                   "--tmpfs", "/tmp:rw,nosuid,nodev,size=512m,mode=1777",
                   "--cpus", "2", "--memory", "2g", "--pids-limit", "256",
                   "--publish", "127.0.0.1::8080",
                   "--mount", f"type=bind,src={extracted},dst=/var/task,readonly",
                   "--env", "AWS_LAMBDA_HTTP_ENDPOINT=0.0.0.0:8080",
                   "--env", "BREF_WEB_FPM_TIMEOUT_SECONDS=2",
                   "--env", "BREF_WEB_REQUEST_TIMEOUT_MS=6000",
                   "--env", "BREF_WEB_MAX_IN_FLIGHT=4",
                   "--env", f"BREF_WEB_MAX_BODY_BYTES={args.body_limit}",
                   *[item for name, value in SUITE_ENVIRONMENT.get(args.suite, {}).items()
                     for item in ("--env", f"{name}={value}")],
                   "--env", "NODE_OPTIONS=--max-old-space-size=64",
                   "--entrypoint", "/var/lang/bin/node", node_image, "/var/task/index.js")
            running = True
            ports = json.loads(docker("inspect", name))[0]["NetworkSettings"]["Ports"]
            if not ports.get("8080/tcp"):
                raise RuntimeError("Entrypoint exited before opening its port: " + docker("logs", name))
            port = ports["8080/tcp"][0]["HostPort"]
            url = f"http://127.0.0.1:{port}"
            result["url"] = url
            ready(url, name)
            result["local_startup_seconds"] = time.monotonic() - started
            result["no_original_opt_php"] = docker("exec", name, "/bin/bash", "-c", "test ! -e /opt/bin/php && echo true") == "true"
            check(result["no_original_opt_php"], "Original Bref /opt unexpectedly present")
            result["system_ca_readable"] = docker("exec", name, "/bin/bash", "-c", "test -r /etc/ssl/cert.pem && echo true") == "true"
            if args.suite == "smoke":
                response = Client(url).json("/", headers={"lambda-web-request-id": "exact-package-smoke"})
                check(response["ok"] and response["sapi"] == "fpm-fcgi", "Smoke app did not execute under FPM")
                check(response["request_id"] == "exact-package-smoke", "Request metadata was lost")
                result["response"] = response
            else:
                code = run_harness([SUITES[args.suite], "--base-url", url, "--output", str(args.output / "suite.json")], args.output / "suite.log")
                result["suite_exit_code"] = code
                check(code == 0, f"{args.suite} failed; see {args.output / 'suite.log'}")
            result["passed"] = True
        except Exception as error:
            result["error"] = str(error)
        finally:
            if running:
                try:
                    stop_started = time.monotonic()
                    docker("stop", "--timeout", "2", name)
                    result["local_stop_command_seconds"] = time.monotonic() - stop_started
                    (args.output / "container.log").write_text(docker("logs", name))
                    state = json.loads(docker("inspect", name))[0]["State"]
                    result["exit_code"] = state["ExitCode"]
                    result["oom_killed"] = state["OOMKilled"]
                    if state["ExitCode"] != 0 or state["OOMKilled"]:
                        result["passed"] = False
                    docker("rm", name)
                except Exception as error:
                    result["cleanup_error"] = str(error)
                    result["passed"] = False
            (args.output / "package-test.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    return int(not result["passed"])


if __name__ == "__main__":
    sys.exit(main())
