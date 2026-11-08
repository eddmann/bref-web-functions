# Historical local comparison

These measurements were recorded on 8 November 2026 before the repository
reorganization and later configuration/packaging fixes. They compare the Rust
prototype with Caddy locally, not Bref Event Lambda with Web Functions on AWS.
Current correctness results are summarized in [verification](verification.md).

## Method

PHP **8.4.27**, Node **24.22.2**, Caddy **2.11.9**, x86_64 static-musl Rust build.
Both gateways used the same local image, PHP front controller and four-worker
FPM configuration. The benchmark sent 300 requests at each concurrency, after
five warmups, with a new HTTP connection per request. Runs were serialized.
All 1,800 benchmark responses were HTTP 200, without transport errors.

| Concurrent clients | Rust requests/s | Caddy requests/s | Rust median / p95 | Caddy median / p95 |
| --- | ---: | ---: | ---: | ---: |
| 1 | 786 | 719 | 1.18 / 1.49 ms | 1.27 / 1.77 ms |
| 4 | 1,694 | 1,425 | 2.07 / 3.48 ms | 2.50 / 3.96 ms |
| 8 | 1,958 | 1,762 | 3.35 / 5.91 ms | 3.98 / 6.84 ms |

This short run is sensitive to host scheduling and Python/client overhead.
Concurrency eight also includes queuing, whose policy differs between gateways.
It does not establish a production speed advantage. No AWS cold starts, scaling,
CPU billing or cost were measured.

## Streaming and worker accounting

A three-chunk response, with 200 ms between application writes, arrived at:

| Gateway | Chunk arrival times |
| --- | --- |
| Rust | 1.4 / 201.6 / 402.1 ms |
| Caddy | 11.3 / 212.0 / 401.9 ms |

This confirms local streaming through HTTP and FastCGI. It does not verify AWS
ingress behavior. Both gateways passed the shared 22-case request suite, including
state reset during worker reuse and isolation across four observed PHP workers.

Separate Rust checks used one worker, no queue, a 150 ms HTTP deadline and a
two-second FPM timeout:

- A hanging request returned HTTP 504 at 151.8 ms. New PHP requests received 503
  while the original worker remained occupied. Capacity returned at 2.57 seconds
  with a new worker PID after FPM's timeout.
- A client disconnected after receiving an acknowledgement while PHP deliberately
  continued working. Capacity remained reserved until normal completion at
  1.04 seconds; the same worker then served another request.

These tests check that frontend completion does not falsely imply backend
completion. They are correctness evidence, not latency tuning results.

## Executable size

| Executable in that build | Bytes | MiB |
| --- | ---: | ---: |
| Rust gateway | 1,187,656 | 1.13 |
| Caddy | 50,892,960 | 48.54 |
| Shared Node executable | 122,981,992 | 117.28 |
| Shared PHP-FPM executable | 21,787,976 | 20.78 |

These are historical executable sizes, not current release guarantees. PHP
extensions, libraries and application dependencies add to deployment size. The
comparison image contained both gateways, so its image size did not measure the
difference between minimal deployment packages.

**No memory comparison is available.** Both containers reported the same parent
cgroup's memory and PID statistics despite requested two-CPU, 2 GiB limits.
Those values cannot establish per-container memory consumption.

## Repeat a comparison

```sh
./scripts/build.sh
python3 scripts/compare.py --iterations 300
```

The script writes raw results and environment metadata under ignored `results/`.
Retain those as local or CI artifacts with the source commit and image identities.
Use a real application on AWS before deciding whether the Rust gateway improves
cost, latency or resource use enough to justify maintaining its FastCGI bridge.
