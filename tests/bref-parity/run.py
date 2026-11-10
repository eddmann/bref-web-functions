#!/usr/bin/env python3
"""Compare what PHP sees under Bref's FPM runtime and under this gateway.

The Bref side drives Bref's real FpmHandler (from tests/bref/vendor) with API
Gateway HTTP API v2 events against the image's stock Bref php-fpm.conf and INI.
The gateway side sends the equivalent HTTP requests through the Rust gateway.
Both run the same application: tests/bref plus this directory's public/ and
php/conf.d/. The report lists every difference; it is not a pass/fail suite.

Requires ./scripts/build.sh and tests/bref dependencies (see tests/bref/README.md).
"""

import argparse
import base64
import http.client
import json
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP = HERE.parent / "bref"
BOUNDARY = "XyZ"
BASE_HEADERS = {
    "host": "example.com", "x-forwarded-for": "198.51.100.9", "x-forwarded-proto": "https",
    "x-forwarded-port": "443", "user-agent": "parity/1", "accept": "*/*",
    "x-amzn-trace-id": "Root=1-trace",
}
MULTIPART = (f"--{BOUNDARY}\r\nContent-Disposition: form-data; name=\"f\"; filename=\"a.bin\"\r\n"
             f"Content-Type: application/octet-stream\r\n\r\n{'z' * 100}\r\n--{BOUNDARY}--\r\n")
CASES = {
    "get": {"method": "GET", "path": "/orders/a%2Fb", "query": "x=1&tag=a+z&dup=one&dup=two&arr[]=1&arr[]=2",
            "headers": {**BASE_HEADERS, "cookie": "a=1; b=2", "authorization": "Basic dTpw"}, "body": ""},
    "post_no_content_type": {"method": "POST", "path": "/form", "query": "", "headers": dict(BASE_HEADERS),
                             "body": "name=bref&x=2"},
    "post_form": {"method": "POST", "path": "/form", "query": "",
                  "headers": {**BASE_HEADERS, "content-type": "application/x-www-form-urlencoded"},
                  "body": "name=bref&x=2"},
    "multipart": {"method": "POST", "path": "/upload", "query": "",
                  "headers": {**BASE_HEADERS, "content-type": f"multipart/form-data; boundary={BOUNDARY}"},
                  "body": MULTIPART},
    "repeated_header": {"method": "GET", "path": "/", "query": "",
                        "headers": {**BASE_HEADERS, "accept-language": "en, fr"}, "body": ""},
}


def docker(*args, **kwargs):
    return subprocess.run(["docker", *args], check=True, capture_output=True, text=True, **kwargs)


def mounts():
    return ["-v", f"{APP}:/var/task:ro", "-v", f"{HERE / 'public'}:/var/task/public:ro",
            "-v", f"{HERE / 'php'}:/var/task/php:ro"]


def bref_side(image, work):
    result = docker("run", "--rm", "--user", "1000:1000", "--read-only",
                    "--tmpfs", "/tmp:rw,size=256m,mode=1777", "-e", "LAMBDA_TASK_ROOT=/var/task",
                    "-e", "BREF_RUNTIME=fpm", *mounts(), "-v", f"{work}:/parity:ro",
                    "--entrypoint", "/opt/bin/php", image, "/parity/bref-fpm-handler.php")
    return {name: {"status": item["status"], "headers": item["headers"], "cookies": item["cookies"],
                   "app": item["app"]} for name, item in json.loads(result.stdout).items()}


def gateway_side(image):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    name = f"bref-parity-{int(time.time() * 1000)}"
    # The image's /var/task holds runtime code, so mount only the application parts.
    docker("run", "-d", "--rm", "--name", name, "--read-only", "--tmpfs", "/tmp:rw,size=256m,mode=1777",
           "-p", f"127.0.0.1:{port}:8080",
           "-v", f"{APP / 'vendor'}:/var/task/vendor:ro", "-v", f"{APP / 'src'}:/var/task/src:ro",
           "-v", f"{APP / 'bootstrap.php'}:/var/task/bootstrap.php:ro",
           "-v", f"{HERE / 'public'}:/var/task/public:ro", "-v", f"{HERE / 'php'}:/var/task/php:ro", image)
    try:
        # Docker's port proxy accepts connections before the gateway listens, so
        # wait for an actual health response.
        for _ in range(100):
            try:
                probe = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
                probe.request("GET", "/__bref_web/health")
                if probe.getresponse().status == 200:
                    break
            except (OSError, http.client.HTTPException):
                pass
            time.sleep(0.1)
        else:
            raise RuntimeError("gateway did not become ready")
        out = {}
        for case, c in CASES.items():
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            url = c["path"] + ("?" + c["query"] if c["query"] else "")
            connection.putrequest(c["method"], url, skip_host=True, skip_accept_encoding=True)
            for key, value in c["headers"].items():
                connection.putheader(key, value)
            connection.putheader("lambda-web-request-id", f"req-{case}")
            body = c["body"].encode()
            if body or c["method"] == "POST":
                connection.putheader("content-length", str(len(body)))
            connection.endheaders(body or None)
            response = connection.getresponse()
            out[case] = {"status": response.status, "headers": response.getheaders(),
                         "app": json.loads(response.read())}
        return out
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def differences(bref, ours):
    report = {}
    for case in CASES:
        b, o = bref[case]["app"], ours[case]["app"]
        found = {}
        for section in ("server", "get", "post", "cookie", "files", "input_len", "ini"):
            x, y = b[section], o[section]
            if isinstance(x, dict) and isinstance(y, dict):
                for key in sorted(set(x) | set(y)):
                    if x.get(key) != y.get(key):
                        found[f"{section}.{key}"] = {"bref": x.get(key), "gateway": y.get(key)}
            elif x != y:
                found[section] = {"bref": x, "gateway": y}
        if bref[case]["status"] != ours[case]["status"]:
            found["status"] = {"bref": bref[case]["status"], "gateway": ours[case]["status"]}
        report[case] = found
    extensions = set(bref["get"]["app"]["extensions"]) ^ set(ours["get"]["app"]["extensions"])
    report["extensions_only_in_one"] = sorted(extensions)
    report["response_headers_get"] = {"bref_api_gateway_v2": {"headers": bref["get"]["headers"],
                                                              "cookies": bref["get"]["cookies"]},
                                      "gateway_http": ours["get"]["headers"]}
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="bref-web-functions:local")
    args = parser.parse_args()
    if not (APP / "vendor" / "bref" / "bref").is_dir():
        parser.exit(2, "Install tests/bref dependencies first (see tests/bref/README.md)\n")
    with tempfile.TemporaryDirectory() as directory:
        work = Path(directory)
        (work / "cases.json").write_text(json.dumps(CASES))
        (work / "bref-fpm-handler.php").write_text((HERE / "bref-fpm-handler.php").read_text())
        work.chmod(0o755)
        for item in work.iterdir():
            item.chmod(0o644)
        bref = bref_side(args.image, work)
        ours = gateway_side(args.image)
    print(json.dumps(differences(bref, ours), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
