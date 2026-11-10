# AWS exploration archives

Applications and checks for the [AWS test plan](../../docs/aws-test-plan.md).

- [`examples/diagnostic`](../../examples/diagnostic/) is the deployable
  diagnostic page.
- [`public/index.php`](public/index.php) adds **test-only** routes for an
  isolated account, falling back to the diagnostic page:

| Route | Effect |
| --- | --- |
| `/aws-test/sleep?ms=` | Hold a worker (0–120000 ms) |
| `/aws-test/stream?chunks=&interval_ms=` | Write lines with pauses, flushing each |
| `/aws-test/large?mb=` | 1–64 MiB response |
| `/aws-test/memory?mb=&hold_ms=` | Allocate memory (beyond `memory_limit` fails), optionally holding it |
| `/aws-test/crash` | SIGKILL the worker: 502, FPM replaces it |
| `/aws-test/stuck?seconds=` | Ignore FPM's SIGTERM so completion is never confirmed: the runtime retires |

Build and check locally:

```sh
./scripts/build.sh    # then export build/ci/runtime and build/ci/bref-web-gateway
tests/aws/build-app.sh build/aws-test-app
node scripts/package.mjs --runtime-dir build/ci/runtime \
  --rust-binary build/ci/bref-web-gateway --architecture x86_64 \
  --app-dir build/aws-test-app --output build/aws/aws-test-only.zip
python3 tests/aws/local_check.py build/aws/aws-test-only.zip
```

CI builds, checks and uploads both archives for each architecture.
