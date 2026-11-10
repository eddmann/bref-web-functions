#!/usr/bin/env python3
"""Native process/network checks for runtime retirement and stalled HTTP clients."""

import argparse
import datetime
import http.client
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests/integration"))
from run import HarnessLock, check


def free_port():
    with socket.socket() as stream:
        stream.bind(("127.0.0.1", 0))
        return stream.getsockname()[1]


def alive(pid):
    try:
        # A dead child awaiting its OS reaper cannot execute or hold capacity.
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        return state not in {"Z", "X"}
    except FileNotFoundError:
        return False


def wait_until(predicate, timeout, message):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.01)
    raise AssertionError(message)


class Runtime:
    def __init__(self, binary, node, directory, log, workers=1, http_ms=300, fpm_seconds=3, queue=0):
        self.directory = directory
        self.log = log
        self.port, self.fpm_port = free_port(), free_port()
        while self.fpm_port == self.port:
            self.fpm_port = free_port()
        self.pids_path = directory / "pids.json"
        self.events_path = directory / "events.jsonl"
        self.release_path = directory / "release-drain"
        fake = directory / "fake-fpm.py"
        fake.write_bytes(Path(__file__).with_name("fake_fpm.py").read_bytes())
        fake.chmod(0o755)
        environment = dict(os.environ)
        environment.update({
            "BREF_WEB_RUST_BIN": str(binary),
            "BREF_WEB_FPM_BIN": str(fake), "BREF_WEB_FPM_CONFIG": str(directory / "unused-fpm.conf"),
            "BREF_WEB_FPM_ADDR": f"127.0.0.1:{self.fpm_port}",
            "AWS_LAMBDA_HTTP_ENDPOINT": f"127.0.0.1:{self.port}",
            "BREF_WEB_MAX_IN_FLIGHT": str(workers), "BREF_WEB_QUEUE_CAPACITY": str(queue),
            "BREF_WEB_QUEUE_TIMEOUT_MS": "1000", "BREF_WEB_MAX_BODY_BYTES": "6291456",
            "BREF_WEB_REQUEST_TIMEOUT_MS": str(http_ms), "BREF_WEB_FPM_TIMEOUT_SECONDS": str(fpm_seconds),
            "BREF_WEB_SHUTDOWN_GRACE_MS": "500", "BREF_WEB_SHUTDOWN_TIMEOUT_MS": "900",
            "BREF_WEB_STARTUP_TIMEOUT_MS": "8000", "HARDENING_PIDS": str(self.pids_path),
            "HARDENING_EVENTS": str(self.events_path), "HARDENING_RELEASE": str(self.release_path),
        })
        self.output = log.open("w")
        self.process = subprocess.Popen([node, str(ROOT / "runtime/launcher/index.js")],
                                        env=environment, stdin=subprocess.DEVNULL,
                                        stdout=self.output, stderr=subprocess.STDOUT,
                                        start_new_session=True)
        self.pids = None

    def __enter__(self):
        try:
            wait_until(self.ready, 8, "Runtime did not become ready")
            self.pids = json.loads(self.pids_path.read_text())
            return self
        except Exception:
            self.close()
            raise

    def ready(self):
        if self.process.poll() is not None:
            raise AssertionError(f"Node exited during startup: {self.log.read_text()[-3000:]}")
        try:
            return self.request("/__bref_web/health")[0] == 200
        except (OSError, http.client.HTTPException):
            return False

    def request(self, path="/health", timeout=1):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def identity(self):
        # Reading a completed HTTP body does not imply FastCGI END_REQUEST has
        # released its backend slot. Obtain setup identity without dispatching PHP.
        return {"ok": True, "fake_fpm_pid": self.pids["master_pid"],
                "rust_pid": self.pids["rust_pid"]}

    def events(self):
        if not self.events_path.exists():
            return []
        text = self.events_path.read_text()
        lines = text.splitlines()
        if not text.endswith("\n"):
            lines = lines[:-1]
        return [json.loads(line) for line in lines if line]

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=2)
        if self.pids_path.exists():
            pids = json.loads(self.pids_path.read_text())
            if any(alive(pids[key]) for key in ("master_pid", "descendant_pid")):
                try:
                    os.killpg(pids["group_id"], signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if alive(pids["rust_pid"]):
                try:
                    os.kill(pids["rust_pid"], signal.SIGKILL)
                except ProcessLookupError:
                    pass
        self.output.close()

    def __exit__(self, *_):
        self.close()


class Suite:
    def __init__(self, binary, node, output):
        self.binary, self.node, self.output = binary, node, output
        self.results = []
        self.logs = output.parent / (output.stem + "-logs")
        self.logs.mkdir(parents=True, exist_ok=True)

    def case(self, name, test):
        started = time.monotonic()
        log = self.logs / f"{name}.log"
        try:
            with tempfile.TemporaryDirectory(prefix="bref-web-hardening-") as temporary:
                detail = test(Path(temporary), log)
            result = {"name": name, "status": "passed", "details": detail}
        except Exception as error:
            result = {"name": name, "status": "failed", "error": f"{type(error).__name__}: {error}"}
            if log.exists():
                result["log_tail"] = log.read_text()[-4000:]
        result.update({"seconds": round(time.monotonic() - started, 6), "log": str(log)})
        self.results.append(result)
        print(f"{result['status'].upper():7} {name}" + (f": {result['error']}" if "error" in result else ""), flush=True)

    def retirement(self, directory, log):
        with Runtime(self.binary, self.node, directory, log, workers=2, http_ms=2500, fpm_seconds=1) as runtime:
            original = runtime.identity()
            started = time.monotonic()
            status, _ = runtime.request("/never-end", timeout=4)
            check(status == 504, f"Expected original HTTP deadline to produce 504, got {status}")
            connection = http.client.HTTPConnection("127.0.0.1", runtime.port, timeout=4)
            try:
                connection.request("GET", "/drain")
                response = connection.getresponse()
                check(response.status == 200, f"Healthy drain request returned {response.status}")
                check(response.readline() == b"drain-ack\n", "Drain acknowledgement was not streamed")
                wait_until(lambda: "unresolved_backend=true" in log.read_text(), 4,
                           "Backend accounting expiry was not reported")
                retirement_at = time.monotonic()
                runtime.release_path.touch()
                check(response.read() == b"drain-done\n", "An in-flight healthy response was lost during bounded drain")
            finally:
                connection.close()
            exit_code = runtime.process.wait(timeout=2)
            check(exit_code != 0, "Unconfirmed backend work did not produce a nonzero supervisor exit")
            elapsed = time.monotonic() - started
            check(4 <= elapsed < 7, f"Retirement was not bounded by accounting plus shutdown budgets: {elapsed:.3f}s")
            wait_until(lambda: not any(alive(runtime.pids[key]) for key in ("master_pid", "descendant_pid", "rust_pid")),
                       1, "Runtime exited with live Rust/FPM process-group descendants")
            try:
                with socket.create_connection(("127.0.0.1", runtime.port), timeout=.2):
                    raise AssertionError("Retired runtime still accepts HTTP connections")
            except (ConnectionRefusedError, TimeoutError):
                pass
            return {"exit_code": exit_code, "seconds_to_retire": elapsed,
                    "seconds_from_retirement_to_exit": time.monotonic() - retirement_at,
                    "original_runtime": original, "owned_processes_terminated": runtime.pids,
                    "healthy_in_flight_response_completed": True}

    def stalled(self, directory, log, pipeline=False):
        with Runtime(self.binary, self.node, directory, log) as runtime:
            original = runtime.identity()
            slow = socket.socket()
            slow.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
            slow.settimeout(1)
            stop = threading.Event()
            writer = None
            pipeline_writes = []
            started = time.monotonic()
            try:
                slow.connect(("127.0.0.1", runtime.port))
                slow.sendall(b"GET /large HTTP/1.1\r\nHost: localhost\r\nConnection: keep-alive\r\n\r\n")
                head = bytearray()
                while not head.endswith(b"\r\n\r\n"):
                    data = slow.recv(1)
                    check(data, "Server closed before response headers arrived")
                    head.extend(data)
                    check(len(head) < 65536, "Unexpectedly large response headers")
                check(head.startswith(b"HTTP/1.1 200"), f"Large response failed: {head!r}")
                # The client deliberately performs no more reads until cleanup.
                status, _ = runtime.request()
                check(status == 503, f"Stalled response did not initially hold its backend reservation: {status}")

                def send_pipeline():
                    deadline = started + 2
                    while not stop.is_set() and time.monotonic() < deadline:
                        try:
                            slow.sendall(b"GET /pipelined HTTP/1.1\r\nHost: localhost\r\n\r\n")
                            pipeline_writes.append(time.monotonic() - started)
                        except OSError:
                            return
                        stop.wait(.04)

                if pipeline:
                    writer = threading.Thread(target=send_pipeline, daemon=True)
                    writer.start()
                probes = []
                recovered = None
                while time.monotonic() - started < 1.5:
                    status, body = runtime.request()
                    elapsed = time.monotonic() - started
                    probes.append({"seconds": round(elapsed, 6), "status": status})
                    if status == 200:
                        recovered = json.loads(body)
                        break
                    check(status == 503, f"Unexpected response during backend recovery: {status}")
                    time.sleep(.02)
                check(recovered is not None, f"Stalled reader retained backend capacity past its deadline: {probes}")
                check(recovered == original and runtime.process.poll() is None,
                      "Recovery restarted/poisoned the runtime instead of draining the completed backend")
                wait_until(lambda: any(item["kind"] == "large_completed" for item in runtime.events()),
                           .5, "Backend did not reach actual completion")
                events = runtime.events()
                check(not any(item.get("path") == "/pipelined" for item in events),
                      "A pipelined request was dispatched while the previous response remained blocked")
                return {"http_timeout_ms": 300, "flush_grace_ms": 100, "admission_probes": probes,
                        "same_runtime": recovered, "pipelined_writes": pipeline_writes,
                        "client_never_resumed_reading": True, "backend_completed": True}
            finally:
                stop.set()
                slow.close()
                if writer is not None:
                    writer.join(timeout=2)

    def keepalive(self, directory, log):
        # This case exercises connection deadlines. Allow a queued request while
        # the previous response's FastCGI END_REQUEST releases its worker slot.
        with Runtime(self.binary, self.node, directory, log, queue=1) as runtime:
            connection = http.client.HTTPConnection("127.0.0.1", runtime.port, timeout=1)
            started = time.monotonic()
            first_socket = None
            identities = []
            try:
                for _ in range(8):
                    connection.request("GET", "/health")
                    response = connection.getresponse()
                    check(response.status == 200, f"Keepalive request returned {response.status}")
                    identities.append(json.loads(response.read()))
                    check(connection.sock is not None, "Server closed the keepalive connection")
                    if first_socket is None:
                        first_socket = connection.sock
                    check(connection.sock is first_socket, "HTTP client silently opened a replacement connection")
                    time.sleep(.1)
                refreshed_elapsed = time.monotonic() - started
                check(refreshed_elapsed > .6 and all(identity == identities[0] for identity in identities),
                      "Keepalive requests did not exercise refreshed deadlines in one runtime")
                # The most recent request also bounds subsequent idle time. Use
                # the retained socket directly so HTTPConnection cannot reconnect.
                time.sleep(.45)
                first_socket.settimeout(.35)
                try:
                    trailing = first_socket.recv(1)
                except ConnectionResetError:
                    trailing = b""
                except socket.timeout as error:
                    raise AssertionError("Last request's connection deadline did not close the idle connection") from error
                check(trailing == b"", f"Unexpected bytes after the final keepalive response: {trailing!r}")
            finally:
                connection.close()
            elapsed = time.monotonic() - started
            return {"requests": len(identities), "seconds": elapsed, "http_timeout_ms": 300,
                    "one_tcp_connection": True, "same_runtime": identities[0],
                    "idle_connection_closed_after_last_deadline": True}

    def run(self):
        self.case("unconfirmed_backend_retires_runtime_and_drains_healthy_work", self.retirement)
        self.case("stalled_reader_releases_backend_without_runtime_restart", self.stalled)
        self.case("pipelined_requests_cannot_extend_stalled_response_deadline",
                  lambda directory, log: self.stalled(directory, log, pipeline=True))
        self.case("keepalive_requests_refresh_connection_deadlines", self.keepalive)
        return self.results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True, type=Path, help="Native static Rust gateway executable")
    parser.add_argument("--node", default=shutil.which("node"))
    parser.add_argument("--output", type=Path, default=ROOT / "results/hardening.json")
    args = parser.parse_args()
    args.binary = args.binary.resolve()
    args.output = args.output.resolve()
    if sys.platform != "linux" or not Path("/proc/self/stat").exists():
        parser.error("Linux with /proc is required for strict process-cleanup verification")
    if not args.binary.is_file() or not os.access(args.binary, os.X_OK) or not args.node:
        parser.error("A native executable gateway and Node.js are required")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with HarnessLock():
        started = time.monotonic()
        results = Suite(args.binary, args.node, args.output).run()
        report = {"timestamp_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  "scope": "Native loopback/process regression tests with a fake FastCGI peer; no AWS validation.",
                  "binary": str(args.binary), "tests": results, "seconds": time.monotonic() - started,
                  "passed": sum(item["status"] == "passed" for item in results),
                  "failed": sum(item["status"] == "failed" for item in results)}
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(f"hardening: {report['passed']} passed, {report['failed']} failed", flush=True)
    return int(report["failed"] > 0)


if __name__ == "__main__":
    sys.exit(main())
