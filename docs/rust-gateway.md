# Rust HTTP/FastCGI gateway

The gateway sends every application request to the PHP file selected by
`BREF_WEB_SCRIPT_FILENAME` (default `/var/task/public/index.php`).
`BREF_WEB_DOCUMENT_ROOT` defaults to `/var/task/public`.

`SCRIPT_NAME` and `DOCUMENT_URI` describe the selected front controller's path
relative to the document root. For example, `/var/task/public/admin/front.php`
with document root `/var/task/public` produces `/admin/front.php`. The document
root is matched by path components, so `/var/task/public-other` is not treated
as part of `/var/task/public`.

If the controller is outside the document root, the gateway uses its basename
at the URL root: `/var/task/controllers/app.php` produces `/app.php`. This is a
metadata convention for a fixed front controller; it does not expose filesystem
paths or add a static-file route. Path derivation is lexical and does not resolve
symlinks. `SCRIPT_FILENAME` remains the explicitly configured filesystem path.

`REQUEST_URI` retains the incoming escaped path and raw query. `PATH_INFO`
retains the existing decoded incoming path behavior. Changing the controller
does not change these request fields.

Run focused tests with `cargo test --locked cgi::tests`, or all unit tests with
`cargo test --locked`.
