# Worker lifecycle checks

These tests deliberately occupy PHP workers. They run against the test fixtures
(`tests/fixtures`) packaged as a deployment ZIP; never expose those routes in a
production application. `scripts/test-package.py` runs them with one worker, no
queue, a 150 ms request deadline and a 2 s PHP timeout:

```bash
node scripts/package.mjs --runtime-dir build/ci/runtime --rust-binary build/ci/bref-web-gateway \
  --architecture x86_64 --app-dir tests/fixtures --output build/ci/destructive-test-only.zip
python3 scripts/test-package.py build/ci/destructive-test-only.zip --suite lifecycle
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
`--output` is provided, and shares the integration suite's process lock. No
unsupported outcome is silently counted as a pass. The configuration above is a
prerequisite; the HTTP interface does not expose or verify it.
These local checks do not establish AWS lifecycle, scaling or billing behavior.
