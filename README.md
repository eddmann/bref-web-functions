# Bref on Lambda Web Functions (proof of concept)

A proof of concept for running PHP applications on
[AWS Lambda Web Functions](https://docs.aws.amazon.com/lambda/latest/dg/web-functions.html)
with [Bref](https://bref.sh)'s PHP distribution, PHP-FPM and framework bridges.
It aims to change as little of Bref as possible: applications keep their
`php.ini` conventions, `php/conf.d/`, `beforeStartup` hooks, `bref-ssm:` secrets
and the Laravel and Symfony bridges.

Web Functions send real HTTP requests to a long-running process, and one
execution environment serves many requests at once. This runtime forwards those
requests to a pool of PHP-FPM workers through a small Rust HTTP-to-FastCGI gateway:

```text
Lambda HTTP ingress -> Rust gateway -> PHP-FPM workers (12 by default) -> public/index.php
```

**Status:** proof of concept, not for production. Verified on AWS (ARM64) with
Laravel 13 and Symfony 7.4 applications; see [testing](docs/testing.md) and the
[benchmark against Bref's event runtime](benchmarks/event-vs-web/). There is no
serverless plugin or CDK construct support: deployment uses the test stack in
[`infra/aws`](infra/aws/).

## How it works

Web Functions support only the managed Node.js runtime, so a small Node launcher
starts first. PHP then prepares the environment and hands over to the gateway:

```text
Node 24 (managed runtime) loads index.js
  -> php -n compat/bootstrap.php   validate BREF_WEB_* settings, write php-fpm.conf
  -> php compat/configure.php      write the runtime INI (application INI wins)
  -> php compat/init.php           Composer autoload, bref-ssm: secrets, beforeStartup
  -> exec bin/bref-web-gateway     start PHP-FPM, listen for HTTP
```

Each step replaces the previous process, so secrets loaded during initialization
reach the gateway without files or IPC. The gateway admits a bounded number of
requests, maps HTTP to FastCGI as Bref's FPM runtime does, streams responses,
enforces deadlines, and retires the environment if a PHP worker cannot be
confirmed stopped. See [architecture](docs/architecture.md).

What differs from Bref's FPM runtime is listed in [compatibility](docs/compatibility.md):
notably the raw query string reaches PHP unchanged, `afterStartup` and the
invocation hooks are not supported, and file sessions are per environment.

## Requirements

- Docker
- Node.js 22 or later (packaging)
- Python 3 (test harnesses)
- Rust, only to run `cargo test` outside Docker

## Build and test

```sh
./scripts/build.sh                 # gateway + Bref PHP image, tagged bref-web-functions:local
./runtime/php/tests.sh             # PHP startup steps, in Bref's PHP image
python3 packaging/tests.py         # packager, archive extraction, Node entry
cargo test --locked                # gateway unit tests
```

`./scripts/build.sh` builds for x86_64; pass
`--build-arg RUST_TARGET=aarch64-unknown-linux-musl --build-arg BREF_IMAGE=bref/arm-php-84:3`
for ARM64.

## Package an application

Export Bref's PHP distribution and the gateway from the image, then package a
production build of the application:

```sh
mkdir -p build
c=$(docker create bref-web-functions:local)
docker cp "$c:/opt" build/runtime
docker cp "$c:/usr/local/bin/bref-web-gateway" build/bref-web-gateway
docker rm "$c"

node scripts/package.mjs --runtime-dir build/runtime --rust-binary build/bref-web-gateway \
  --architecture x86_64 --output build/example.zip
python3 scripts/test-package.py build/example.zip
```

This packages the bundled example application and runs the archive exactly as
deployed, in the managed Node image. To package your own application, add
`--app-dir path/to/app` (a production build; the front controller defaults to
`public/index.php`).

Web Functions run on ARM64 only: deployable archives need ARM64 inputs and
`--architecture aarch64`. See the [packaging guide](packaging/README.md).

## Deploy to AWS

[`infra/aws`](infra/aws/) is a CDK stack that deploys archives as Web Functions
with public endpoints, for testing in an isolated AWS account. It creates
billable resources; `npx cdk destroy` removes them.

## Configuration

Settings are environment variables such as `BREF_WEB_MAX_IN_FLIGHT` (PHP-FPM
workers, default 12), `BREF_WEB_PHP_MEMORY_LIMIT` (default `128M` per worker) and
`BREF_WEB_REQUEST_TIMEOUT_MS`. See [runtime settings](docs/compatibility.md#runtime-settings)
and [memory sizing](docs/compatibility.md#memory-sizing).

## Repository layout

| Path | Contents |
| --- | --- |
| `src/` | Rust HTTP-to-FastCGI gateway and PHP-FPM supervisor |
| `runtime/launcher/` | Node entry for the managed runtime |
| `runtime/php/` | PHP startup steps |
| `scripts/` | Build, package and archive test commands |
| `packaging/` | Archive extraction for tests, and packaging tests |
| `tests/` | HTTP, lifecycle, hardening, Bref parity, framework and AWS checks |
| `examples/` | Smoke application and an AWS diagnostic page |
| `infra/aws/` | CDK test stack |
| `benchmarks/event-vs-web/` | Comparison with Bref's event runtime |
| `upstream/` | A proposed fix for `bref/symfony-bridge` |
| `docs/` | Architecture, compatibility, testing, AWS test plan |

## License

[MIT](LICENSE)
