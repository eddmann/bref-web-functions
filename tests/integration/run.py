#!/usr/bin/env python3
"""Local HTTP integration contract shared by both prototype gateways (stdlib only)."""

import argparse
import concurrent.futures
import datetime
import fcntl
import hashlib
import http.client
import ipaddress
import json
import pathlib
import os
import sys
import time
import tempfile
import urllib.parse
from http.cookies import SimpleCookie


class HarnessLock:
    """Serialize both gateways' test/benchmark runs on this machine."""

    def __enter__(self):
        path = os.path.join(tempfile.gettempdir(), "bref-web-functions-harness.lock")
        self.fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Waiting for another local integration/benchmark run to finish...", file=sys.stderr, flush=True)
            fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *_):
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)


def local_target(base_url):
    url = urllib.parse.urlsplit(base_url)
    if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password:
        raise ValueError("--base-url must be an HTTP(S) loopback URL without credentials")
    if url.path not in ("", "/") or url.query or url.fragment:
        raise ValueError("--base-url must not have a path, query or fragment")
    try:
        local = ipaddress.ip_address(url.hostname).is_loopback
    except ValueError:
        local = url.hostname == "localhost"
    if not local:
        raise ValueError("Only loopback targets are allowed by this local fixture harness")
    return url


class Client:
    def __init__(self, base_url, timeout=8):
        self.url = local_target(base_url)
        self.timeout = timeout

    def connection(self):
        cls = http.client.HTTPSConnection if self.url.scheme == "https" else http.client.HTTPConnection
        return cls(self.url.hostname, self.url.port, timeout=self.timeout)

    def request(self, path, method="GET", body=None, headers=None, encode_chunked=False):
        connection = self.connection()
        start = time.monotonic()
        try:
            connection.request(method, path, body=body, headers=headers or {}, encode_chunked=encode_chunked)
            response = connection.getresponse()
            data = response.read()
            return {"status": response.status, "headers": response.getheaders(), "body": data,
                    "seconds": time.monotonic() - start}
        finally:
            connection.close()

    def json(self, path, **kwargs):
        response = self.request(path, **kwargs)
        check(response["status"] == 200, f"Expected HTTP 200, got {response['status']}: {response['body'][:300]!r}")
        return json.loads(response["body"])


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


