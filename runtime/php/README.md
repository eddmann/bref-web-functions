# PHP startup steps

Run in order, each `exec`ing the next (see [architecture](../../docs/architecture.md#startup)):

| Step | Runs with | Does |
| --- | --- | --- |
| `bootstrap.php` | `php -n` (no INI) | Validates `BREF_WEB_*` settings (exit 78, `configuration_error=<reason>`), derives paths, writes `php-fpm.conf` |
| `configure.php` | Application INI loaded | Writes the runtime INI; application directives win |
| `init.php` | Runtime INI loaded | Composer autoload, `bref-ssm:` secrets, `BREF_WEB_INIT_FILE`, `beforeStartup`, then execs the gateway |

The steps configure PHP-FPM and the gateway against Bref's PHP distribution.
`BREF_WEB_RUNTIME_ROOT` defaults to `/opt`; a deployment archive can set it to
`/var/task/runtime` containing the original `/opt` directory's contents. The PHP
binary, FPM default, library search path, PHP configuration and extension directory
are derived from that root. `LAMBDA_TASK_ROOT` defaults to `/var/task` and can be
changed for archive tests. Writable configuration, file sessions, uploads and
OPcache files live under `/tmp/bref-web`.

The default `PHPRC` points to the runtime's `bref/etc/php` directory; an explicit
application `PHPRC` is preserved. Application `PHP_INI_SCAN_DIR` directories are
preserved, the actual runtime defaults are always included, and the original
`/opt/bref/etc/php/conf.d` entry maps to the relocated directory. Duplicate paths
and a stale generated override are excluded before reading application settings.
The steps resolve each other relative to their own directory, so they work from
an extracted archive.

The Rust gateway starts FPM with `BREF_WEB_FPM_BIN`,
`BREF_WEB_FPM_CONFIG` and `BREF_WEB_FPM_ADDR`. The common static pool is sized by
`BREF_WEB_MAX_IN_FLIGHT` (default 12); each child recycles after 100 requests and has
a 128M PHP allocation limit. OPcache has a separate shared 128MB budget. Together
these fit a 2 GB environment even with every worker at its limit; see
[memory sizing](../../docs/compatibility.md#memory-sizing).

`BREF_WEB_MAX_BODY_BYTES` controls the gateway's body limit and, unless
application INI sets them, PHP's `post_max_size`/`upload_max_filesize`, in exact
decimal bytes. The default is
6,291,456 bytes; allowed values are positive integers through 2,147,483,647.
This is an input bound, not a promise that a body near the configured limit fits
in the environment's memory. Multipart framing consumes part of the total body
limit, so an individual file cannot necessarily reach that full size.

`configure.php` reads application INI with CLI OPcache disabled before generating the final runtime
overrides. Additional disabled functions survive; `fastcgi_finish_request` is
always added. Unrelated INI directives and explicit `openssl.cafile`/`curl.cainfo`
settings are preserved. Otherwise PHP's TLS CA settings use the readable runtime
or system bundle selected in `SSL_CERT_FILE`. A non-file PHP session handler keeps
its application save path.

As with Bref, directives set by application INI win. The generated INI only
fills in directives the application left unset: logging, streaming/buffering,
writable temporary paths, memory, OPcache and input limits, using
`BREF_WEB_PHP_MEMORY_LIMIT`, `BREF_WEB_OPCACHE_MEMORY_MB` and
`BREF_WEB_MAX_BODY_BYTES`. The extension directory and the disabled
`fastcgi_finish_request` are always enforced. Custom extension binaries must be packaged for the selected
runtime/architecture and loaded from its extension directory or an explicit path.

FPM's per-request wall timeout defaults to 28 seconds and also covers shutdown
work. It can be changed with `BREF_WEB_FPM_TIMEOUT_SECONDS`. FPM checks it only
every third of the timeout, so a worker may run up to a third longer. Gateway HTTP timeouts
and client disconnects do not prove PHP stopped. `fastcgi_finish_request` remains
disabled so a response cannot deliberately finish while hidden PHP work continues.

Initialization loads an available Composer autoloader, reuses Bref's optional
secrets loader, requires `BREF_WEB_INIT_FILE` when configured, and calls registered
`beforeStartup` subscribers. Use `putenv()` in custom initialization when a value
must reach the gateway or FPM. The PHP helper then replaces itself with the Rust
gateway (the executable passed to `bootstrap.php`), carrying its environment
without serializing secrets to logs or files. Managed Node starts the helper and
remains the outer supervisor.

This does not preserve Bref's `afterStartup`, invocation hooks, persistent hook
objects, cold-start tracker or event-runtime error metrics. The PHP helper runs
before the gateway/FPM become ready and does not execute the HTTP front controller.
Changes to the runtime's own pool and configuration variables must be set as
function environment variables: initialization runs after they are read.
Application startup hooks remain trusted application code and can write their
own logs.

The runtime is tested as a nonroot process, with a read-only root filesystem and
writable `/tmp`. The `nobody` pool user is ignored by unprivileged
FPM; FPM workers retain the container's user identity, as on Lambda.

Run `./runtime/php/tests.sh` from the repository to verify environment inheritance,
initialization ordering, safe initialization failures, exact body limits,
application INI/TLS/disabled-function precedence, alternate root path derivation,
invalid configuration failures and actual four-worker pool startup/shutdown in
the Bref PHP 8.4 container. It uses no AWS credentials or networking.
`BREF_WEB_TEST_IMAGE` can select another local image. The alternate-root unit check
still has `/opt` available; only independent exact-archive tests with the relocated
files can establish that their native dependencies and paths actually work.
