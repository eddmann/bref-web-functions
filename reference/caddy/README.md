This reference implementation uses Caddy for HTTP/FastCGI and Bref PHP-FPM for PHP.
The shared Node 24 entry file only launches and supervises processes. It serves no HTTP
and does not call private Lambda lifecycle APIs. Select `BREF_WEB_GATEWAY=rust`
(the default) or `caddy`.

In Rust mode it starts `BREF_WEB_RUST_BIN` (default
`/usr/local/bin/bref-web-gateway`); Rust owns PHP-FPM. In Caddy mode it starts the
common FPM configuration, waits for its TCP listener, writes
`/tmp/bref-web/caddy.json`, and starts `BREF_WEB_CADDY_BIN` (default
`/usr/local/bin/caddy`). Both gateways bind the exact
`AWS_LAMBDA_HTTP_ENDPOINT`, with `0.0.0.0:8080` as the local default. The shared
`runtime/php/` entrypoint prepares FPM configuration and environment.

The Caddy baseline uses plain HTTP/1.1, with automatic HTTPS, the admin listener,
configuration persistence and response compression disabled. It sends every
application route to the fixed front controller (default
`/var/task/public/index.php`), preserving the original request URI and query.
`GET /__bref_web/health` is served by the gateway without running PHP. The real
Lambda `lambda-web-request-id` header becomes the per-request FastCGI
`LAMBDA_REQUEST_ID`; requests made locally without that header get a UUID. This
does not synthesize the Event Function invocation context. CGI sets `HTTPS=on`,
`REQUEST_SCHEME=https` and `SERVER_PORT=443` because this prototype models Lambda's
TLS-terminated ingress, including in local tests. It does not trust forwarded
scheme headers. `PATH_INFO` receives the original request path; `REQUEST_URI`
continues to preserve the original escaped path and raw query.

The default request limit is 6 MiB, controlled by `BREF_WEB_MAX_BODY_BYTES`.
The request buffer is that limit plus one byte, so Caddy can observe EOF for an
exactly-at-limit chunked body. An over-limit body is still rejected by the body
limit. A native Caddy error route handles only its internal FastCGI missing-length
411 and verifies the original body's sticky size-limit error before returning
413. Other internal 411 errors keep their status, and application-generated 411
responses never enter this error route. The diagnostic matching is pinned to
Caddy 2.11.9; rerun the body-limit tests when upgrading. Successful requests do
not perform an extra body read. The local checks cover small, exactly-at-limit
and oversized chunked bodies, declared oversized bodies, and PHP 411 responses.
The positive 10 ms proxy flush interval supports
streaming without opting into Caddy's negative-interval cancellation policy.
The upstream request cap is `BREF_WEB_MAX_IN_FLIGHT + BREF_WEB_QUEUE_CAPACITY`
(default 8). It bounds FPM requests but is not Rust's separate active-request
semaphore and one-second queue. Overload behavior must be reported separately;
benchmark at or below the common FPM pool size for a comparable steady state.
FastCGI read/write deadlines use `BREF_WEB_REQUEST_TIMEOUT_MS` (default 30 seconds).
These are upstream deadlines, not a claim of identical end-to-end timeout policy.

On shutdown, Node sends SIGQUIT to FPM's dedicated process group, SIGTERM to Caddy
or Rust, and SIGKILL to survivors after `BREF_WEB_SHUTDOWN_TIMEOUT_MS` (default
900 ms). Rust receives `BREF_WEB_SHUTDOWN_GRACE_MS` (default 500 ms); the launcher
requires at least a 300 ms margin before its own deadline. An unexpected
essential-child exit stops the whole runtime with a nonzero exit code. The
startup deadline is `BREF_WEB_STARTUP_TIMEOUT_MS` (default 8 seconds). Increase
these settings only for local diagnostics; AWS's documented initialization and
shutdown deadlines still apply.

Generate the exact JSON without launching processes:

```sh
BREF_WEB_GATEWAY=caddy node runtime/launcher/index.js --print-caddy-config > /tmp/caddy.json
caddy validate --config /tmp/caddy.json
```

The generated configuration was validated against official Caddy 2.11.9.
Relevant upstream definitions: [request-body limit](https://github.com/caddyserver/caddy/blob/v2.11.9/modules/caddyhttp/requestbody/requestbody.go),
[FastCGI variables and original URI](https://github.com/caddyserver/caddy/blob/v2.11.9/modules/caddyhttp/reverseproxy/fastcgi/fastcgi.go),
[proxy buffering and flush interval](https://github.com/caddyserver/caddy/blob/v2.11.9/modules/caddyhttp/reverseproxy/reverseproxy.go),
and [path-only rewrite preserves query](https://github.com/caddyserver/caddy/blob/v2.11.9/modules/caddyhttp/rewrite/rewrite.go).

Configuration generation is isolated in `reference/caddy/config.cjs`. The shared
launcher loads it only when Caddy mode or `--print-caddy-config` is selected; Rust
deployment ZIPs do not include Caddy configuration code. Source/container layouts
preserve the repository paths under `/var/task/runtime/launcher` and
`/var/task/reference/caddy`.
