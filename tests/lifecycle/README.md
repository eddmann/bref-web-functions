# Dedicated Rust lifecycle checks

These tests deliberately occupy or kill PHP workers. Run them only against the
local prototype fixtures in a dedicated container. Do not expose the fixture
routes in a production application.

Configure the Rust container with:

```text
BREF_WEB_GATEWAY=rust
BREF_WEB_MAX_IN_FLIGHT=1
BREF_WEB_QUEUE_CAPACITY=0
BREF_WEB_REQUEST_TIMEOUT_MS=150
BREF_WEB_FPM_TIMEOUT_SECONDS=2
```

Use a separate loopback port, then run:

```bash
python3 tests/lifecycle/run.py --base-url http://127.0.0.1:8083 --output results/rust-lifecycle.json
```

The first check calls `/hang`, which sleeps for five seconds. The frontend must
return HTTP 504 near its 150 ms deadline. PHP `/health` requests must still get
HTTP 503 while the single backend worker remains occupied. Recovery must follow
the PHP hard timeout, with a different worker PID. The test fails if the gateway
releases capacity merely because its frontend timed out.

The second check calls `/disconnect-work`, reads its flushed acknowledgement,
then closes the client socket. This **test-only** route sets
`ignore_user_abort(true)` and continues sleeping for one second. PHP `/health`
must receive HTTP 503 until that work completes, then recover in the same worker
PID. This intentionally checks accounting for work after a client disconnect;
production application code should normally cancel unnecessary work instead.

The script accepts only loopback URLs, writes every pass/failure to JSON when
`--output` is provided, and shares the integration/benchmark process lock. No
unsupported outcome is silently counted as a pass. The container configuration
is a prerequisite; the HTTP interface does not expose or verify its environment.
These local checks do not establish AWS lifecycle, scaling or billing behavior.
