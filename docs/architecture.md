# Architecture

Rust provides the HTTP/FastCGI gateway and supervises Bref PHP-FPM. The PHP
distribution and extensions remain separate bundled files. Each FPM child runs
one ordinary PHP request at a time; the pool provides concurrency.

## Managed-runtime startup

```text
AWS starts managed Node 24
  -> shared Node supervisor starts PHP initialization
  -> configuration is written under /tmp/bref-web
  -> PHP loads Composer, optional Bref secrets and beforeStartup
  -> PHP execs Rust, preserving its initialized environment
  -> Rust starts FPM and waits for readiness
  -> Rust listens on AWS_LAMBDA_HTTP_ENDPOINT
```

Node stays alive as the outer supervisor. It checks readiness, detects an
unexpected Rust exit and forwards shutdown signals. HTTP requests do not pass
through Node. The initialization child does not need to serialize secrets through
files or return them to Node.

The local comparison container uses a different outer entrypoint: the same PHP
helper initializes first and execs Node. In either path, initialization finishes
before application traffic is accepted.

## Request path

```mermaid
flowchart LR
    Ingress[Lambda HTTP ingress] --> HTTP[Hyper HTTP/1 server]
    HTTP --> Admission[Active and queue limits]
    Admission --> CGI[HTTP-to-CGI mapping]
    CGI --> FastCGI[FastCGI connection]
    FastCGI --> Pool[Bref PHP-FPM pool]
    Pool --> App[PHP front controller]
```

Rust uses Hyper for HTTP and a single-thread Tokio runtime for asynchronous I/O,
timers and process events. A small owned FastCGI codec uses a fresh connection for
each request; it does not multiplex several PHP requests onto one connection.

The gateway admits work before collecting the request body. Default limits are
four active PHP reservations and four queued requests, with a one-second queue
wait. Bodies are fully buffered within the configured limit. CGI parameters carry
per-request metadata; request IDs are not written into shared process environment.
Responses stream through bounded buffers with backpressure.

A backend task owns its reservation independently of the frontend response.
Disconnecting or reaching an HTTP deadline does not prove PHP stopped. The task
keeps accounting for backend work until FastCGI completion or termination is
confirmed. Unconfirmed work currently closes admission until runtime restart.

## Process lifecycle

Rust starts FPM in a dedicated process group. FPM enforces its per-worker request
timeout and replenishes failed workers. A single worker failure does not require
restarting the entire pool. An unexpected FPM master exit stops the Rust runtime.

The default Rust shutdown grace is 500 ms, inside Node's 900 ms outer deadline.
Surviving FPM processes are killed as a group. Abrupt process death cannot run
cleanup guards, and graceful shutdown does not guarantee durable work completion.

## Code boundaries

| File | Responsibility |
| --- | --- |
| [`src/main.rs`](../src/main.rs) | FPM supervision, HTTP connections and shutdown |
| [`src/gateway.rs`](../src/gateway.rs) | Admission, deadlines, bodies, streaming and backend accounting |
| [`src/cgi.rs`](../src/cgi.rs) | HTTP/CGI mapping and PHP response headers |
| [`src/fcgi.rs`](../src/fcgi.rs) | FastCGI framing and validation |
| [`src/config.rs`](../src/config.rs) | Environment-based configuration |

The [Caddy reference](../reference/caddy/) uses the same Node/PHP preparation and
FPM settings with Caddy's FastCGI transport. Its upstream request cap and deadlines
are not identical to Rust's admission/accounting policy.