class Suite:
    def __init__(self, client, label, skip_destructive=False):
        self.client = client
        self.label = label
        self.results = []
        self.skip_destructive = skip_destructive

    def case(self, name, test, destructive=False):
        start = time.monotonic()
        if destructive and self.skip_destructive:
            item = {"name": name, "status": "skipped", "reason": "Explicit --skip-destructive selection"}
        else:
            try:
                detail = test()
                item = {"name": name, "status": "passed", "details": detail or {}}
            except Exception as error:
                item = {"name": name, "status": "failed", "error": f"{type(error).__name__}: {error}"}
        item["seconds"] = round(time.monotonic() - start, 6)
        self.results.append(item)
        print(f"{item['status'].upper():7} {name}" + (f": {item['error']}" if "error" in item else ""), flush=True)

    def run(self):
        self.case("php_health", self.health)
        self.case("encoded_uri_query_and_headers", self.uri)
        self.case("forwarded_https_and_request_context", self.context)
        self.case("binary_put_and_patch", self.methods)
        self.case("form_post", self.form)
        self.case("multipart_upload", self.multipart)
        self.case("chunked_upload_small", lambda: self.chunked_upload(65536))
        self.case("chunked_upload_exact_limit", lambda: self.chunked_upload(6 * 1024 * 1024))
        self.case("chunked_upload_over_limit", lambda: self.chunked_upload(6 * 1024 * 1024 + 1, oversized=True))
        self.case("application_411_get_preserved", self.application_411)
        self.case("application_411_chunked_put_preserved", lambda: self.application_411(chunked=True))
        self.case("binary_response", self.binary)
        self.case("repeated_set_cookie", self.cookies)
        self.case("streaming_chunks", self.streaming)
        self.case("sequential_php_state_reset", self.isolation)
        self.case("concurrent_request_context_isolation", self.concurrent)
        self.case("bounded_large_response_integrity", self.large_response)
        self.case("session_cookie_roundtrip", self.session)
        self.case("fatal_error_and_recovery", self.fatal, destructive=True)
        self.case("worker_timeout_isolated_from_healthy_request", self.timeout, destructive=True)
        self.case("worker_crash_isolated_from_other_requests", self.crash, destructive=True)
        self.case("request_body_limit", self.body_limit)
        return self.results

    def health(self):
        result = self.client.json("/health")
        check(result.get("ok") is True and isinstance(result.get("pid"), int), f"Bad health response: {result}")
        return result

    def uri(self):
        path = "/echo/a%2Fb?dup=one&dup=two&arr%5B%5D=1&arr%5B%5D=2&space=a%20b&plus=x%2By"
        result = self.client.json(path, headers={"X-Fixture-Probe": "literal value", "Authorization": "Basic Zml4dHVyZTpsb2NhbA=="})
        check(result["raw_uri"] == path, f"Raw URI changed: {result['raw_uri']!r}")
        check(result["query_string"] == path.split("?", 1)[1], "Raw query string changed")
        check(result["method"] == "GET", "Method changed")
        check(result["get"] == {"dup": "two", "arr": ["1", "2"], "space": "a b", "plus": "x+y"}, f"Incorrect PHP query parsing: {result['get']}")
        check(result["headers"].get("x-fixture-probe") == "literal value", "Custom header missing")
        check(result["headers"].get("authorization") == "Basic Zml4dHVyZTpsb2NhbA==", "Authorization header lost")
        return {"raw_uri": result["raw_uri"], "cgi": result["cgi"]}

    def context(self):
        identity = "fixture-context-https"
        result = self.client.json("/echo", headers={"lambda-web-request-id": identity, "X-Forwarded-Proto": "https"})
        check(result["request_id"] == identity, f"Request ID was not mapped per request: {result['request_id']!r}")
        check(str(result["cgi"].get("HTTPS", "")).lower() in ("on", "1", "true"), f"HTTPS CGI was not normalized: {result['cgi']}")
        return {"cgi": result["cgi"]}

    def methods(self):
        body = bytes(range(256)) * 32
        for method in ("PUT", "PATCH"):
            result = self.client.json("/echo?method=" + method, method=method, body=body,
                                      headers={"Content-Type": "application/octet-stream"})
            check(result["method"] == method, f"{method} became {result['method']}")
            check(result["body_size"] == len(body) and result["body_sha256"] == digest(body), f"{method} body corrupted")
        return {"methods": ["PUT", "PATCH"], "body_bytes": len(body)}

    def form(self):
        body = b"message=hello+world&items%5B%5D=one&items%5B%5D=two"
        result = self.client.json("/echo", method="POST", body=body,
                                  headers={"Content-Type": "application/x-www-form-urlencoded"})
        check(result["post"] == {"message": "hello world", "items": ["one", "two"]}, f"POST fields missing: {result['post']}")
        check(result["body_sha256"] == digest(body), "Raw form body changed")

    def multipart(self):
        boundary = "bref-local-fixture-boundary"
        content = bytes(range(256)) * 16
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="message"\r\n\r\nhello\r\n'
                f'--{boundary}\r\nContent-Disposition: form-data; name="upload"; filename="fixture.bin"\r\n'
                'Content-Type: application/octet-stream\r\n\r\n').encode() + content + f"\r\n--{boundary}--\r\n".encode()
        result = self.client.json("/echo", method="POST", body=body,
                                  headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        check(result["post"] == {"message": "hello"}, f"Multipart form parsing failed: {result['post']}")
        upload = result["files"].get("upload", {})
        check(upload.get("error") == 0 and upload.get("size") == len(content) and upload.get("sha256") == digest(content),
              f"Multipart upload corrupted: {upload}")
        return {"file_bytes": len(content), "sha256": digest(content)}

    def binary(self):
        response = self.client.request("/binary")
        expected = bytes(range(256)) * 16
        check(response["status"] == 200 and response["body"] == expected, "Binary response corrupted")
        return {"bytes": len(expected), "sha256": digest(expected)}

    @staticmethod
    def upload_chunks(total):
        block = bytes(range(256)) * 256
        remaining = total
        while remaining:
            size = min(len(block), remaining)
            yield block[:size]
            remaining -= size

    def chunked_upload(self, total, oversized=False):
        expected = hashlib.sha256()
        for block in self.upload_chunks(total):
            expected.update(block)
        try:
            response = self.client.request("/echo?chunked=1", method="POST", body=self.upload_chunks(total),
                                           headers={"Content-Type": "application/octet-stream", "Transfer-Encoding": "chunked"},
                                           encode_chunked=True)
        except (OSError, http.client.HTTPException) as error:
            raise AssertionError(f"Chunked upload of {total} bytes ended with transport close/error, not an HTTP response: "
                                 f"{type(error).__name__}: {error}; expected {'413' if oversized else '200'}") from error
        if oversized:
            check(response["status"] == 413, f"Over-limit chunked upload must return HTTP 413, got {response['status']}")
            return {"bytes": total, "status": response["status"], "framing": "chunked; no Content-Length"}
        check(response["status"] == 200, f"Valid {total}-byte chunked upload returned HTTP {response['status']}: {response['body'][:300]!r}")
        result = json.loads(response["body"])
        check(result["body_size"] == total and result["body_sha256"] == expected.hexdigest(),
              f"Chunked upload corrupted: got {result['body_size']} bytes / {result['body_sha256']}, expected {total} / {expected.hexdigest()}")
        check(result["method"] == "POST", "Chunked request method changed")
        return {"bytes": total, "sha256": expected.hexdigest(), "framing": "chunked; no Content-Length"}

    def application_411(self, chunked=False):
        body = bytes(range(256)) * 256 if chunked else b""
        method = "PUT" if chunked else "GET"
        headers = {"Transfer-Encoding": "chunked", "Content-Type": "application/octet-stream"} if chunked else {}
        response = self.client.request("/application-411", method=method,
                                       body=self.upload_chunks(len(body)) if chunked else None,
                                       headers=headers, encode_chunked=chunked)
        returned_headers = {key.lower(): value for key, value in response["headers"]}
        check(response["status"] == 411, f"Genuine application 411 changed to {response['status']} for {method}")
        check(returned_headers.get("x-fixture-application") == "intentional-411",
              f"Application 411 marker missing for {method}: {response['headers']}")
        check(returned_headers.get("x-fixture-method") == method, f"Application method marker changed for {method}")
        check(returned_headers.get("x-fixture-body-sha256") == digest(body), f"Application 411 input body changed for {method}")
        check(response["body"] == b"fixture application 411\n", f"Application 411 response body replaced for {method}: {response['body']!r}")
        return {"method": method, "status": response["status"], "input_bytes": len(body),
                "framing": "chunked; no Content-Length" if chunked else "ordinary GET", "application_marker": "intentional-411"}

    def cookies(self):
        response = self.client.request("/cookies")
        cookies = [value for key, value in response["headers"] if key.lower() == "set-cookie"]
        check(response["status"] == 200 and len(cookies) == 2, f"Repeated Set-Cookie headers collapsed: {cookies}")
        check(any(value.startswith("first=one;") for value in cookies) and any(value.startswith("second=two;") for value in cookies),
              f"Unexpected Set-Cookie values: {cookies}")
        return {"set_cookie": cookies}

    def streaming(self):
        connection = self.client.connection()
        start = time.monotonic()
        times, lines = [], []
        try:
            connection.request("GET", "/stream")
            response = connection.getresponse()
            check(response.status == 200, f"Stream returned {response.status}")
            for _ in range(3):
                lines.append(response.readline())
                times.append(time.monotonic() - start)
            check(response.read() == b"", "Unexpected stream suffix")
        finally:
            connection.close()
        check(lines == [b"chunk-1\n", b"chunk-2\n", b"chunk-3\n"], f"Wrong chunks: {lines!r}")
        check(times[2] - times[0] >= .25, f"Stream appears buffered: arrival seconds {times}")
        check(times[0] < times[2] - .25, f"First chunk arrived only at completion: {times}")
        return {"chunk_arrival_seconds": [round(value, 6) for value in times]}

    def isolation(self):
        pids = []
        for number in range(16):
            identity = f"fixture-sequential-{number}"
            result = self.client.json(f"/isolation?id={identity}", headers={"lambda-web-request-id": identity})
            self.check_isolation(result, identity)
            pids.append(result["pid"])
        check(len(set(pids)) < len(pids), f"No worker was reused; cannot establish reset within the same PHP PID: {pids}")
        return {"pids": pids, "unique_pids": len(set(pids))}

    @staticmethod
    def check_isolation(result, identity):
        check(result["static_counter"] == 1 and result["global_counter"] == 1 and result["previous_context"] is None,
              f"PHP state leaked between requests: {result}")
        check(result["context"] == identity and result["request_id"] == identity and result["id"] == identity,
              f"Request context crossed requests: {result}, expected {identity}")

    def concurrent(self):
        def invoke(number):
            identity = f"fixture-concurrent-{number}"
            result = self.client.json(f"/isolation?id={identity}&sleep_ms=100", headers={"lambda-web-request-id": identity})
            self.check_isolation(result, identity)
            return result["pid"]
        start = time.monotonic()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            pids = list(executor.map(invoke, range(24)))
        check(len(set(pids)) >= 2, f"Concurrent requests used one PHP worker only: {pids}")
        return {"requests": 24, "client_concurrency": 8, "php_pids": sorted(set(pids)), "seconds": time.monotonic() - start}

    def large_response(self):
        response = self.client.request("/large-response")
        expected_digest = digest(bytes(range(256)) * 16384)
        check(response["status"] == 200 and len(response["body"]) == 4 * 1024 * 1024, "Large response truncated")
        check(digest(response["body"]) == expected_digest, "Large response digest changed")
        return {"bytes": len(response["body"]), "sha256": expected_digest}

    def session(self):
        first = self.client.request("/session")
        check(first["status"] == 200 and json.loads(first["body"])["count"] == 1, "First session request failed")
        cookie = SimpleCookie()
        for key, value in first["headers"]:
            if key.lower() == "set-cookie":
                cookie.load(value)
        check("BREFWEBFIXTURE" in cookie, "Session cookie missing")
        value = cookie["BREFWEBFIXTURE"].value
        second = self.client.json("/session", headers={"Cookie": "BREFWEBFIXTURE=" + value})
        check(second["count"] == 2, f"Session did not survive a local request round trip: {second}")
        return {"counts": [1, second["count"]], "note": "Only tests one environment's temporary file sessions; not durable/shared storage."}

    def fatal(self):
        response = self.client.request("/fatal")
        check(response["status"] == 500, f"PHP fatal did not produce HTTP 500: {response['status']}")
        return {"fatal_status": response["status"], "recovery": self.health()}

    def timeout(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(self.client.request, "/hang")
            time.sleep(.15)
            started = time.monotonic()
            self.health()
            healthy_seconds = time.monotonic() - started
            response = future.result(timeout=7)
        check(healthy_seconds < .5, f"A hanging worker blocked a healthy request for {healthy_seconds:.3f}s")
        check(response["status"] in (502, 504) and response["seconds"] < 4,
              f"Expected configured 2s worker timeout and gateway error; got {response['status']} after {response['seconds']:.3f}s")
        return {"hang_status": response["status"], "hang_seconds": response["seconds"],
                "healthy_seconds": healthy_seconds, "recovery": self.health()}

    def crash(self):
        def overlapping(number):
            identity = f"fixture-crash-overlap-{number}"
            result = self.client.json(f"/isolation?id={identity}&sleep_ms=350", headers={"lambda-web-request-id": identity})
            self.check_isolation(result, identity)
            return result
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            pending = [executor.submit(overlapping, number) for number in range(2)]
            time.sleep(.06)
            response = self.client.request("/crash")
            check(response["status"] != 501, "Real worker-crash test unsupported: PHP posix extension missing (not counted as a pass)")
            check(response["status"] in (502, 503, 504), f"Unexpected worker-crash status: {response['status']}")
            results = [future.result(timeout=4) for future in pending]
        return {"crash_status": response["status"], "unaffected_requests": len(results), "recovery": self.health()}

    def body_limit(self):
        connection = self.client.connection()
        total = 6 * 1024 * 1024 + 1
        try:
            connection.putrequest("POST", "/echo")
            connection.putheader("Content-Type", "application/octet-stream")
            connection.putheader("Content-Length", str(total))
            connection.endheaders()
            remaining = total
            try:
                while remaining:
                    size = min(65536, remaining)
                    connection.send(b"x" * size)
                    remaining -= size
            except (BrokenPipeError, ConnectionResetError):
                # A server may reject Content-Length before consuming the full body.
                pass
            response = connection.getresponse()
            response.read(8192)
            check(response.status == 413, f"Expected configured 6MiB limit HTTP 413, got {response.status}")
            return {"declared_bytes": total, "status": response.status}
        finally:
            connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--label", default="gateway")
    parser.add_argument("--output", type=pathlib.Path)
    parser.add_argument("--skip-destructive", action="store_true", help="Explicitly skip timeout/crash/fatal cases; recorded as skipped")
    args = parser.parse_args()
    try:
        suite = Suite(Client(args.base_url), args.label, args.skip_destructive)
    except ValueError as error:
        parser.error(str(error))
    with HarnessLock():
        started = time.monotonic()
        results = suite.run()
        report = {"label": args.label, "base_url": args.base_url,
                  "timestamp_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  "scope": "Local fixture integration only; not AWS validation or a production certification.",
                  "seconds": round(time.monotonic() - started, 6), "tests": results,
                  "passed": sum(item["status"] == "passed" for item in results),
                  "failed": sum(item["status"] == "failed" for item in results),
                  "skipped": sum(item["status"] == "skipped" for item in results)}
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(f"{args.label}: {report['passed']} passed, {report['failed']} failed, {report['skipped']} skipped", flush=True)
    return 1 if report["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
