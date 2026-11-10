#!/usr/bin/env python3
"""Compare a real Laravel or Symfony application under Bref and under this adapter.

Bref side: the application's own Bref FpmHandler (Bref's FPM runtime) driven with
API Gateway HTTP API events in the Bref image, with Bref's stock FPM/INI config.
Adapter side: the exact deployment ZIP (scripts/package.mjs) in the managed Node
image, as tested by scripts/test-package.py. Both sides receive the same Lambda
environment variables and the same requests; the report lists every difference
in status, redirects, cookies, bodies and logs.

Prepare applications with tests/frameworks/setup.sh first.
"""

import argparse
import base64
import http.client
import importlib.util
import json
import re
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from node_images import NODE_IMAGES  # noqa: E402

LAMBDA_ENV = {
    "AWS_LAMBDA_FUNCTION_NAME": "framework-parity",
    "AWS_LAMBDA_FUNCTION_VERSION": "$LATEST",
    "AWS_LAMBDA_FUNCTION_MEMORY_SIZE": "1024",
    "AWS_REGION": "eu-west-1",
    "AWS_DEFAULT_REGION": "eu-west-1",
}
FRAMEWORK_ENV = {
    "laravel": {
        "APP_ENV": "production", "APP_DEBUG": "false",
        "APP_KEY": "base64:" + base64.b64encode(b"0123456789abcdef0123456789abcdef").decode(),
        "APP_URL": "https://example.com", "LOG_CHANNEL": "stderr",
        "SESSION_DRIVER": "file", "CACHE_STORE": "array", "DB_CONNECTION": "sqlite",
    },
    # Values a deployment sets as environment variables (Dotenv is disabled).
    "symfony": {"APP_ENV": "prod", "APP_DEBUG": "0", "APP_SECRET": "0123456789abcdef0123456789abcdef",
                "DEFAULT_URI": "https://example.com", "APP_SHARE_DIR": "/tmp/share"},
}
HEADERS = {
    "host": "example.com", "x-forwarded-for": "203.0.113.7", "x-forwarded-proto": "https",
    "x-forwarded-port": "443", "user-agent": "framework-parity/1", "accept": "*/*",
}
BOUNDARY = "ParityBoundary"
MULTIPART = (f"--{BOUNDARY}\r\nContent-Disposition: form-data; name=\"note\"\r\n\r\nhello\r\n"
             f"--{BOUNDARY}\r\nContent-Disposition: form-data; name=\"upload\"; filename=\"a.bin\"\r\n"
             f"Content-Type: application/octet-stream\r\n\r\n{'z' * 1000}\r\n--{BOUNDARY}--\r\n")


def case(name, method, path, query="", headers=None, body="", jar=None):
    return {"name": name, "method": method, "path": path, "query": query,
            "headers": {**HEADERS, **(headers or {})}, "jar": jar,
            "body_base64": base64.b64encode(body.encode()).decode()}


CASES = [
    case("probe_get", "GET", "/probe/a%2Fb", "x=1&arr[]=1&arr[]=2&tag=a+z",
         {"cookie": "pref=dark", "authorization": "Basic dTpw"}),
    case("probe_form", "POST", "/probe/form", "", {"content-type": "application/x-www-form-urlencoded"}, "name=bref&x=2"),
    case("probe_json", "POST", "/probe/json", "", {"content-type": "application/json"}, '{"a":1,"b":[1,2]}'),
    case("probe_multipart", "POST", "/probe/upload", "", {"content-type": f"multipart/form-data; boundary={BOUNDARY}"}, MULTIPART),
    case("probe_no_content_type", "POST", "/probe/raw", "", {}, "name=bref&x=2"),
    case("session_first", "GET", "/session", jar="session"),
    case("session_second", "GET", "/session", jar="session"),
    case("redirect", "GET", "/redirect"),
    case("error", "GET", "/error"),
    case("stream", "GET", "/stream"),
    case("not_found", "GET", "/definitely-missing"),
]


def docker(*args, check=True):
    result = subprocess.run(["docker", *args], capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args[:3])} failed: {result.stderr[-2000:]}")
    return result


def env_args(environment):
    return [item for key, value in environment.items() for item in ("-e", f"{key}={value}")]


