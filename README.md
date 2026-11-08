# Bref Web Functions

An experimental Rust HTTP/FastCGI gateway for running Bref PHP-FPM on AWS Lambda
Web Functions. Rust is the primary implementation; Caddy is a reference
implementation for comparing behavior and operational tradeoffs.

The gateway reuses Bref's PHP distribution and normal FPM request lifecycle.
Each environment can run several PHP workers while AWS manages HTTP ingress and
scaling. A thin Node 24 launcher starts initialization and supervises the runtime;
application HTTP requests go directly to Rust.

**Status:** native AMD64 and ARM64 CI passed the Rust/Caddy integration suites,
exact deployment archive tests and real Bref dependency checks. Actual AWS
execution, full Laravel/Symfony application validation and production hardening
remain. See [verification](docs/verification.md),
[compatibility](docs/compatibility.md) and the [roadmap](docs/roadmap.md).

## Run locally

Requires Linux, Docker with a local daemon, Python 3 and Node.js for packaging
tests. Rust builds run in the pinned builder image; a host Rust toolchain is
optional.

```sh
./scripts/build.sh
./scripts/run.sh rust 8081
```

From another terminal:

```sh
curl http://127.0.0.1:8081/health
```

The local image contains test fixtures, including intentional timeout and crash
routes. Keep it on loopback and use an explicit application staging directory for
deployment packages. ZIP builds default to the separate minimal smoke example.

To run Caddy instead:

```sh
./scripts/run.sh caddy 8082
```

Both implementations use the same PHP distribution, initialization and FPM
configuration. The [Caddy reference](reference/caddy/) has different admission and
timeout semantics; it is not an assertion of identical overload behavior.

## Check the implementations

```sh
cargo test --locked
python3 packaging/tests.py
./runtime/php/tests.sh
python3 scripts/compare.py --skip-benchmark
```

The comparison script creates its own loopback containers, runs the shared HTTP
suite serially against Rust and Caddy, then checks Rust timeout/disconnect
accounting. It uses a two-second FPM timeout for the deliberate hang tests and
removes its containers afterward. Reports are written under ignored `results/`.

The native [hardening suite](tests/hardening/) also checks controlled runtime
retirement, process-group cleanup, stalled readers, pipelining and keepalive
deadline refresh. Both architecture jobs run it against the exported binary.

To include the small local benchmark:

```sh
python3 scripts/compare.py --iterations 300
```

These measurements do not establish AWS latency, memory usage or cost. Historical
local observations and their limits are recorded in [benchmarks](docs/benchmarks.md).

## Package an application

The [packaging guide](packaging/README.md) covers static Rust builds, exporting
Bref's runtime, deterministic ZIP assembly and testing the exact archive in the
managed Node image without an original Bref `/opt` installation.

Application INI files remain under `php/conf.d/`. Runtime budgets such as worker
count, memory and upload limits use environment variables; see
[configuration and compatibility](docs/compatibility.md). Web Functions' Node
entrypoint and PHP dependencies are bundled together. Existing Bref event/console
runtimes remain separate deployment choices.

## Repository layout

| Path | Purpose |
| --- | --- |
| `src/` | Primary Rust HTTP/FastCGI gateway and FPM supervisor |
| `runtime/launcher/` | Shared Node supervision and managed-runtime entrypoint |
| `runtime/php/` | Shared Bref initialization and PHP/FPM configuration |
| `reference/caddy/` | Caddy configuration and reference-specific documentation |
| `packaging/` | ZIP validation, extraction and packaging tests |
| `tests/` | Shared HTTP, lifecycle, real Bref and fixture code |
| `examples/minimal/` | Non-destructive smoke application |
| `scripts/` | Build, run, package and comparison commands |
| `docs/` | Architecture, compatibility, verification, measurements and roadmap |

Build outputs, ZIPs, installed dependencies and raw logs are local or CI artifacts,
not tracked source. No AWS resources are created by the local test commands.
