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
| `LAMBDA_REQUEST_ID` | Passed per request from `lambda-web-request-id` when it is 1–256 `[A-Za-z0-9._:-]` characters; otherwise a generated UUID |
| Legacy invocation/API Gateway context and X-Ray context contract | Unsupported |
| Binary bodies, repeated cookies, multipart and chunked uploads | Covered by the shared HTTP tests |
| Response streaming | Verified locally and on AWS: chunks reach the client as written |
| File and non-file session configuration | Preserved. File sessions live in one environment's `/tmp`, and Lambda spreads a client's requests over warm environments (on AWS, six consecutive requests reached six environments): use cookie, DynamoDB or Redis sessions, as Bref recommends for event functions |
| Real AWS credentials and `bref-ssm:` secrets | Verified on AWS (Laravel with `bref/secrets-loader` resolved an SSM parameter at startup); credential refresh during a long-lived environment unverified |
| Laravel 13 + `bref/laravel-bridge` 3.1.6, Symfony 7.4 + `bref/symfony-bridge` 3.0.3 | Same behaviour as Bref's FPM runtime in local parity runs ([framework bridges](#framework-bridges)); the same probes pass on AWS. Laravel needs [trusted-proxy configuration](#headers-on-lambda-web-functions) |
| Native ARM64 runtime and exact archive execution | Verified in CI |
| Actual Lambda Web Functions deployment | Verified on AWS (ARM64, us-west-1): deploy, update to a new revision without errors, scaling, retirement and replacement, shutdown |

FPM resets ordinary PHP request state between requests, including reused workers.
OPcache, temporary files, sessions and external services can still be shared.
Applications must tolerate several requests using those resources simultaneously.
Durable data belongs in external storage rather than disposable `/tmp` files.

## Alignment with Bref's FPM runtime

`tests/bref-parity/run.py` runs Bref 3.0.15's own `FpmHandler` with API Gateway
HTTP API events beside this gateway and reports every difference PHP can see.

Aligned with Bref:

