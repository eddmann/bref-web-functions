# Node-first Web Function package

`scripts/package.py` assembles a reproducible ZIP with this startup path:

```text
Managed Node 24 loads index.js and stays alive
  -> shared compat/entrypoint.sh prepares writable configuration
  -> PHP loads Composer, optional Bref secrets and beforeStartup
  -> PHP execs the Rust gateway with its initialized environment
  -> Rust starts and supervises PHP-FPM
```

The launcher reuses `runtime/launcher/index.js`; the ZIP contains no second Node binary
or Node process. Initialization results do not pass through plaintext files, logs
or IPC. Normal application HTTP traffic goes directly to Rust.

The root `index.js` is valid with either CommonJS or ES-module application
`package.json` settings; it imports the supervisor's explicit `.cjs` files.
Application asset-tooling metadata is preserved rather than rewritten.

The `runtime/` directory contains an exported Bref image's original `/opt`
contents. The shared compatibility code selects that relocated directory using
`BREF_WEB_RUNTIME_ROOT`. Rust is stored under `bin/`. PHP extensions and native
libraries are included from the supplied runtime; the builder checks every ELF
file's 64-bit little-endian architecture. Dynamic linking and native execution
must be tested separately on the target runtime.

## Build

Inputs are explicit, already-exported files. The builder never contacts AWS,
pulls images or copies `/opt` from the host.

For example, export the pinned native runtime from a container that is created
but never started. Use the same local Docker daemon as the build script and an
empty runtime destination:

```sh
mkdir -p build/amd64
docker create --platform linux/amd64 --name bref-export-native \
  bref/php-84@sha256:cf1cd6aeeed301781b6a04a741fdecd6d40c1079bb25549b82df10c69633870f
docker cp bref-export-native:/opt build/amd64/runtime
docker rm bref-export-native
```

The ARM64 counterpart also needs no emulation to export files:

```sh
mkdir -p build/arm64
docker create --platform linux/arm64 --name bref-export-arm64 \
  bref/arm-php-84@sha256:472f7ce7118a68a95c1c90c0aa1291af554f7fcff325993b7eee4d147d38f57f
docker cp bref-export-arm64:/opt build/arm64/runtime
docker rm bref-export-arm64
```

```sh
python3 scripts/package.py \
  --runtime-dir build/amd64/runtime \
  --rust-binary build/amd64/bref-web-gateway \
  --architecture x86_64 \
  --output build/packages/bref-web-local-x86_64.zip
```

An x86_64 package is marked **local-test-only**. Lambda Web Functions require
ARM64, so the corresponding release build uses matching ARM64 inputs:

```sh
python3 scripts/package.py \
  --runtime-dir /path/to/exported/arm64/opt \
  --rust-binary build/arm64/bref-web-gateway \
  --architecture aarch64 \
  --output build/packages/bref-web-arm64.zip
```

The default application is a small non-destructive PHP JSON smoke endpoint.
It contains none of the crash/hang integration routes. To package an application,
pass a prepared production staging directory explicitly:

```sh
python3 scripts/package.py \
  --runtime-dir build/amd64/runtime \
  --rust-binary build/amd64/bref-web-gateway \
  --architecture x86_64 \
  --app-dir /path/to/production-staging \
  --front-controller public/index.php \
  --document-root public \
  --output build/packages/application-local.zip
```

The staging directory supplies the application and its production Composer
dependencies. Familiar `.env`/`.env.*`, credential files, VCS metadata,
`node_modules`, test/build/result directories and logs are omitted. Additional
app-relative exclusions can be given with repeated `--exclude` globs. This is a
packaging allow-boundary, not a secret scanner: stage only files intended for
deployment, including any precompiled application configuration. Reserved package
path collisions fail the build instead of overwriting application files.

Paths and archive order/timestamps are deterministic. Executable modes and safe
symlinks are preserved. Original absolute `/opt/...` symlinks are rebased within
the archive. The only allowed external link is the runtime's existing
`bref/ssl/cert.pem -> /etc/ssl/cert.pem`, preserved without dereferencing it against
the build host. Test that system CA bundle in the actual runtime image.
Event-runtime bootstrap files are omitted.

Every archive contains `package-manifest.json` with file hashes, modes, symlink
targets and ELF machine types. A sibling `.zip.sha256` identifies the complete
artifact. The builder enforces a 512 MiB uncompressed package bound. It records
native/AWS execution as unperformed; ELF inspection alone is not execution proof.

## Extract and test the exact artifact

Python's plain `ZipFile.extractall()` does not restore Unix symlinks/modes. Use:

```sh
python3 packaging/extract.py \
  build/packages/bref-web-local-x86_64.zip \
  build/packages/extracted-native
```

The destination must be empty. Extraction verifies every file against the
manifest before writing, rejects traversal/unsupported file types and symlinked
parents, then restores modes and links. It does not authenticate an artifact's
origin; compare its external SHA-256 with the artifact selected for testing.

Run the malformed-archive regressions with `python3 packaging/tests.py`. They cover
payload corruption, traversal, duplicate entries, symlink/file parent collisions,
external/dangling links, executable restoration and unexpected permission bits.

Mount the extracted contents read-only at `/var/task` in the managed Node 24 base
image, make `/tmp` writable, and override the local container entrypoint with
`/var/lang/bin/node /var/task/index.js`. Do not use the Bref image for this test:
its original `/opt` would hide relocation mistakes. Bind only a local test port
and set `AWS_LAMBDA_HTTP_ENDPOINT` to the matching container listener address.

The complete local native runner automates those steps and records shutdown:

```sh
python3 scripts/test-package.py build/packages/bref-web-local-x86_64.zip \
  --output results/preaws/package-smoke
```

First check the smoke response, extension loading and shared initialization.
Then build a separate explicitly named test archive with `--app-dir tests/fixtures`
for the local destructive integration suite. Never deploy that test archive.

Before claiming AWS compatibility, execute the ARM64 archive natively and verify
the managed runtime, initialization deadline, metadata/credentials, real ingress
streaming, scaling and shutdown in an isolated AWS environment. This packaging
work does not add unsupported Bref invocation hooks or API Gateway contexts.