def bref_side(image, app, environment, work):
    (work / "cases.json").write_text(json.dumps(CASES))
    (work / "driver.php").write_text((HERE / "bref-fpm-handler.php").read_text())
    for item in (work, work / "cases.json", work / "driver.php"):
        item.chmod(0o755 if item.is_dir() else 0o644)
    result = docker("run", "--rm", "--user", "1000:1000", "--read-only",
                    "--tmpfs", "/tmp:rw,size=512m,mode=1777", "-e", "LAMBDA_TASK_ROOT=/var/task",
                    "-e", "BREF_RUNTIME=fpm", *env_args(environment),
                    "-v", f"{app}:/var/task:ro", "-v", f"{work}:/parity:ro",
                    "--entrypoint", "/opt/bin/php", image, "/parity/driver.php", "/parity/cases.json", check=False)
    if result.returncode != 0:
        raise RuntimeError(f"Bref side failed:\n{result.stderr[-4000:]}")
    responses = {}
    for name, item in json.loads(result.stdout).items():
        headers = {k: (", ".join(v) if isinstance(v, list) else str(v)) for k, v in item["headers"].items()}
        responses[name] = {"status": item["status"], "headers": headers, "set_cookie": item["set_cookie"],
                           "body": base64.b64decode(item["body_base64"])}
    return responses, result.stderr


# Symfony applications keep their dotenv files, such as Bref's .env.local.php recipe.
PACKAGE_FLAGS = {"laravel": [], "symfony": ["--keep-dotenv"]}


def adapter_side(app, environment, runtime_dir, rust_binary, architecture, package_flags):
    spec = importlib.util.spec_from_file_location("package_extract", ROOT / "packaging/extract.py")
    extractor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(extractor)
    (ROOT / "build").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="framework-parity-", dir=ROOT / "build") as temporary:
        archive = Path(temporary) / "app.zip"
        built = subprocess.run(["node", str(ROOT / "scripts/package.mjs"), "--runtime-dir", str(runtime_dir),
                                "--rust-binary", str(rust_binary), "--architecture", architecture,
                                "--app-dir", str(app), "--output", str(archive), *package_flags],
                               capture_output=True, text=True)
        if built.returncode != 0:
            raise RuntimeError(f"Packaging failed: {built.stderr}")
        extracted = Path(temporary) / "task"
        extractor.extract(archive, extracted)
        name = f"framework-parity-{uuid.uuid4().hex[:8]}"
        docker("run", "--detach", "--name", name, "--user", "1000:1000", "--read-only",
               "--tmpfs", "/tmp:rw,nosuid,nodev,size=512m,mode=1777", "--publish", "127.0.0.1::8080",
               "--mount", f"type=bind,src={extracted},dst=/var/task,readonly",
               "-e", "AWS_LAMBDA_HTTP_ENDPOINT=0.0.0.0:8080", *env_args(environment),
               "--entrypoint", "/var/lang/bin/node", NODE_IMAGES[architecture], "/var/task/index.js")
        try:
            port = None
            for _ in range(150):
                ports = json.loads(docker("inspect", name).stdout)[0]["NetworkSettings"]["Ports"].get("8080/tcp")
                if ports:
                    port = int(ports[0]["HostPort"])
                    try:
                        probe = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
                        probe.request("GET", "/__bref_web/health")
                        if probe.getresponse().status == 200:
                            break
                    except (OSError, http.client.HTTPException):
                        pass
                time.sleep(0.1)
            else:
                raise RuntimeError("Adapter did not become ready:\n" + docker("logs", name, check=False).stderr[-4000:])
            jars, responses = {}, {}
            for item in CASES:
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
                url = item["path"] + ("?" + item["query"] if item["query"] else "")
                connection.putrequest(item["method"], url, skip_host=True, skip_accept_encoding=True)
                headers = dict(item["headers"])
                jar = jars.get(item["jar"], {}) if item["jar"] else {}
                if jar:
                    cookies = [headers.pop("cookie")] if "cookie" in headers else []
                    headers["cookie"] = "; ".join(cookies + [f"{k}={v}" for k, v in jar.items()])
                for key, value in headers.items():
                    connection.putheader(key, value)
                connection.putheader("lambda-web-request-id", f"req-{item['name']}")
                body = base64.b64decode(item["body_base64"])
                if body or item["method"] == "POST":
                    connection.putheader("content-length", str(len(body)))
                connection.endheaders(body or None)
                response = connection.getresponse()
                raw = response.getheaders()
                set_cookie = [v for k, v in raw if k.lower() == "set-cookie"]
                if item["jar"]:
                    for cookie in set_cookie:
                        pair = cookie.split(";", 1)[0]
                        key, _, value = pair.partition("=")
                        jars.setdefault(item["jar"], {})[key.strip()] = value.strip()
                folded = {}
                for key, value in raw:
                    if key.lower() != "set-cookie":
                        folded[key.lower()] = f"{folded[key.lower()]}, {value}" if key.lower() in folded else value
                responses[item["name"]] = {"status": response.status, "headers": folded,
                                           "set_cookie": set_cookie, "body": response.read()}
            time.sleep(0.3)
            return responses, docker("logs", name, check=False).stderr
        finally:
            docker("rm", "-f", name, check=False)