- `REMOTE_ADDR` and `SERVER_ADDR` are `127.0.0.1`, so Bref's documented
  `trusted_proxies: '127.0.0.1'` keeps trusting `X-Forwarded-*` headers. Trust
  only the headers Lambda's ingress controls; see
  [headers on Lambda Web Functions](#headers-on-lambda-web-functions).
  `REMOTE_PORT` and `SERVER_PORT` follow `X-Forwarded-Port`.
- A request body without `Content-Type` is form data, and `HTTP_CONTENT_TYPE`
  and `HTTP_CONTENT_LENGTH` are present alongside the CGI variables.
- `_X_AMZN_TRACE_ID` comes from the `X-Amzn-Trace-Id` header when present. On
  Web Functions the client controls that header.
- Application `php/conf.d` (or `PHPRC`) directives win over runtime defaults.
- `beforeStartup`, `BREF_AUTOLOAD_PATH`, `bref-ssm:` secrets and
  `fastcgi_finish_request` disabled by default.

Deliberately different:

| Bref | This adapter | Why |
| --- | --- | --- |
| `SERVER_PORT` 80 without `X-Forwarded-Port` | 443 | The ingress is HTTPS; `https` on port 80 makes wrong absolute URLs |
| No `HTTPS` variable | `HTTPS=on`, `REQUEST_SCHEME=https` | Correct scheme even without trusted-proxy configuration |
| Query string rebuilt from parsed parameters; `?a=1&a=2` becomes an array | Raw `QUERY_STRING`/`REQUEST_URI`, native PHP parsing | Signed URLs and webhook signatures need the original bytes |
| `memory_limit=10240M` | `128M` per worker unless the application sets it | Several workers share one environment's memory |
| One FPM worker | `BREF_WEB_MAX_IN_FLIGHT` workers (default 12) | Concurrency is the point; set 1 for single-request applications |
| Application `disable_functions` replaces Bref's | Merged; `fastcgi_finish_request` always disabled | Early completion would release a worker the gateway still needs |
| Underscore request header names passed | Dropped | `X_Foo` could otherwise replace `X-Foo` set by a proxy |
| Timeout: restart FPM, Lambda error | FPM terminates the worker, up to a third of its limit late; the client gets 502 if that is before the HTTP deadline, otherwise 504 | Restarting FPM would kill concurrent requests |
| Communication failure: restart FPM in place | Unconfirmed completion retires the runtime (drain, stop FPM, exit nonzero) for platform replacement | One stuck worker must not kill concurrent requests; replacement on AWS is unverified |
| `PATH_INFO` encoded, no `SCRIPT_NAME` | Decoded `PATH_INFO`, `SCRIPT_NAME=/index.php` | Conventional nginx front-controller values |
| `LAMBDA_INVOCATION_CONTEXT`, `LAMBDA_REQUEST_CONTEXT` | Absent | Not supplied by Web Functions; not synthesized |
| `afterStartup`, `beforeInvoke`, `afterInvoke` | Not called | No invocation loop; requests run in FPM workers |
| `BREF_RUNTIME=function` HTTP handlers (PSR-15, Octane) | Unsupported | Only PHP-FPM is bridged |
| `_HANDLER` selects the front controller | Package `--front-controller` / `BREF_WEB_SCRIPT_FILENAME` | `_HANDLER` belongs to the managed Node runtime |

## Headers on Lambda Web Functions

Measured on AWS for a client sending its own values (see the AWS test plan, case 2):

| Header | Lambda's ingress | What PHP sees |
| --- | --- | --- |
| `X-Forwarded-Proto`, `X-Forwarded-Port` | Overwritten (`https`, `443`) | Safe to trust |
| `X-Forwarded-For` | Appended: the client's value, then the address the ingress saw | Only the **right-most** entry is reliable |
| `lambda-web-request-id` | Overwritten | Safe (`LAMBDA_REQUEST_ID`) |
| `X-Forwarded-Host`, `Forwarded`, `X-Real-IP` | Passed through unchanged | Client-controlled |
| `X-Amzn-Trace-Id` | Passed through; not set by the ingress | Client-controlled (`_X_AMZN_TRACE_ID`) |
| `X_Forwarded_For` (underscores) | Not observed separately | Never reaches PHP: the gateway drops underscore names |

Every request reaches PHP from `127.0.0.1`, so trusting that address trusts the
client for any header the ingress does not overwrite. Trust `127.0.0.1` and only
`X-Forwarded-For`, `-Proto` and `-Port`; frameworks then take the right-most
`X-Forwarded-For` entry as the client IP:

- **Symfony:** Bref's documented configuration already does this
  (`trusted_proxies: '127.0.0.1'`, `trusted_headers: [x-forwarded-for, x-forwarded-proto, x-forwarded-port]`).
  Verified on AWS: spoofed headers change neither the client IP nor the host.
- **Laravel:** `bref/laravel-bridge` trusts every address (`0.0.0.0/0`) and
  Laravel trusts `X-Forwarded-Host` and `X-Forwarded-Prefix` by default. On AWS
  a client could set the IP Laravel reports (`192.0.2.1`) and the host and path
  prefix used in generated URLs and redirects (`https://evil.example/evil/...`).
  Use the gateway's ingress mode below, or override it in `bootstrap/app.php`
  (verified with the spoofed request: real IP and host, redirect unchanged):

  ```php
  ->withMiddleware(function (Middleware $middleware): void {
      $middleware->trustProxies(at: '127.0.0.1', headers: Request::HEADER_X_FORWARDED_FOR
          | Request::HEADER_X_FORWARDED_PROTO | Request::HEADER_X_FORWARDED_PORT);
  })
  ```

### Ingress mode

When nothing sits in front of the Web Function endpoint, set
`BREF_WEB_FORWARDED_HEADERS=ingress`. The gateway then passes PHP only what
Lambda's ingress controls: `X-Forwarded-For` reduced to its right-most entry,
`X-Forwarded-Proto` and `X-Forwarded-Port`. It drops `X-Forwarded-Host`,
`X-Forwarded-Prefix`, `X-Forwarded-Server`, `Forwarded`, `X-Real-IP` and
`X-Amzn-Trace-Id` (so `_X_AMZN_TRACE_ID` is unset). An application that trusts
every proxy, such as a stock Laravel app with the Bref bridge, then reports the
real client IP and host with no configuration change (verified on AWS with the
spoofed headers above, including `X-Forwarded-Prefix`).

The default, `passthrough`, keeps every header as Bref does. Keep it when a proxy
sits in front of the endpoint (for example CloudFront setting `X-Forwarded-Host`
for a custom domain): ingress mode would drop that host and report the proxy's
address as the client IP. Then trust only what that proxy overwrites, and note
its address is the right-most `X-Forwarded-For` entry.

## Framework bridges

[`tests/frameworks/`](../tests/frameworks/) runs real applications under Bref's
own `FpmHandler` and as the exact deployment ZIP on this gateway. With Laravel
13.40 + `bref/laravel-bridge` 3.1.6 and Symfony 7.4.23 + `bref/symfony-bridge`
3.0.3 (Bref 3.0.15), all eleven requests per framework matched on status,
redirect `Location`, cookie attributes, bodies and error logging: client IP from
`X-Forwarded-For`, HTTPS URLs, form/JSON/multipart input, Basic auth, persisted
sessions, streamed responses and 404/500 handling. The only differences were the
documented query-string rewriting and, under Symfony, `getContentTypeFormat()`
returning `form` for a GET under Bref because its FastCGI client always sends
`CONTENT_TYPE`.

The Laravel bridge works because `beforeStartup` runs before the gateway starts:
it creates `/tmp/storage`, runs `artisan config:cache` with the relocated `php`,
and exports `APP_CONFIG_CACHE` to the FPM workers. The Symfony bridge uses
`LAMBDA_TASK_ROOT` to move the cache to `/tmp/cache`, symlinking a cache warmed
at build time (outside Lambda mode, so without `LAMBDA_TASK_ROOT`).

**Symfony and `.env`:** the packager omits `.env` and `.env.*` by default, and a
Symfony application that loads Dotenv then fails on every request (`Unable to
read the "/var/task/.env" environment file`). Package Symfony applications with
`--keep-dotenv`. Both Bref's documented production recipe
(`echo "<?php return [];" > .env.local.php`, with configuration in environment
variables) and the skeleton's committed `.env` were verified that way. Laravel's
`.env` usually holds secrets such as `APP_KEY`; keep the default and configure
Laravel through environment variables.

## Runtime settings

Set these environment variables before initialization. In AWS, application
environment variables belong to a Web Function revision. These are this project's
settings, not additional native Lambda worker controls.

| Setting | Default | Purpose |
| --- | --- | --- |
| `BREF_WEB_MAX_IN_FLIGHT` | `12` | FPM children and Rust active backend reservations; set Lambda's `maxConcurrencyPerEnvironment` to the same value |
| `BREF_WEB_QUEUE_CAPACITY` | `4` | Extra admitted requests waiting for a worker |
| `BREF_WEB_QUEUE_TIMEOUT_MS` | `1000` | Maximum Rust queue wait |
| `BREF_WEB_REQUEST_TIMEOUT_MS` | `30000` | Rust HTTP deadline, including queue/body time; transport cutoff adds 100 ms terminal-response grace |
| `BREF_WEB_FPM_TIMEOUT_SECONDS` | `28` | FPM per-worker wall timeout; FPM enforces it up to a third late |
| `BREF_WEB_FORWARDED_HEADERS` | `passthrough` | `ingress` keeps only the forwarding headers Lambda's ingress controls; see [headers](#headers-on-lambda-web-functions) |
| `BREF_WEB_PHP_MEMORY_LIMIT` | `128M` | Per-worker `memory_limit` unless application INI sets it |
| `BREF_WEB_OPCACHE_MEMORY_MB` | `128` | `opcache.memory_consumption` unless application INI sets it |
| `BREF_WEB_MAX_BODY_BYTES` | `6291456` | Gateway body limit, in exact bytes; also PHP post/upload limits unless application INI sets them |
| `BREF_WEB_FPM_MAX_REQUESTS` | `100` | Requests before an FPM child is recycled |
| `BREF_WEB_SCRIPT_FILENAME` | `/var/task/public/index.php` | PHP front controller |
| `BREF_WEB_DOCUMENT_ROOT` | `/var/task/public` | CGI document root |
| `BREF_WEB_INIT_FILE` | Unset | Optional application initialization file |

The archive's Node entry derives the controller and document root from the
package definition (`--front-controller`, `--document-root`). See
[request mapping](architecture.md#request-mapping) for custom or nested
controllers. `BREF_WEB_RUNTIME_ROOT` defaults to `/opt` (Bref's image) and is set
to the archive's relocated `runtime/` directory by the Node entry.

### Memory sizing

A Web Function environment has 2 GB, shared by every PHP worker, OPcache and the
gateway; one that runs out is stopped with all its requests. Keep

```text
workers × memory_limit + OPcache + about 70 MB  ≤  2048 MB
```

The defaults (12 × 128 MB + 128 MB + 70 MB ≈ 1.73 GB) fit even when every worker
is at its limit. Measured in the managed Node image capped at 2 GB, with every
worker allocating 125 MB at once (126 MB exceeds `memory_limit`):

| Workers | Peak memory | Out-of-memory kills |
| --- | --- | --- |
| 4 | 531 MB | 0 |
| 12 | 1,558 MB | 0 |
| 16 | 1,781 MB | 0, but its worst case (about 2.1 GB) exceeds the environment |

A Laravel application under normal load peaked at 126 MB with 4 workers and
171 MB with 16. On AWS, Lambda routed at most 10 concurrent requests to one
environment when allowed 16 or more, and at most 8 when allowed 12, throttling the
rest; so 12 workers loses little capacity. Saturating ten environments with
125 MB requests held for 3 s, the busiest environment peaked at 1,214 MB with 8
requests in flight, with no failures. For
more workers, lower `BREF_WEB_PHP_MEMORY_LIMIT` to keep within the formula; for
requests that need more memory, use fewer workers.

## INI precedence and limits

The helper includes the runtime's defaults and application `PHP_INI_SCAN_DIR`
directories, then generates a final file. As with Bref, any directive set in
application INI (or an application `PHPRC`) keeps its value; startup logs the
names as `bref_web_compat application_ini=...`. Directives the application leaves
unset get this adapter's defaults for logging, buffering, writable temporary
paths, memory, OPcache and input limits, replacing the runtime's own values.
Only `extension_dir` (the relocated runtime) and `fastcgi_finish_request` in
`disable_functions` are always enforced; other disabled functions are merged.

An application `post_max_size` above `BREF_WEB_MAX_BODY_BYTES` does not raise
the gateway limit, which rejects larger bodies first, much as Lambda's payload
limit does under Bref.

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
