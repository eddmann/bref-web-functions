#!/usr/bin/env python3
"""Run both local prototypes, serializing functional checks and measurements."""

import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
DOCKER = ["docker", "--host=unix:///var/run/docker.sock"]
ENV = {k: v for k, v in os.environ.items() if k not in {
    "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"
}}


def docker(*args):
    try:
        return subprocess.check_output(DOCKER + list(args), env=ENV, text=True,
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


def source_hashes():
    result = {}
    for item in sorted(ROOT.rglob("*")):
        relative = item.relative_to(ROOT)
        if not item.is_file() or any(part in {
            "target", "results", "build", ".git", "__pycache__", "vendor"
        } for part in relative.parts):
            continue
        result[str(relative)] = hashlib.sha256(item.read_bytes()).hexdigest()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="bref-web-functions:local")
    parser.add_argument("--iterations", type=int, default=200)
    parser.add_argument("--concurrency", type=int, nargs="+", default=[1, 4, 8])
    parser.add_argument("--skip-benchmark", action="store_true", help="Run functional/lifecycle checks only")
    parser.add_argument("--output", type=Path, default=ROOT / "results")
    args = parser.parse_args()
    if not 1 <= args.iterations <= 100000 or any(not 1 <= x <= 256 for x in args.concurrency):
        parser.error("Invalid iteration count or concurrency")
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    metadata = {
        "timestamp_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "scope": "Local container checks; no deployed Lambda or production-performance claim.",
        "host": platform.platform(),
        "image": json.loads(docker("image", "inspect", args.image))[0]["Id"],
        "run_flags": {"cpus": 2, "memory": "2g", "read_only": True, "uid": 1000,
                      "tmpfs": "512m", "pids_limit": 256},
        "configuration": {"fpm_workers": 4, "queue_capacity": 4,
                          "fpm_timeout_seconds": 2, "http_timeout_ms": 6000,
                          "max_body_bytes": 6291456},
        "source_sha256": source_hashes(),
        "gateways": {},
    }
    names = []
    failed = False

    def start(mode, profile, extra_env=()):
        name = f"bref-web-compare-{profile}-{uuid.uuid4().hex[:8]}"
        command = ["run", "--detach", "--rm", "--name", name,
                   "--read-only", "--tmpfs", "/tmp:rw,nosuid,nodev,size=512m,mode=1777",
                   "--cpus", "2", "--memory", "2g", "--pids-limit", "256",
                   "--publish", "127.0.0.1::8080",
                   "--env", f"BREF_WEB_GATEWAY={mode}",
                   "--env", "BREF_WEB_FPM_TIMEOUT_SECONDS=2",
                   "--env", "BREF_WEB_REQUEST_TIMEOUT_MS=6000"]
        for setting in extra_env:
            command += ["--env", setting]
        docker(*command, args.image)
        names.append(name)
        port = json.loads(docker("inspect", name))[0]["NetworkSettings"]["Ports"]["8080/tcp"][0]["HostPort"]
        url = f"http://127.0.0.1:{port}"
        ready(url, name)
        metadata["gateways"][profile] = {"url": url, "container": name}
        print(f"{profile}: ready", flush=True)
        return url

    try:
        for mode in ("rust", "caddy"):
            start(mode, mode)

        reference = names[0]
        metadata["binary_bytes"] = json.loads(docker("exec", reference, "/var/lang/bin/node", "-e",
            "const fs=require('fs'); console.log(JSON.stringify(Object.fromEntries("
            "['/usr/local/bin/bref-web-gateway','/usr/local/bin/caddy','/var/lang/bin/node','/opt/bin/php-fpm']"
            ".map(p=>[p,fs.statSync(p).size]))));"))
        metadata["versions"] = {
            "node": docker("exec", reference, "/var/lang/bin/node", "--version"),
            "php": docker("exec", reference, "/opt/bin/php", "-r", "echo PHP_VERSION;"),
            "caddy": docker("exec", reference, "/usr/local/bin/caddy", "version"),
        }
        metadata["php_fixture_lint"] = docker("exec", reference, "/opt/bin/php", "-l", "/var/task/public/index.php")
        metadata["idle_memory"] = [json.loads(line) for line in docker(
            "stats", "--no-stream", "--format", "{{json .}}", *names).splitlines()]
        metadata["memory_measurement"] = {
            "valid_for_comparison": False,
            "reason": "Raw Docker stats only. Verify cgroup isolation before using these as per-container measurements; this workspace reports a shared parent cgroup."
        }

        for mode in ("rust", "caddy"):
            url = metadata["gateways"][mode]["url"]
            for kind in (("integration",) if args.skip_benchmark else ("integration", "benchmark")):
                output = args.output / f"{mode}-{kind}.json"
                command = ["tests/integration/run.py" if kind == "integration" else "tests/integration/benchmark.py",
                           "--base-url", url, "--label", mode, "--output", str(output)]
                if kind == "benchmark":
                    command += ["--iterations", str(args.iterations), "--concurrency"]
                    command += [str(value) for value in args.concurrency]
                code = run_harness(command, args.output / f"{mode}-{kind}.log")
                failed |= code != 0
                print(f"{mode} {kind}: {'PASS' if code == 0 else 'FAIL'} ({output})", flush=True)
                metadata["gateways"][mode][kind + "_exit_code"] = code

        url = start("rust", "lifecycle", ["BREF_WEB_MAX_IN_FLIGHT=1",
                     "BREF_WEB_QUEUE_CAPACITY=0", "BREF_WEB_REQUEST_TIMEOUT_MS=150"])
        code = run_harness(["tests/lifecycle/run.py", "--base-url", url, "--output",
                            str(args.output / "rust-lifecycle.json")], args.output / "rust-lifecycle.log")
        failed |= code != 0
        metadata["gateways"]["lifecycle"]["exit_code"] = code
        print(f"rust lifecycle: {'PASS' if code == 0 else 'FAIL'}", flush=True)
    finally:
        for name in names:
            try:
                (args.output / (name.split("-")[3] + "-container.log")).write_text(docker("logs", name))
            finally:
                subprocess.run(DOCKER + ["stop", "--time", "2", name], env=ENV,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        (args.output / "environment.json").write_text(json.dumps(metadata, indent=2) + "\n")
    return int(failed)


if __name__ == "__main__":
    sys.exit(main())
