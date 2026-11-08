#!/usr/bin/env python3
"""Run the exact extracted native ZIP in the managed Node base, without Bref /opt."""

import argparse
import datetime
import hashlib
import importlib.util
import json
from pathlib import Path
import platform
import sys
import tempfile
import time
import uuid

from compare import ROOT, docker, ready, run_harness

NODE_IMAGES = {
    "x86_64": "public.ecr.aws/lambda/nodejs:24@sha256:d03ccef4a3be84ef9891b371f526198209d010c0102afd87f4760d9075bcceb7",
    "aarch64": "public.ecr.aws/lambda/nodejs:24@sha256:657dcbd936861638a626cdaab538238f40cd346d7e239ae8c4c487dfd4bf31f2",
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--suite", choices=("smoke", "integration", "bref"), default="smoke")
    parser.add_argument("--output", type=Path, default=ROOT / "results/preaws/package-test")
    parser.add_argument("--body-limit", type=int, default=6291456)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    if not 1 <= args.body_limit <= 2147483647 or not 1 <= args.workers <= 128:
        parser.error("Invalid body limit or worker count")
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
        "configuration": {"workers": args.workers, "max_body_bytes": args.body_limit},
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
                   "--env", f"BREF_WEB_MAX_IN_FLIGHT={args.workers}",
                   "--env", f"BREF_WEB_MAX_BODY_BYTES={args.body_limit}",
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
                script = "tests/integration/run.py" if args.suite == "integration" else "tests/bref/run.py"
                code = run_harness([script, "--base-url", url, "--output", str(args.output / "suite.json")], args.output / "suite.log")
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
