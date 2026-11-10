# Real Bref compatibility fixture

This small Composer application pins **bref/bref 3.0.15**. It uses the package's
actual EventDispatcher and BrefEventSubscriber classes, rather than replacing
them with test doubles. Its front controller requires Composer autoload on each
FPM request and reports narrowly scoped diagnostic fields for local assertions.
Do not deploy this diagnostic front controller as a production application.

The committed lock pins Bref source reference
`18776f2f9bcf6be47736a33b9dfa301085ee827e` and all ten production packages. The
harness verifies both that version/reference and the actual vendor class path.
No AWS SDK, secrets service, Laravel or Symfony framework app is installed;
Symfony Process is one of Bref's own production dependencies.

Install with the committed Composer lock using PHP 8.4:

```sh
composer install --no-dev --prefer-dist --no-interaction --no-scripts --no-plugins
```

Dependency installation needs package-download access once. Runtime tests use
only the installed files and a loopback endpoint. The initial installation used
Composer 2.11.1; its downloaded phar was verified with upstream SHA256
`6f1b891e1d4927d62740276c540fb490e47689e19e102c49e8da849794882f8c`.

Run this app as the exact archive's application root, or mount the whole fixture
over `/var/task` in the local comparison container and mount `runtime/php/` back
at `/var/task/compat` and `runtime/launcher/` at `/var/task/runtime/launcher`. Preserve the fixture's
`php/conf.d` directory: it enables `intl`, changes `precision`, and disables two
additional functions to test application configuration precedence.

Then run:

```sh
python3 tests/bref/run.py --base-url http://127.0.0.1:8084 --output results/real-bref.json
```

The shared initialization helper must load `/var/task/vendor/autoload.php`
before it starts the runtime. Composer's root autoload file registers a real
subscriber. `beforeStartup` writes one marker and sets an inherited environment
value. Each PHP request must observe that initialization once, preserve its own
request ID, and reset ordinary static and `putenv()` state between requests,
including when FPM reuses the same worker. A configured `intl` call also executes,
so the test checks native extension operation rather than only file presence.

The final contract assertion deliberately verifies the current gaps:
`afterStartup`, invocation hooks and Event Function context are not provided;
PHP objects created in the initialization helper do not survive into FPM.
Those gaps are also listed as `unsupported` in the result JSON. Passing this
assertion means the boundary is recorded accurately, not that those features
have been implemented. No Laravel/Symfony or AWS service compatibility is implied.
