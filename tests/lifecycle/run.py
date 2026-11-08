#!/usr/bin/env python3
"""Dedicated local Rust lifecycle contract; see README.md for required settings."""

import argparse
import datetime
import json
import pathlib
import socket
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "integration"))
from run import Client, HarnessLock, check


class LifecycleSuite:
    def __init__(self, client):
        self.client = client
        self.results = []

    def case(self, name, test):
        started = time.monotonic()
        try:
            details = test()
            result = {"name": name, "status": "passed", "details": details}
        except Exception as error:
            result = {"name": name, "status": "failed", "error": f"{type(error).__name__}: {error}"}
        result["seconds"] = round(time.monotonic() - started, 6)
        self.results.append(result)
        print(f"{result['status'].upper():7} {name}" + (f": {result['error']}" if "error" in result else ""), flush=True)

    def wait_for_php(self, started, minimum_seconds, maximum_seconds):
        """Every probe before recovery must be rejected at admission with HTTP 503."""
        probes = []
        first = True
        while time.monotonic() - started < maximum_seconds:
            response = self.client.request("/health")
            elapsed = time.monotonic() - started
            probes.append({"seconds": round(elapsed, 6), "status": response["status"]})
            if first:
                check(response["status"] == 503,
                      f"Worker reservation was not retained immediately after frontend completion: {probes}")
                first = False
            if response["status"] == 200:
                check(elapsed >= minimum_seconds,
                      f"PHP capacity returned before backend work could finish ({elapsed:.3f}s): {probes}")
                health = json.loads(response["body"])
                check(health.get("ok") is True and isinstance(health.get("pid"), int), f"Invalid PHP recovery health: {health}")
                return health, probes
            check(response["status"] == 503,
                  f"A reserved PHP worker admitted/queued another request instead of returning HTTP 503: {probes}")
            check(response["seconds"] < .5,
                  f"Admission rejection waited {response['seconds']:.3f}s; expected immediate 503 with no queue")
            time.sleep(.04)
        raise AssertionError(f"PHP did not recover within {maximum_seconds}s; observed admission probes: {probes}")

    def timeout_retains_worker(self):
        before = self.client.json("/health")
        started = time.monotonic()
        response = self.client.request("/hang", headers={"lambda-web-request-id": "fixture-lifecycle-timeout"})
        check(response["status"] == 504,
              f"Expected frontend HTTP 504 before PHP termination, got {response['status']} after {response['seconds']:.3f}s")
        check(.08 <= response["seconds"] < .8,
              f"Expected a 150ms frontend timeout, measured {response['seconds']:.3f}s")
        recovered, probes = self.wait_for_php(started, minimum_seconds=1.5, maximum_seconds=4.5)
        check(recovered["pid"] != before["pid"],
              f"The hanging PHP worker was not replaced by its hard timeout: before={before['pid']} after={recovered['pid']}")
        return {"frontend_status": response["status"], "frontend_seconds": response["seconds"],
                "php_pid_before": before["pid"], "php_pid_after": recovered["pid"], "admission_probes": probes}

    def disconnect_retains_worker(self):
        before = self.client.json("/health")
        connection = self.client.connection()
        response = None
        started = time.monotonic()
        try:
            connection.request("GET", "/disconnect-work",
                               headers={"lambda-web-request-id": "fixture-lifecycle-disconnect"})
            response = connection.getresponse()
            check(response.status == 200, f"Acknowledgement route returned HTTP {response.status}")
            headers = {key.lower(): value for key, value in response.getheaders()}
            check(headers.get("x-fixture-application") == "disconnect-work-test-only", "Missing test-only acknowledgement marker")
            acknowledgement = response.readline()
            acknowledgement_seconds = time.monotonic() - started
            check(acknowledgement == b"disconnect-ack\n", f"Unexpected acknowledgement: {acknowledgement!r}")
            check(acknowledgement_seconds < .15,
                  f"Acknowledgement did not arrive before the configured 150ms frontend deadline: {acknowledgement_seconds:.3f}s")
            # Close both the socket and its HTTPResponse makefile. Closing only the
            # HTTPConnection can leave the response's socket reference alive.
            if connection.sock is not None:
                try:
                    connection.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        finally:
            if response is not None:
                response.close()
            connection.close()
        recovered, probes = self.wait_for_php(started, minimum_seconds=.75, maximum_seconds=3)
        check(recovered["pid"] == before["pid"],
              f"Expected normal completion in the same PHP worker, not a pool/worker restart: before={before['pid']} after={recovered['pid']}")
        return {"acknowledgement_seconds": acknowledgement_seconds, "php_pid_before": before["pid"],
                "php_pid_after": recovered["pid"], "admission_probes": probes}

    def run(self):
        self.case("frontend_timeout_retains_backend_reservation_until_php_termination", self.timeout_retains_worker)
        self.case("client_disconnect_retains_backend_reservation_until_php_completion", self.disconnect_retains_worker)
        return self.results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="Loopback URL of a dedicated Rust lifecycle container")
    parser.add_argument("--label", default="rust-lifecycle")
    parser.add_argument("--output", type=pathlib.Path)
    args = parser.parse_args()
    try:
        suite = LifecycleSuite(Client(args.base_url, timeout=4))
    except ValueError as error:
        parser.error(str(error))
    with HarnessLock():
        started = time.monotonic()
        results = suite.run()
        report = {"label": args.label, "base_url": args.base_url,
                  "timestamp_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  "scope": "Dedicated local Rust lifecycle test only; not AWS validation.",
                  "required_configuration": {"BREF_WEB_MAX_IN_FLIGHT": 1, "BREF_WEB_QUEUE_CAPACITY": 0,
                                             "BREF_WEB_REQUEST_TIMEOUT_MS": 150, "BREF_WEB_FPM_TIMEOUT_SECONDS": 2},
                  "seconds": round(time.monotonic() - started, 6), "tests": results,
                  "passed": sum(result["status"] == "passed" for result in results),
                  "failed": sum(result["status"] == "failed" for result in results)}
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(f"{args.label}: {report['passed']} passed, {report['failed']} failed", flush=True)
    return int(report["failed"] > 0)


if __name__ == "__main__":
    sys.exit(main())
