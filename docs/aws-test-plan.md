# AWS exploration test plan

Local and CI results cannot answer how Lambda Web Functions' ingress, scaling and
environment lifecycle treat this runtime. This plan lists what to measure on AWS,
how, and what counts as a pass. Run it in an **isolated account**.

## Artifacts

CI uploads both archives per architecture as `aws-archives-<arch>` (14 days),
each with its `.sha256`. Web Functions require ARM64: use `aws-archives-aarch64`.

| Archive | Contents | Where |
| --- | --- | --- |
| `diagnostic.zip` | [`examples/diagnostic`](../examples/diagnostic/): reports what the runtime receives and which environment answered; writes only a `/tmp` marker; redacts credentials | Any account |
| `aws-test-only.zip` | The diagnostic page plus [test routes](../tests/aws/) that hold, crash or wedge workers | Isolated test account only |

Deploy the exact CI artifact and record its SHA-256 with every result. The
packager's manifest (`package-manifest.json`) records the source hashes.

## Deployment and configuration

Deploy with the [CDK stack](../infra/aws/). It applies these constraints from the
[Web Functions documentation](https://docs.aws.amazon.com/lambda/latest/dg/web-functions-create.html):

- ARM64 only, `nodejs24.x`, the ZIP's root `index.js` as entry; code from a
  versioned S3 bucket.
- Each environment has a fixed 2 vCPU and 2 GB. `maxConcurrencyPerEnvironment`
  defaults to 64; the stack sets it to `BREF_WEB_MAX_IN_FLIGHT` (default 12) so
  Lambda routes no more concurrent requests than there are PHP workers.
- `timeoutSeconds` (default 30) is Lambda's request timeout. Lambda answers 504
  `Runtime.RequestTimeout` itself at about that limit, so the stack sets the
  gateway deadline to it and FPM's script limit two seconds lower. FPM enforces
  that limit up to a third late (28 s → up to 37.3 s), so overruns usually end in
  Lambda's 504; the gateway waits for FPM's late termination instead of retiring.
- The server must listen within 10 s. After SIGTERM the process has 1 s; the
  launcher's 900 ms and Rust's 500 ms budgets fit inside it.
- Lambda's own rejections (429 throttling, 503 `Platform.EndpointDisabled`) carry
  a `lambda-web-error-type` response header; the runtime's own 503s do not.
- Logs go to `/aws/lambda/web/<function>`.

## What the logs show

| Line | Meaning |
| --- | --- |
| `[launcher] ready on ... startup_ms=N` | Node start to ready |
| `bref-web ready ... fpm_ready_ms=N startup_ms=N` | Rust start to FPM ready and listening |
| `bref_web_fpm request_id=... status=... duration_us=...` | Every request PHP served |
| `bref-web request_id=... gateway_status=S reason=R elapsed_ms=N` | A response the gateway produced itself (`queue_full`, `queue_wait_expired`, `admission_closed`, `body_*`, `backend_failed`, `backend_stopped`, `response_head_timeout`) |
| `bref-web request_id=... http_connection_error=...` | Connection cut off (request deadline, header timeout); `request_id=-` before any request was dispatched |
| `bref-web accept_error=... action=...` | `accept()` failed; `retry` or `pause` |
| `... unresolved_backend=true` then `runtime retiring: backend completion unconfirmed request_id=...` | Retirement: PHP completion unconfirmed |
| `aws_test route=...` | A test route started (test archive only) |

`lambda-web-request-id` becomes the request ID in all of them; the diagnostic
page returns it as `request_id`.

## Cases

Record for each: archive SHA-256, time, request IDs, client-observed status and
timing, diagnostic `environment_id`, and the matching log lines.

### 1. Startup and environment identity (diagnostic)
- Deploy, then `GET /` twice.
- **Measure:** `startup_ms` lines, Lambda's init duration, `environment_id`,
  `first_request_in_environment`, `runtime_configured_at`.
- **Pass:** ready well within the init deadline; the second request reports the
  same `environment_id` with `first_request_in_environment: false`.

### 2. What the ingress sends (diagnostic)
- `GET /` plainly, then with spoofed `X-Forwarded-For: 192.0.2.1`,
  `X-Forwarded-Proto: http`, `X-Forwarded-Port: 80`,
  `lambda-web-request-id: spoofed` and `X-Amzn-Trace-Id: Root=spoofed`.
- **Measure:** `headers`, `cgi` and `process_environment` in the response.
- **Pass:** the ingress supplies the real client IP, `https` and `443`, and a
  request ID and trace ID the client cannot replace.
- **Observed on AWS:** proto, port and request ID are overwritten;
  `X-Forwarded-For` is appended to; the trace ID, `X-Forwarded-Host`,
  `Forwarded` and `X-Real-IP` pass through. See
  [headers on Lambda Web Functions](compatibility.md#headers-on-lambda-web-functions)
  for what applications must trust.

### 3. Request correlation
- Any request; find its ID in the ingress logs, FPM access log and gateway lines.
- **Pass:** one ID ties the request together end to end.

### 4. Streaming (test archive)
- `curl --no-buffer` on `/aws-test/stream?chunks=5&interval_ms=1000`, timestamping
  each line as it arrives.
- **Pass:** lines arrive about a second apart. All at the end means the ingress
  buffers responses.

### 5. Response size (test archive)
- `/aws-test/large?mb=8`, `32` and `64`.
- **Measure:** status, bytes received, any ingress limit or truncation.

### 6. Timeouts (test archive)
- `/aws-test/sleep?ms=25000`, then `ms=29000` and `ms=60000`.
- **Expected:** 25 s and usually 29 s return 200: FPM checks its 28 s limit only
  every 9.33 s (a third of it), so it ends an overrun at 28–37.3 s. An overrun
  returns 502 (`reason=backend_failed`) when FPM ends it before 30 s, otherwise
  504 (on AWS, Lambda's own `Runtime.RequestTimeout` answers first). The runtime
  must **not** retire. The first AWS run found it retiring in most overruns
  (fixed by waiting for FPM's late termination).
- **Measure:** what the client receives, and whether the ingress has its own
  shorter timeout that answers first.

### 7. Concurrency and scaling (test archive)
- Send N parallel `/aws-test/sleep?ms=2000` for N = 4, 8, 16, 32.
- **Measure:** distinct `environment_id`s and PIDs, gateway `queue_full` or
  `queue_wait_expired` 503s, ingress errors, latency.
- **Pass:** AWS adds environments rather than overfilling one; few or no 503s.

### 8. Connection reuse (diagnostic)
- Requests separated by 5 s, 25 s, 31 s and 40 s of idle time (the gateway closes
  idle keep-alive connections after 30.1 s).
- **Measure:** ingress errors (502/connection reset) on the request after a long
  idle gap; gateway `http_connection_error` lines.
- **Pass:** no client-visible errors; the ingress reconnects or retries.

### 9. Worker crash (test archive)
- `/aws-test/crash`, then `GET /`.
- **Pass:** 502, then 200 from the same `environment_id`.

### 10. Retirement and replacement (test archive)
- `/aws-test/stuck` (ignores FPM's SIGTERM), then `GET /` every second for two
  minutes. Repeat three times.
- **Expected:** Lambda's 504 at the 30 s deadline; retirement about 39.3 s after
  the gateway received the request (FPM's worst-case termination, 28 s × 4/3,
  plus 2 s); Rust and Node exit nonzero.
- **Measure:** time until a new `environment_id` answers; what requests receive
  in between; whether repeated retirements are treated as a crash loop.
- **Observed on AWS:** at most one request per retirement gets 503
  `Runtime.Unreachable` (none when other warm environments exist); Lambda starts
  the replacement on demand, with an ordinary cold start. Two retirements 20 s
  apart caused no client-visible errors.
- **Pass:** a replacement environment serves requests without manual action.

### 11. Shutdown
- Leave the function idle until AWS recycles environments.
- **Measure:** `[launcher] stopping: SIGTERM`, time to exit, FPM cleanup.
- **Pass:** clean exit within Lambda's shutdown deadline.

### 12. Memory (test archive)
- `/aws-test/memory?mb=100` and `?mb=200` (per-worker limit 128M), then several
  concurrent 100 MB allocations.
- **Measure:** 500 for the limit; environment memory; out-of-memory behaviour.

### 13. Credentials and secrets
- `aws_credentials_present` in the diagnostic response.
- `bref-ssm:` secrets need an application with `bref/secrets-loader`; the
  Laravel app from case 14 includes it, and the stack gives it a `bref-ssm:`
  parameter that its probe reports as `ssm_probe`.

### 14. Laravel and Symfony
- Build with [`tests/frameworks/setup.sh`](../tests/frameworks/), package with
  the CI archive's `runtime/` and gateway as `laravel.zip` and `symfony.zip`
  (Symfony with `--keep-dotenv`) plus `.sha256` files, deploy with
  `-c apps=diagnostic,test,laravel,symfony`, and call the probe routes, also with
  spoofed `X-Forwarded-For` and `X-Forwarded-Host`.
- **Pass:** the same results as the local [framework comparison](compatibility.md#framework-bridges)
  (HTTPS URLs, client IP, sessions, redirects, uploads, errors in the logs), and
  `ssm_probe` resolved. Spoofed headers must not change the client IP or host
  once the application trusts only what the ingress controls, or with
  `-c forwardedHeaders=ingress` for an unmodified application.

## Local rehearsal

[`tests/aws/local_check.py`](../tests/aws/local_check.py) runs the test archive
in the managed Node image and checks the routes, the log lines above and real
PHP-FPM retirement. CI runs it on both architectures. It is not AWS evidence.
