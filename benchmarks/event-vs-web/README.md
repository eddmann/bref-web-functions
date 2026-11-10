# Bref event function vs Web Function

Runs one Laravel application both ways and measures what users see and what
Lambda bills:

- **Event function:** Bref 3's FPM runtime as its serverless plugin configures
  it (`provided.al2023`, ARM64, the `arm-php-84` layer, `BREF_RUNTIME` set to
  the FPM runtime), 1024 MB, behind a Function URL. One request per
  environment.
- **Web Functions:** this runtime at 4, 16, 32 and 64 PHP-FPM workers
  (`maxConcurrencyPerEnvironment` equal), the default cap of 10 environments.

The application is the `tests/frameworks` Laravel app with [`routes.php`](routes.php)
appended: `/bench/fast` (a small JSON page with a session), `/bench/io?ms=`
(waits, as on a database or API call), `/bench/cpu?ms=` (busy CPU),
`/bench/large?mb=` and `/bench/stream`. Every JSON response names the
execution environment that served it.

## Results

us-west-1, 9 November 2026, Laravel 13 with `bref/laravel-bridge`. Cost per
million requests from Lambda's own accounting, at published US East Arm prices.

| Load | Event function (1024 MB) | Web Function (16 workers) | Environments (event / web) |
| --- | --- | --- | --- |
| I/O page (200 ms wait), 90 rps | $3.07 | **$0.60** (5.1× less) | 43 / 4 |
| I/O page (200 ms wait), 50 rps | $3.00 | **$0.83** (3.6× less) | 22 / 8 |
| Small page, 50 rps | **$0.33** | ~$0.77 (2.3× more)* | 3 / 8 |
| CPU page (50 ms), 20 rps | **$1.08** | ~$2.30 (2.1× more)* | 4 / 4 |
| Cold burst, 40 simultaneous I/O requests | 40 cold starts, p50 2.17 s | 4 cold starts (3 environments served all 40), p50 1.49 s | |

\* Approximate: Lambda's per-minute usage windows did not align with these runs.

- Warm latency was the same on both (p50 0.16 s small page, 0.36 s I/O page,
  0.22 s CPU page; client about 150 ms from the Region).
- Event cost scales with configured memory: the application used 138 MB; at
  512 MB the I/O-page advantage would be about 2×.
- Lambda packed up to 10 concurrent requests into one Web Function environment
  at 90 rps, but spread them (about 2 per environment) at 50 rps. Beyond 16
  workers nothing changed.
- Event functions failed above about 6 MB of response (502) and delivered a
  stream all at once; Web Functions returned 32 MB and streamed each chunk as
  written.

## Run

```sh
tests/frameworks/setup.sh laravel build/frameworks/laravel
# runtime/ and bin/bref-web-gateway from a CI aws-archives-aarch64 diagnostic.zip
benchmarks/event-vs-web/build.sh build/frameworks/laravel <runtime-dir> <gateway> build/bench

cd infra/aws && npx cdk deploy -c testToken=<token> -c apps=bench \
  -c archives=$PWD/../../build/bench/archives -c benchAppDirectory=$PWD/../../build/bench/app \
  --outputs-file outputs.json
cd ../..

benchmarks/event-vs-web/run.sh rate  infra/aws/outputs.json results/bench/runs
# when every target is cold:
benchmarks/event-vs-web/run.sh burst infra/aws/outputs.json results/bench/runs 40
python3 benchmarks/event-vs-web/analyse.py results/bench/runs/*.json > results/bench/summary.json
```

`load.py` paces requests at a fixed rate over persistent HTTPS connections
(open loop: it does not wait for earlier responses), so latency includes the
network between the client and the Region. `client_queue_p99_s` shows whether
the client itself fell behind.

`analyse.py` reads Lambda's own accounting from CloudWatch Logs: for Web
Functions, `platform.initStart` (cold starts) and `platform.invokeSummary`
(requests, active CPU, memory per environment per minute); for the event
function, `REPORT` lines (billed duration, memory, init duration). It prices
them with the published US East (N. Virginia) Arm rates; see the script.

## Cost model

| | Web Function | Event function |
| --- | --- | --- |
| Requests | $0.30 / million | $0.20 / million |
| Compute | $0.0000276944 per **active** vCPU-second; I/O wait is not billed | $0.0000133334 per GB-second of configured memory for the whole billed duration, I/O wait included |
| Memory | $0.0000036667 per GB-second consumed by each environment for as long as it exists, idle included | (in compute) |

So the comparison depends on the workload: requests that wait on I/O cost an
event function their full duration, a Web Function only the CPU they use, but a
Web Function also pays for the memory of idle environments (about five minutes
after their last request, as measured).
