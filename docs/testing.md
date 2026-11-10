# Testing

Every suite except the Rust and packaging unit tests runs the **exact deployment
ZIP**: extracted with hash, mode and symlink checks, mounted read-only at
`/var/task` in the managed Node 24 image (which has no Bref `/opt`), as UID 1000
with a read-only root and writable `/tmp`. A working response therefore depends
on the archive's relocated PHP binaries, libraries, extensions and configuration.

| Suite | What it establishes | Runs in |
| --- | --- | --- |
| `cargo test --locked` | HTTP/CGI mapping, FastCGI framing, admission, deadlines, backend accounting, forwarded-header modes | CI, both architectures |
| [`runtime/php/tests.sh`](../runtime/php/tests.sh) | Startup steps in Bref's PHP image: initialization order, environment inheritance, failures exit 78 without logging secrets, INI precedence, TLS CA choices, relocated paths, every settings validation, a real four-worker FPM pool | CI |
| [`packaging/tests.py`](../packaging/tests.py) | Packager layout, determinism and refusals (ELF architecture, escaping or dangling links, collisions); extractor refusals; the Node entry under CommonJS and ES-module apps | CI |
| `scripts/test-package.py --suite smoke` | The default archive starts and serves PHP under FPM | CI |
| `--suite integration` ([`tests/integration`](../tests/integration/)) | 22 HTTP checks: methods, bodies, uploads, cookies, headers, streaming, limits, errors, request IDs, PHP state isolation | CI |
| `--suite lifecycle` ([`tests/lifecycle`](../tests/lifecycle/)) | A frontend timeout or client disconnect keeps the PHP worker reserved until PHP actually stops | CI |
| `--suite bref` ([`tests/bref`](../tests/bref/)) | Real Bref 3.0.15 classes: event dispatcher, `beforeStartup`, application INI, native `intl`, body limits, unsupported hooks stay absent | CI |
| [`tests/hardening`](../tests/hardening/) | Native processes: retirement after unconfirmed completion, draining, process-group cleanup, stalled clients, pipelining, keep-alive deadlines | CI (Linux only) |
| [`tests/aws/local_check.py`](../tests/aws/) | The AWS test archive's routes, log lines, overruns and real PHP-FPM retirement | CI |
| [`tests/bref-parity`](../tests/bref-parity/) | Every CGI variable PHP sees, against Bref's own `FpmHandler` | Local |
| [`tests/frameworks/run.py`](../tests/frameworks/) | Laravel and Symfony with their bridges, against Bref's FPM runtime: 11 requests each | Local |
| [`tests/frameworks/cold_start.py`](../tests/frameworks/cold_start.py) | Concurrent first requests on fresh environments (several workers preparing caches at once) | Local |

The framework suites need a built application (`tests/frameworks/setup.sh`) and
are not in CI. `cold_start.py` fails for Symfony until
[`upstream/symfony-bridge/atomic-cache-dir.patch`](../upstream/symfony-bridge/)
is applied: 7 of 192 cold first requests failed without it, none of 384 with it.

## On AWS

The [AWS test plan](aws-test-plan.md) and [`infra/aws`](../infra/aws/) deploy CI's
archives as Web Functions. Verified on ARM64 in us-west-1 (November 2026):
deployment and revision updates without failed requests, scaling, overruns,
retirement and replacement, shutdown, streaming, 64 MiB responses, credentials
and `bref-ssm:` secrets, Laravel and Symfony probes, and the forwarded-header
modes. The [benchmark](../benchmarks/event-vs-web/) compares the same Laravel
application with Bref's event runtime.

Re-verified on 10 November 2026 after moving startup into PHP, packaging with
Node and defaulting to 12 workers: the full startup path (424 ms environment init,
PHP-FPM ready in 26 ms), streaming, 64 MiB responses, crash recovery, overruns,
retirement (one 503 in 75 polls, then a fresh environment), Laravel and Symfony
probes with `bref-ssm:` secrets, and memory with every environment saturated
(peak 1,214 MB of 2 GB, no failures; see [memory sizing](compatibility.md#memory-sizing)).

Not yet verified: credential refresh in a long-lived environment, and running
behind CloudFront or a custom domain.
