# Node launcher

Lambda Web Functions run on the managed Node runtime, so Node is the outer
process, playing the part of Bref's Bash bootstrap. It serves no HTTP: it starts
`php -n compat/bootstrap.php` with the runtime's libraries on the library path
(the [PHP startup steps](../php/) then exec the Rust gateway), waits until the
gateway listens, stops on an unexpected exit and forwards shutdown signals with
a bounded cleanup budget.

Secrets loaded during initialization never pass through the Node process,
files or IPC: PHP hands its environment to Rust by `exec`.

The packager maps the launcher sources to these archive paths:

| Repository source | Archive path |
| --- | --- |
| `runtime/launcher/web-entry.js` | `index.js` |
| `runtime/launcher/web-entry.cjs` | `launcher/entry.cjs` |
| `runtime/launcher/index.js` | `launcher/index.cjs` |
| `runtime/launcher/config.cjs` | `launcher/config.cjs` |

The root `index.js` dynamically imports `launcher/entry.cjs`, so it works whether
the application's `package.json` declares CommonJS or ES modules. `entry.cjs`
derives the archive paths and calls `main({ initializeRust: true })`.

Run directly (`node runtime/launcher/index.js`), the launcher starts the binary in
`BREF_WEB_RUST_BIN` without PHP initialization. `tests/hardening/run.py` uses
this with a fake FastCGI backend.
