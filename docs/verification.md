# Verification status

The following results were recorded during prototype development on
8 November 2026. They describe local execution, not an AWS deployment. The
repository's tests and lock files allow the checks to be repeated after changes.
Raw logs, extracted runtimes and archives are generated artifacts rather than
tracked repository files.

| Check | Recorded result | Source |
| --- | --- | --- |
| Rust unit tests | 14 passed | [`src/`](../src/) |
| Packaging and Node entrypoint tests | 10 passed | [`packaging/tests.py`](../packaging/tests.py) |
| Shared configuration, initialization and FPM tests | 8 PASS groups | [`runtime/php/tests.sh`](../runtime/php/tests.sh) |
| Shared HTTP suite | 22 passed per gateway, no skips | [`tests/integration/run.py`](../tests/integration/run.py) |
| Rust timeout/disconnect accounting | 2 passed | [`tests/lifecycle/run.py`](../tests/lifecycle/run.py) |
| Actual Bref dependency checks | 9 passed | [`tests/bref/`](../tests/bref/) |
| Exact x86_64 smoke and HTTP test archives | Passed in the managed Node base image | [`scripts/test-package.py`](../scripts/test-package.py) |
| ARM64 executable and archive | Built, ELF-inspected and checksum-verified; not executed | [Packaging guide](../packaging/README.md) |

## What the archive tests establish

The exact x86_64 ZIPs were extracted with hash, mode and symlink verification,
then mounted read-only in the managed Node 24 base image. The image had no original
`/opt/bin/php`, so a working response depended on the archive's relocated PHP
binaries, libraries, extensions and configuration. Tests ran as UID 1000 with a
read-only root and writable `/tmp`. Startup, request handling and SIGTERM cleanup
completed successfully.

The actual Bref fixture pins **Bref 3.0.15**, source reference
`18776f2f9bcf6be47736a33b9dfa301085ee827e`. It checks real vendor classes and the
event dispatcher, `beforeStartup`, environment inheritance, application INI,
native `intl`, disabled functions, body limits, request metadata and concurrent
PHP state cleanup. One case verifies that unsupported hooks/context remain
explicitly absent; it does not count those features as implemented.

The PHP version used was **8.4.27**. No full Laravel/Symfony application, database,
external session store or real secrets-service call was exercised.

## Reproduce

```sh
./scripts/build.sh
python3 scripts/compare.py --skip-benchmark
./runtime/php/tests.sh
python3 packaging/tests.py
```

Run Rust unit tests with `cargo test --locked`, or pass
`--build-arg RUN_RUST_TESTS=1` to the Docker build. Follow the
[packaging guide](../packaging/README.md) to assemble and execute exact archives.
The [real Bref fixture instructions](../tests/bref/README.md) describe installing
its pinned production dependencies before packaging it as a test application.

Generated JSON and logs go under `results/`. Keep those outputs with the archive
checksum, source commit and image identities when comparing runs. Local Docker
startup/stop timings include container and CLI overhead; they are not Lambda
cold-start or shutdown-deadline measurements.

## Unverified boundaries

- Native ARM64 dynamic linking and execution.
- AWS startup, HTTP routing, streaming, scaling, resets and shutdown behavior.
- Execution-role credentials and refresh, real SSM/Secrets Manager retrieval,
  IAM, networking and CloudWatch correlation.
- Full framework behavior, sustained load, actual memory peaks and downstream
  database/service limits.

The original nested Docker environment reported a shared parent cgroup despite
requested limits. Its memory statistics cannot support per-gateway memory claims.
See the [compatibility contract](compatibility.md) and [roadmap](roadmap.md) for
the remaining implementation work. No AWS resources were created or modified
during these recorded local checks.
