#!/usr/bin/env python3
"""Real Bref dependency checks against a local dedicated compatibility app."""

import argparse
import concurrent.futures
import datetime
import hashlib
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integration"))
from run import Client, HarnessLock, check

EXPECTED_VERSION = "3.0.15"
EXPECTED_REFERENCE = "18776f2f9bcf6be47736a33b9dfa301085ee827e"


def php_bytes(value):
    value = value.strip()
    suffix = value[-1:].upper()
    factors = {"K": 1024, "M": 1024 ** 2, "G": 1024 ** 3}
    return int(value[:-1]) * factors[suffix] if suffix in factors else int(value)


class Suite:
    def __init__(self, client):
        self.client = client
        self.results = []
        self.reference = None

    def case(self, name, test):
        start = time.monotonic()
        try:
            details = test()
            result = {"name": name, "status": "passed", "details": details}
        except Exception as error:
            result = {"name": name, "status": "failed", "error": f"{type(error).__name__}: {error}"}
        result["seconds"] = round(time.monotonic() - start, 6)
        self.results.append(result)
        print(f"{result['status'].upper():7} {name}" + (f": {result['error']}" if "error" in result else ""), flush=True)

    def inspect(self, **kwargs):
        return self.client.json("/inspect", **kwargs)

    def real_dependency(self):
        result = self.inspect()
        check(result["bref_version"] == EXPECTED_VERSION, f"Unexpected Bref version: {result['bref_version']}")
        check("/vendor/bref/bref/src/Bref.php" in result["bref_class_path"], "Bref class was replaced by a fixture/stub")
        check(result["subscriber_registered"] is True, "Real Bref EventDispatcher is unavailable")
        self.reference = result["bref_reference"]
        check(self.reference == EXPECTED_REFERENCE, f"Unexpected Composer source reference: {self.reference}")
        return {"version": result["bref_version"], "reference": self.reference, "class_path": result["bref_class_path"]}

    def initialization(self):
        result = self.inspect()
        check(result["runtime"] == "fpm", f"Wrong BREF_RUNTIME: {result['runtime']}")
        check(result["task_root"] == "/var/task", f"Unexpected task root: {result['task_root']}")
        check(result["startup_count"] == 1, f"beforeStartup did not run exactly once: {result['startup_count']}")
        check(result["autoload_count"] == 2, f"Initialization environment was not inherited/reset: {result['autoload_count']}")
        check(result["hook_events"] == ["beforeStartup"], f"Unexpected lifecycle hooks: {result['hook_events']}")
        return {"runtime": result["runtime"], "startup_count": result["startup_count"], "autoload_count": result["autoload_count"]}

    def application_ini_and_extension(self):
        result = self.inspect()
        check(result["precision"] == "12", "Application php/conf.d INI was not honored")
        check(result["intl_available"] and result["intl_result"] == 8364, "Configured intl extension did not execute")
        check("/php/conf.d/fixture.ini" in result["ini_scanned"], "Application INI absent from scan result")
        return {"precision": result["precision"], "intl_result": result["intl_result"]}

    def disabled_functions(self):
        result = self.inspect()
        required = {"exec", "passthru", "fastcgi_finish_request"}
        check(required.issubset(result["disable_functions"]),
              f"Runtime override removed application disabled functions: {result['disable_functions']}")
        return {"required_functions_disabled": sorted(required)}

    def body_limit_configuration(self):
        result = self.inspect()
        size = result["configured_max_body_bytes"]
        check(size > 0, "Gateway body-limit environment was not exported")
        check(php_bytes(result["post_max_size"]) == size and php_bytes(result["upload_max_filesize"]) == size,
              f"Gateway/PHP limits disagree: {size}, {result['post_max_size']}, {result['upload_max_filesize']}")
        return {"max_body_bytes": size, "post_max_size": result["post_max_size"], "upload_max_filesize": result["upload_max_filesize"]}

    def request_mapping(self):
        body = b"\x00actual-Bref-compatibility\xff\n"
        response = self.client.json("/inspect?identity=request-mapping&encoded=%2F%26", method="PATCH", body=body,
                                    headers={"lambda-web-request-id": "real-bref-request-mapping",
                                             "Content-Type": "application/octet-stream"})
        check(response["request_id"] == "real-bref-request-mapping", "Per-request Lambda ID missing")
        check(response["method"] == "PATCH", "Request method changed")
        check(response["raw_uri"] == "/inspect?identity=request-mapping&encoded=%2F%26", "Raw request URI changed")
        check(response["body_sha256"] == hashlib.sha256(body).hexdigest(), "Binary input changed")
        return {"request_id": response["request_id"], "method": response["method"]}

    def request_cleanup(self):
        pids = []
        for index in range(16):
            identity = f"sequential-{index}"
            result = self.client.json(f"/inspect?identity={identity}")
            self.assert_clean(result, identity)
            pids.append(result["pid"])
        check(len(set(pids)) < len(pids), "Did not observe PHP worker reuse")
        return {"requests": len(pids), "pids": sorted(set(pids))}

    @staticmethod
    def assert_clean(result, identity):
        check(result["request_environment_before"] is False, "Request putenv value leaked into another request")
        check(result["request_environment_after"] == identity, "Request environment identity changed")
        check(result["function_static_counter"] == 1 and result["class_static_counter"] == 1, "PHP static state leaked between requests")
        check(result["startup_count"] == 1 and result["autoload_count"] == 2, "Startup/autoload state leaked or repeated")

    def concurrent_context(self):
        def invoke(index):
            identity = f"concurrent-{index}"
            result = self.client.json(f"/inspect?identity={identity}&sleep_ms=100", headers={"lambda-web-request-id": identity})
            self.assert_clean(result, identity)
            check(result["identity"] == identity and result["request_id"] == identity, "Concurrent request context leaked")
            return result["pid"]
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            pids = list(executor.map(invoke, range(12)))
        check(len(set(pids)) > 1, f"Did not observe multiple PHP workers: {pids}")
        return {"requests": len(pids), "pids": sorted(set(pids))}

    def explicit_compatibility_boundary(self):
        result = self.inspect()
        check(result["hook_events"] == ["beforeStartup"], f"Unexpected hook behavior: {result['hook_events']}")
        check(result["startup_memory_transferred"] is False, "Fixture unexpectedly relies on startup object memory")
        check(not any(result["event_context_presence"].values()), "Runtime synthesized unsupported event context")
        return {"hook_events": result["hook_events"], "startup_memory_transferred": False,
                "event_context_presence": result["event_context_presence"],
                "meaning": "Verifies documented gaps; does not count unsupported features as implemented."}

    def run(self):
        for name, test in (
            ("real_bref_package_and_dispatcher", self.real_dependency),
            ("real_before_startup_hook_and_environment_inheritance", self.initialization),
            ("application_ini_and_native_intl_extension", self.application_ini_and_extension),
            ("application_disabled_functions_preserved", self.disabled_functions),
            ("gateway_and_php_body_limit_agree", self.body_limit_configuration),
            ("request_mapping_with_real_bref_autoload", self.request_mapping),
            ("php_state_and_environment_reset_with_worker_reuse", self.request_cleanup),
            ("concurrent_request_ids_and_state_isolated", self.concurrent_context),
            ("documented_unsupported_contract_remains_explicit", self.explicit_compatibility_boundary),
        ):
            self.case(name, test)
        return self.results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        client = Client(args.base_url)
    except ValueError as error:
        parser.error(str(error))
    with HarnessLock():
        suite = Suite(client)
        results = suite.run()
        report = {"timestamp_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  "scope": "Local real-Bref dependency tests; no AWS or full framework compatibility claim.",
                  "bref_version": EXPECTED_VERSION, "bref_reference": suite.reference,
                  "tests": results, "passed": sum(r["status"] == "passed" for r in results),
                  "failed": sum(r["status"] == "failed" for r in results),
                  "unsupported": ["afterStartup", "beforeInvoke", "afterInvoke", "persistent initialization PHP objects",
                                  "LAMBDA_INVOCATION_CONTEXT", "LAMBDA_REQUEST_CONTEXT", "_X_AMZN_TRACE_ID"]}
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(f"real Bref: {report['passed']} passed, {report['failed']} failed", flush=True)
    return int(report["failed"] > 0)


if __name__ == "__main__":
    sys.exit(main())
