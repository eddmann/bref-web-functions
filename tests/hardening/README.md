# Native runtime hardening regressions

Run this suite on a native Linux AMD64 or ARM64 host after exporting the matching
static Rust gateway. Python 3 and Node.js are required; Docker and AWS credentials
are not used by the harness.

```sh
python3 tests/hardening/run.py \
  --binary build/ci/bref-web-gateway \
  --output results/ci/hardening.json
```

The harness launches the actual Node supervisor, which starts Rust. Rust starts a
small **test-only fake FPM process** in its own process group. The fake peer also
creates a descendant that ignores graceful stop signals, so process-group cleanup
is checked rather than inferred from the Rust exit code. All listeners bind to
loopback on dynamically selected ports, and each case cleans up its own processes.

The checks are:

1. A backend that never reports completion produces a frontend 504, then causes
   bounded runtime retirement. A second request already streaming remains able to
   finish during draining. Node exits nonzero, the listener closes, and Rust/FPM
   descendants have terminated. The fixture is released only after the existing
   `unresolved_backend=true` log marker, avoiding a timing guess about retirement.
2. A client reads response headers for a 64 MiB response, then never reads again.
   With one backend slot, health first receives 503, then recovers after the
   300 ms HTTP deadline plus the 100 ms write grace. The fake backend reaches
   completion and the same Rust/FPM processes remain alive.
3. The same stalled-client check continuously pipelines extra GET requests behind
   the blocked response. Read-ahead must not extend its original response deadline
   or dispatch those requests ahead of the blocked write.
4. Eight ordinary requests reuse one TCP connection over a period longer than
   one request timeout. Each request refreshes the deadline; this is not a fixed
   connection-lifetime cutoff. Once requests stop, the last request's deadline
   closes that same idle connection.

Results and bounded diagnostic log tails are written to JSON, with full per-case
logs alongside it. Unsupported execution, cleanup failures or missing evidence
are failures rather than skipped passes. The fake peer does not model PHP engine
behavior; the existing real-FPM, real-Bref and exact-archive suites retain that
coverage. These tests do not establish AWS lifecycle behavior.