def cookie_shape(cookies):
    """Cookie names and attributes, without values or expiry times."""
    shaped = {}
    for cookie in cookies:
        parts = [part.strip() for part in cookie.split(";")]
        name = parts[0].split("=", 1)[0]
        # Laravel's cookie session driver names its cookie after a random session ID.
        name = "<session-id>" if re.fullmatch(r"[A-Za-z0-9]{40}", name) else name
        attributes = sorted(p.lower() if "=" not in p else p.split("=", 1)[0].lower() + "=" + p.split("=", 1)[1].lower()
                            for p in parts[1:] if not p.lower().startswith(("expires=", "max-age=")))
        shaped[name] = attributes
    return shaped


def compare(bref, adapter):
    report = {}
    for item in CASES:
        name = item["name"]
        b, a = bref[name], adapter[name]
        found = {}
        if b["status"] != a["status"]:
            found["status"] = {"bref": b["status"], "adapter": a["status"]}
        for header in ("location", "content-type", "cache-control"):
            if b["headers"].get(header) != a["headers"].get(header):
                found[f"header.{header}"] = {"bref": b["headers"].get(header), "adapter": a["headers"].get(header)}
        if cookie_shape(b["set_cookie"]) != cookie_shape(a["set_cookie"]):
            found["set_cookie"] = {"bref": cookie_shape(b["set_cookie"]), "adapter": cookie_shape(a["set_cookie"])}
        try:
            bj, aj = json.loads(b["body"]), json.loads(a["body"])
        except ValueError:
            bj = aj = None
        if isinstance(bj, dict) and isinstance(aj, dict):
            for key in sorted(set(bj) | set(aj)):
                if bj.get(key) != aj.get(key):
                    found[f"body.{key}"] = {"bref": bj.get(key), "adapter": aj.get(key)}
        elif b["body"] != a["body"]:
            found["body"] = {"bref": b["body"][:300].decode(errors="replace"),
                             "adapter": a["body"][:300].decode(errors="replace")}
        report[name] = found
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("framework", choices=FRAMEWORK_ENV)
    parser.add_argument("app_dir", type=Path)
    parser.add_argument("--image", default="bref-web-functions:local")
    parser.add_argument("--runtime-dir", type=Path, default=ROOT / "build/ci/runtime")
    parser.add_argument("--rust-binary", type=Path, default=ROOT / "build/ci/bref-web-gateway")
    parser.add_argument("--architecture", default="x86_64", choices=("x86_64", "aarch64"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    app = args.app_dir.resolve()
    environment = {**LAMBDA_ENV, **FRAMEWORK_ENV[args.framework]}
    with tempfile.TemporaryDirectory() as work:
        bref, bref_logs = bref_side(args.image, app, environment, Path(work))
    adapter, adapter_logs = adapter_side(app, environment, args.runtime_dir.resolve(),
                                         args.rust_binary.resolve(), args.architecture,
                                         PACKAGE_FLAGS[args.framework])
    marker = "Deliberate framework parity error"
    result = {
        "framework": args.framework,
        "bref_probe": json.loads(bref["probe_get"]["body"]) if bref["probe_get"]["status"] == 200 else None,
        "statuses": {name: {"bref": bref[name]["status"], "adapter": adapter[name]["status"]} for name in bref},
        "error_logged": {"bref": marker in bref_logs, "adapter": marker in adapter_logs},
        # Equal is not enough: the second request must see the first one's session.
        "session_counts": {side: [json.loads(responses[name]["body"]).get("count")
                                  if responses[name]["status"] == 200 else None
                                  for name in ("session_first", "session_second")]
                           for side, responses in (("bref", bref), ("adapter", adapter))},
        "differences": compare(bref, adapter),
    }
    if args.output:
        args.output.mkdir(parents=True, exist_ok=True)
        (args.output / "bref.log").write_text(bref_logs)
        (args.output / "adapter.log").write_text(adapter_logs)
        (args.output / "report.json").write_text(json.dumps(result, indent=2, sort_keys=True))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
