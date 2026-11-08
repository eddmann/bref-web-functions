# Configuration and compatibility

This adapter reuses Bref's PHP/FPM foundation while replacing the event-runtime
transport. It does not run Bref's normal event polling loop or `FpmHandler`
concurrently. Compatibility with the PHP distribution is broader than the subset
of Bref lifecycle hooks currently implemented.

## Current contract

| Area | Status |
| --- | --- |
| Bref PHP 8.4 and ordinary FPM request cleanup | Verified in native AMD64 and ARM64 CI |
| Composer and actual Bref 3.0.15 classes | Verified with the pinned test fixture |
| Application `php/conf.d/` and configured native extensions | Verified locally, including `intl` |
| `beforeStartup` | Called by the initialization helper; environment changes carry forward |
| Initialization PHP object/global memory | Not preserved after the helper execs Rust |
| `afterStartup`, `beforeInvoke`, `afterInvoke` | Unsupported |
| `LAMBDA_REQUEST_ID` | Passed per request from `lambda-web-request-id`, with a UUID fallback locally |
| Legacy invocation/API Gateway context and X-Ray context contract | Unsupported |
| Binary bodies, repeated cookies, multipart and chunked uploads | Covered by the shared HTTP tests |
| Response streaming | Verified locally; real AWS ingress remains unverified |
| File and non-file session configuration | Preserved; an external session backend has not been exercised |
| Real AWS credentials, refresh and secrets retrieval | Unverified |
| Full Laravel/Symfony applications | Unverified |
| Native ARM64 runtime and exact archive execution | Verified in CI |
| Actual Lambda Web Functions deployment | Unverified |

FPM resets ordinary PHP request state between requests, including reused workers.
OPcache, temporary files, sessions and external services can still be shared.
Applications must tolerate several requests using those resources simultaneously.
Durable data belongs in external storage rather than disposable `/tmp` files.

## Runtime settings

Set these environment variables before initialization. In AWS, application
environment variables belong to a Web Function revision. These are this project's
settings, not additional native Lambda worker controls.

| Setting | Default | Purpose |
| --- | --- | --- |
| `BREF_WEB_MAX_IN_FLIGHT` | `4` | FPM children and Rust active backend reservations |
| `BREF_WEB_QUEUE_CAPACITY` | `4` | Extra admitted requests waiting for a worker |
| `BREF_WEB_QUEUE_TIMEOUT_MS` | `1000` | Maximum Rust queue wait |
| `BREF_WEB_REQUEST_TIMEOUT_MS` | `30000` | Rust HTTP deadline, including queue/body time; transport cutoff adds 100 ms terminal-response grace |
| `BREF_WEB_FPM_TIMEOUT_SECONDS` | `28` | FPM per-worker wall timeout |
| `BREF_WEB_PHP_MEMORY_LIMIT` | `128M` | PHP allocation limit per worker |
| `BREF_WEB_OPCACHE_MEMORY_MB` | `128` | Shared OPcache budget |
| `BREF_WEB_MAX_BODY_BYTES` | `6291456` | Gateway and PHP upload/post limits, in exact bytes |
| `BREF_WEB_FPM_MAX_REQUESTS` | `100` | Requests before an FPM child is recycled |
| `BREF_WEB_SCRIPT_FILENAME` | `/var/task/public/index.php` | PHP front controller |
| `BREF_WEB_DOCUMENT_ROOT` | `/var/task/public` | CGI document root |
| `BREF_WEB_INIT_FILE` | Unset | Optional application initialization file |

Archive entrypoints derive the controller/root paths from the package definition.
See [front-controller metadata](rust-gateway.md) for custom/nested controllers.
`BREF_WEB_RUNTIME_ROOT` defaults to `/opt` in the local container and is set to the
archive's relocated `runtime/` directory by its Node entrypoint.

Match the initial AWS concurrency upper bound to the tested PHP pool size, then
measure before increasing it. AWS can route fewer requests than that upper bound.
More workers share the same environment resources; neither the PHP allocation
limit nor OPcache settings alone guarantee total process memory fits.

## INI precedence and limits

The helper includes the runtime's defaults and application `PHP_INI_SCAN_DIR`
directories, then generates a final override. Runtime paths, logging, buffering,
writable temporary paths, memory, OPcache and input limits deliberately take
precedence. Use the environment settings above for those budgets. Unrelated INI,
explicit TLS CA settings and additional disabled functions are preserved.

The body limit is a positive integer through 2,147,483,647 bytes, not a memory-fit
guarantee. Multipart framing consumes part of the total request limit. Rust
buffers the admitted request body; this version does not stream uploads into FPM.

`fastcgi_finish_request` stays disabled so PHP cannot deliberately report
completion while hidden work continues. HTTP expiry/client disconnect does not
immediately cancel PHP; backend reservations remain held until completion is
confirmed. A connection watchdog cuts off stalled writes independently of body
polling. If completion still cannot be confirmed, the runtime closes admission,
drains within its shutdown budget and exits nonzero for replacement by its
supervisor/platform. This does not add immediate PHP cancellation or an in-place
pool restart. See [architecture](architecture.md) for deadline and shutdown details.

CLI commands, queues and other event handlers should continue to use the existing
Bref event/console runtimes. Extra PHP extensions must be bundled for the selected
architecture and ABI; a conventional Lambda layer is not a Web Function package.
