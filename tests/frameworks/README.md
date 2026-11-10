# Framework bridge parity

Runs a real Laravel or Symfony application with its Bref bridge two ways and
reports every difference the application can observe:

- **Bref:** the application's own `Bref\FpmRuntime\FpmHandler`, after the same
  startup sequence as `FpmRuntime\Main` (secrets, `beforeStartup`), driven with
  API Gateway HTTP API events in the Bref image with Bref's stock FPM/INI files.
- **Adapter:** the exact deployment ZIP from `scripts/package.mjs`, extracted
  read-only into the managed Node 24 image, with HTTP sent to the Rust gateway.

Both sides get the same Lambda environment variables, application and requests:
probes of the framework's request view (client IP, scheme, host, port, URL
generation, query/form/JSON/multipart input, Basic auth, storage and cache
paths), a two-request session, a redirect, a thrown exception, a streamed
response and a 404.

```sh
./scripts/build.sh                        # local image, then export build/ci/runtime
tests/frameworks/setup.sh laravel /tmp/parity-laravel
tests/frameworks/setup.sh symfony /tmp/parity-symfony
python3 tests/frameworks/run.py laravel /tmp/parity-laravel --output results/frameworks/laravel
python3 tests/frameworks/run.py symfony /tmp/parity-symfony --output results/frameworks/symfony
```

`run.py` expects `build/ci/runtime` and `build/ci/bref-web-gateway` exported from
the tested image, as in CI. `setup.sh` uses `--prefer-source` so it also works
where GitHub archive downloads are blocked; Packagist must be reachable.

The `overlay/` directories hold the only application changes: Bref's documented
Symfony kernel and trusted-proxy settings, and probe routes for both frameworks.
The Symfony build follows Bref's production recipe (`.env.local.php` returning
an empty array) and is packaged with `--keep-dotenv`.

Concurrent first requests on fresh environments (several PHP-FPM workers
preparing framework caches at once) are checked separately:

```sh
python3 tests/frameworks/cold_start.py symfony /tmp/parity-symfony --architecture x86_64
```

Symfony fails this until [the upstream patch](../../upstream/symfony-bridge/) is
applied to `bref/symfony-bridge`.
