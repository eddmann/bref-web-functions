# Shared Node launcher

Node supervises processes and serves no HTTP. Rust is the default gateway; select
`BREF_WEB_GATEWAY=caddy` to run the reference transport. The launcher waits for
readiness, stops on an unexpected essential-child exit, and forwards shutdown
signals with a bounded cleanup budget.

For the managed Web Function ZIP, `web-entry.js` is archived as root `index.js`.
It dynamically imports `web-entry.cjs`, archived as `launcher/entry.cjs`, so the
entry works with an application's CommonJS or ES-module `package.json`.
`web-entry.cjs` establishes archive paths and invokes
`main({ initializeRust: true })` in the shared supervisor. Node then starts the
shared PHP initialization helper, which execs Rust with its initialized
environment. Rust starts and supervises PHP-FPM. No second Node process or secret
IPC file is required.

The shared helper remains at `/var/task/compat/entrypoint.sh` inside artifacts;
its repository source lives under `runtime/php/`. Runtime source layout and
artifact layout are intentionally distinct and are mapped by the packager.

Rust ZIPs require exactly these launcher source mappings:

| Repository source | Archive path |
| --- | --- |
| `runtime/launcher/web-entry.js` | `index.js` |
| `runtime/launcher/web-entry.cjs` | `launcher/entry.cjs` |
| `runtime/launcher/index.js` | `launcher/index.cjs` |
| `runtime/launcher/config.cjs` | `launcher/config.cjs` |

`index.js` lazy-loads the [Caddy reference](../../reference/caddy/README.md) only
when selected. A Rust archive has no dependency on the `reference/` directory.
The supervisor exposes `main()` for the entry and `caddyConfig()` for local
configuration inspection. `config.cjs` provides shared address/integer parsing.

Normal source/container use can launch `node runtime/launcher/index.js` after
PHP initialization. The ZIP entry is for an assembled archive, whose manifest
and `runtime-config.json` are supplied by the packager.
